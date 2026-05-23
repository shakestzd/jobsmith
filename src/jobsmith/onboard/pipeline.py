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

Stub note
---------
The actual agentic dispatch (parsers, gap-interview) is deliberately stubbed.
Slice-4 adds the parsers; slice-5 adds gap-interview. This slice delivers:
  - Command shell (in cli.py)
  - Repo bootstrap (phase 0, via scaffold_repo)
  - Clobber guard
  - State plumbing (.onboard-state/)
  - Dispatch boundary (this module)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
    """Create .onboard-state/ and write initial run metadata.

    Returns the state directory path.
    """
    state_dir = repo_root / ".onboard-state"
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
) -> int:
    """CLI entry point: dispatch the onboarding pipeline agentic run.

    Spawns the agentic run with ``cwd = repo_root`` and writes state to the
    ``.onboard-state/`` directory. This is the non-SSE CLI path (no live event
    streaming — the CLI is not in the API event loop).

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

    Returns
    -------
    int
        Exit code: 0 on success, non-zero on failure.

    Stub note
    ---------
    The agentic dispatch body is a stub — slice-4 (parsers) and slice-5
    (gap-interview) will wire in the real agent invocations. This function
    establishes the state directory and plumbing so those slices can plug in.
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
    # STUB: agentic dispatch boundary
    # Slice-4 (parsers) plugs in here — e.g.:
    #   from jobsmith.onboard.parsers import run_ingestion
    #   rc = run_ingestion(state_dir, repo_root, ...)
    # Slice-5 (gap-interview) plugs in after parsers:
    #   from jobsmith.onboard.gap_interview import run_gap_interview
    #   rc = run_gap_interview(state_dir, repo_root, ...)
    # For now we write a completion marker so callers can assert success.
    # -----------------------------------------------------------------
    metadata_path = state_dir / "run.json"
    if metadata_path.exists():
        data = json.loads(metadata_path.read_text())
        data["status"] = "dispatched"
        metadata_path.write_text(json.dumps(data, indent=2))

    return 0


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
) -> int:
    """In-process onboarding pipeline callable for the API path.

    Designed to be wrapped in ``asyncio.to_thread`` by slice-6's route
    (mirroring how apply's API path uses ``apply.run_apply``). The ``events``
    argument accepts an EventSink (e.g. a ``_SupervisorEventSink``) so SSE
    subscribers receive live pipeline events.

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
    # STUB: in-process agentic dispatch boundary
    # Slice-4 plugs in parsers here; slice-5 plugs in gap-interview.
    # -----------------------------------------------------------------
    metadata_path = state_dir / "run.json"
    if metadata_path.exists():
        data = json.loads(metadata_path.read_text())
        data["status"] = "dispatched"
        metadata_path.write_text(json.dumps(data, indent=2))

    _emit("phase_complete", "onboard", message="onboard pipeline complete")
    return 0
