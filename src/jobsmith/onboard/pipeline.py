"""jobsmith.onboard.pipeline — dual-entry onboarding pipeline (feat-19e2d594).

This module is the boundary between the CLI/API layers and the agentic
onboarding logic. It exposes two entry points that mirror apply's architecture:

CLI path (``dispatch_onboard_pipeline``)
    Spawns the agentic run via ``headless.run_phase`` (Popen with ``cwd`` =
    resolved repo root). Writes state to the DB. No live SSE — the CLI is not
    in the API event loop.

API path (``run_onboard_pipeline``)
    Runs in-process via ``asyncio.to_thread`` + ``supervisor.register_run``
    (same pattern as apply's API path) so slice-6's web flow gets live SSE.
    Build as a callable that slice-6 can wrap in a route.
    **Do NOT add the FastAPI route here** — that is slice-6's responsibility.

Phase 0 (ensured by the CLI command before dispatch)
    Repo bootstrap: ensure ``.apply-config.yaml`` exists. If absent, scaffold
    it via ``scaffold_repo()``. Done in the CLI command, not here.

Gap-interview I/O design
------------------------
Neither entry point calls raw ``input()``.

CLI path:
    ``dispatch_onboard_pipeline`` accepts an ``input_fn`` keyword argument
    (default: built-in ``input``).  Tests and non-interactive callers pass a
    mock to avoid blocking.  The ``input_fn`` is forwarded to
    ``run_gap_interview_cli``.

API path:
    ``run_onboard_pipeline`` accepts an ``answer_callback`` keyword argument.
    When provided it is called with the question list and must return an
    ``answers`` dict (``{section.field: str}``).  This is how slice-6 will
    inject web-layer answers.  When ``None``, the API path emits
    ``gap_questions`` events and proceeds with empty answers (slice-6 wires
    the callback).
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from jobsmith.onboard.gap import build_gap_questions, run_gap_interview_cli
from jobsmith.onboard.merge import merge_candidates_to_masters
from jobsmith.onboard.parsers import run_ingestion

logger = logging.getLogger(__name__)

# Sentinel constants for clobber policy
CLOBBER_ABORT = "abort"
CLOBBER_FORCE = "force"
CLOBBER_MERGE = "merge"


# ---------------------------------------------------------------------------
# Helper: check whether master YAMLs contain real (non-stub) content
# ---------------------------------------------------------------------------


def _masters_have_content(repo_root: Path) -> bool:
    """Return True if any master YAML under repo_root is non-empty/non-stub.

    "Non-empty" means the file exists AND contains at least one non-comment,
    non-whitespace line that isn't the standard stub header.
    """
    from jobsmith.config import find_config, load_config
    from jobsmith.paths import resolve

    config_path = find_config(repo_root)
    if config_path is None:
        return False

    config = load_config(config_path)
    check_paths = [
        resolve(config.master.work_yml, repo_root),
        resolve(config.master.skill_yml, repo_root),
        resolve(config.master.education_yml, repo_root),
        resolve(config.master.author_yml, repo_root),
    ]

    for path in check_paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return True
    return False


# ---------------------------------------------------------------------------
# Helper: initialise .onboard-state/ run metadata
# ---------------------------------------------------------------------------


def _init_onboard_state(
    repo_root: Path,
    run_id: str,
    *,
    resume_file: Path | None = None,
    linkedin_export: Path | None = None,
    linkedin_url: str | None = None,
    paste: str | None = None,
    paste_file: Path | None = None,
) -> Path:
    """Create a per-run .onboard-state/{run_id}/ dir and write run metadata.

    Each run gets its own isolated state directory so candidate-*.json files
    from a prior (or concurrent) run are never silently merged/reused — see
    ingest._write_candidate_files which accumulates within a state dir.

    Returns the state directory path.
    """
    state_dir = repo_root / ".onboard-state" / run_id
    state_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "started",
        "inputs": {
            "resume_file": str(resume_file) if resume_file else None,
            "linkedin_export": str(linkedin_export) if linkedin_export else None,
            "linkedin_url": linkedin_url,
            "paste": paste,
            "paste_file": str(paste_file) if paste_file else None,
        },
    }
    (state_dir / "run.json").write_text(json.dumps(metadata, indent=2))
    return state_dir


# ---------------------------------------------------------------------------
# CLI path: dispatch_onboard_pipeline
# ---------------------------------------------------------------------------


def dispatch_onboard_pipeline(
    *,
    repo_root: Path,
    resume_file: Path | None = None,
    linkedin_export: Path | None = None,
    linkedin_url: str | None = None,
    paste: str | None = None,
    paste_file: Path | None = None,
    run_id: str | None = None,
    input_fn: Callable[[str], str] | None = None,
    clobber: str = CLOBBER_MERGE,
) -> int:
    """CLI entry point: dispatch the onboarding pipeline agentic run.

    Runs in-process: ingest → gap-interview → merge → lint-gate.
    Does not call raw ``input()``; all user I/O goes through ``input_fn``
    so callers and tests can inject a mock without blocking.

    Parameters
    ----------
    repo_root:
        Resolved repo root directory. Must already be bootstrapped (phase 0
        ensures ``.apply-config.yaml`` exists before this is called).
    resume_file:
        Optional path to a resume file (PDF, DOCX, TXT, or Markdown).
    linkedin_export:
        Optional path to a LinkedIn data export ZIP.
    linkedin_url:
        Optional public LinkedIn profile URL for scraping.
    paste:
        Optional raw pasted resume/profile text.
    paste_file:
        Optional path to a file containing pasted text.
    run_id:
        Optional run ID override. Auto-generated if not provided.
    input_fn:
        Callable(prompt: str) -> str used for gap-interview prompts.
        Defaults to built-in ``input``.  Pass a mock in tests.

    Returns
    -------
    int
        Exit code: 0 on success, non-zero on failure.
    """
    effective_run_id = run_id or uuid.uuid4().hex

    logger.info(
        "dispatch_onboard_pipeline: repo_root=%s run_id=%s",
        repo_root,
        effective_run_id,
    )

    state_dir = _init_onboard_state(
        repo_root,
        effective_run_id,
        resume_file=resume_file,
        linkedin_export=linkedin_export,
        linkedin_url=linkedin_url,
        paste=paste,
        paste_file=paste_file,
    )

    # -----------------------------------------------------------------
    # Phase 1: ingest specialists (parsers — slice-4)
    # -----------------------------------------------------------------
    ingest_rc = run_ingestion(
        state_dir,
        repo_root,
        resume_file=resume_file,
        linkedin_export=linkedin_export,
        linkedin_url=linkedin_url,
        paste=paste,
        paste_file=paste_file,
    )
    logger.info("dispatch_onboard_pipeline: ingest rc=%d", ingest_rc)

    # -----------------------------------------------------------------
    # Phase 2: gap-interview (CLI path — injectable input_fn, no raw input)
    # -----------------------------------------------------------------
    answers = run_gap_interview_cli(state_dir, input_fn=input_fn)

    # -----------------------------------------------------------------
    # Phase 3: merge + lint-gate
    # -----------------------------------------------------------------
    merge_result = merge_candidates_to_masters(
        state_dir,
        repo_root,
        answers,
        clobber=clobber,
    )

    metadata_path = state_dir / "run.json"
    if metadata_path.exists():
        data = json.loads(metadata_path.read_text())
        data["status"] = "complete" if merge_result.ok else "lint_failed"
        if not merge_result.ok:
            data["lint_errors"] = merge_result.lint_errors
        metadata_path.write_text(json.dumps(data, indent=2))

    if not merge_result.ok:
        logger.error(
            "dispatch_onboard_pipeline: lint gate failed — %d errors",
            len(merge_result.lint_errors),
        )
        return 1

    return ingest_rc


# ---------------------------------------------------------------------------
# API path: run_onboard_pipeline (slice-6 callable)
# ---------------------------------------------------------------------------


def run_onboard_pipeline(
    *,
    repo_root: Path,
    resume_file: Path | None = None,
    linkedin_export: Path | None = None,
    linkedin_url: str | None = None,
    paste: str | None = None,
    paste_file: Path | None = None,
    run_id: str | None = None,
    events=None,
    answer_callback: Callable[[list], dict[str, str]] | None = None,
    clobber: str = CLOBBER_MERGE,
) -> int:
    """In-process onboarding pipeline callable for the API path.

    Designed to be wrapped in ``asyncio.to_thread`` by slice-6's route
    (mirroring how apply's API path uses ``apply.run_apply``). The ``events``
    argument accepts an EventSink (e.g. a ``_SupervisorEventSink``) so SSE
    subscribers receive live pipeline events.

    Gap-interview I/O
    -----------------
    The API path NEVER blocks on ``input()``.  Instead:

    1. Gap questions are emitted as a ``gap_questions`` event so slice-6 can
       render them over SSE.
    2. If ``answer_callback`` is provided it is called with the question list
       and must return an ``answers`` dict (``{section.field: str}``).  This
       is how slice-6 will inject web-layer answers once it has collected them.
    3. If ``answer_callback`` is ``None`` the pipeline proceeds with empty
       answers (all optional merges use whatever the candidate files contain).

    Parameters
    ----------
    repo_root:
        Resolved repo root directory.
    resume_file, linkedin_export, linkedin_url, paste, paste_file:
        Input sources (same as ``dispatch_onboard_pipeline``).
    run_id:
        Optional run ID. Auto-generated if not provided.
    events:
        Optional EventSink. When provided, pipeline events are emitted so
        SSE subscribers receive them. Pass ``None`` for tests / CLI use.
    answer_callback:
        Optional callable(questions: list[GapQuestion]) -> dict[str, str].
        Slice-6 injects this to feed web-collected answers into the merge step.

    Returns
    -------
    int
        Exit code: 0 on success, non-zero on failure.

    Usage (slice-6 route, do NOT add FastAPI route here):
    ::

        from jobsmith.onboard.pipeline import run_onboard_pipeline
        from jobsmith.api.supervisor import get_supervisor

        supervisor = get_supervisor()
        run_id = uuid.uuid4().hex
        sink = supervisor.register_run(run_id=run_id, slug="onboard")
        task = asyncio.create_task(
            asyncio.to_thread(
                run_onboard_pipeline,
                repo_root=repo_root,
                resume_file=resume_file,
                run_id=run_id,
                events=sink,
                answer_callback=my_answer_callback,
            )
        )
        supervisor.set_task(run_id, task)
    """
    from jobsmith.core.events import PipelineEvent

    effective_run_id = run_id or uuid.uuid4().hex

    logger.info(
        "run_onboard_pipeline: repo_root=%s run_id=%s",
        repo_root,
        effective_run_id,
    )

    def _emit(kind: str, phase: str, **payload) -> None:
        if events is not None:
            try:
                event = PipelineEvent(kind=kind, phase=phase, payload=payload)
                events.emit(event)
            except Exception:  # noqa: BLE001
                pass

    _emit("phase_start", "onboard", message="onboard pipeline starting")

    state_dir = _init_onboard_state(
        repo_root,
        effective_run_id,
        resume_file=resume_file,
        linkedin_export=linkedin_export,
        linkedin_url=linkedin_url,
        paste=paste,
        paste_file=paste_file,
    )

    # -----------------------------------------------------------------
    # Phase 1: ingest specialists (parsers — slice-4)
    # -----------------------------------------------------------------
    _emit("phase_start", "ingest", message="ingesting profile sources")
    ingest_rc = run_ingestion(
        state_dir,
        repo_root,
        resume_file=resume_file,
        linkedin_export=linkedin_export,
        linkedin_url=linkedin_url,
        paste=paste,
        paste_file=paste_file,
    )
    logger.info("run_onboard_pipeline: ingest rc=%d", ingest_rc)
    _emit("phase_complete", "ingest", message="ingest complete", rc=ingest_rc)

    # -----------------------------------------------------------------
    # Phase 2: gap-interview (API path — no blocking input, emit questions)
    # -----------------------------------------------------------------
    questions = build_gap_questions(state_dir)
    _emit(
        "gap_questions",
        "gap",
        message="gap interview questions",
        questions=[q.to_dict() for q in questions],
    )

    # Collect answers: use callback if provided, else empty (slice-6 wires this)
    if answer_callback is not None:
        answers: dict[str, str] = answer_callback(questions)
    else:
        answers = {}

    # -----------------------------------------------------------------
    # Phase 3: merge + lint-gate
    # -----------------------------------------------------------------
    _emit("phase_start", "merge", message="merging candidates to masters")
    merge_result = merge_candidates_to_masters(
        state_dir,
        repo_root,
        answers,
        clobber=clobber,
    )
    _emit(
        "phase_complete",
        "merge",
        message="merge complete",
        ok=merge_result.ok,
        lint_errors=merge_result.lint_errors,
    )

    metadata_path = state_dir / "run.json"
    if metadata_path.exists():
        data = json.loads(metadata_path.read_text())
        data["status"] = "complete" if merge_result.ok else "lint_failed"
        if not merge_result.ok:
            data["lint_errors"] = merge_result.lint_errors
        metadata_path.write_text(json.dumps(data, indent=2))

    # Terminal event: a single phase_complete for the "onboard" phase marks the
    # whole pipeline done. Subscribers (slice-6 web wizard) close the SSE stream
    # only on this event — never on a subphase (ingest/merge) completion.
    _emit(
        "phase_complete",
        "onboard",
        message="onboarding complete" if merge_result.ok else "onboarding finished with lint errors",
        ok=merge_result.ok,
    )

    _emit("phase_complete", "onboard", message="onboard pipeline complete")
    return 0 if merge_result.ok else 1
