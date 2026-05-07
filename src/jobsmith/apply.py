"""apply.py — three-phase apply pipeline for `jobsmith apply <url>`.

Public API
----------
- :func:`derive_slug` — sanitize a JD URL into a filesystem-safe slug
- :func:`ensure_bootstrap` — auto-bootstrap `.apply-config.yaml` if missing
- :func:`build_phase_prompt` — construct the user prompt for each phase
- :func:`run_phase_iter` — generator yielding :class:`PipelineEvent` objects
  per phase completion (gather→draft→render). Consumers observe phase-granular
  events; per-specialist granularity is deferred to slice 8.
- :func:`run_apply` — orchestrate all three phases with confirm gates and
  per-phase resume from completed work. Internally drives
  ``run_phase_iter()`` and discards events for the CLI path.

Pipeline state — DB vs disk (trk-60217f9f, 0.8.4)
-------------------------------------------------
The ``apply_state`` and ``apply_state_log`` tables are the source of truth
for orchestrator-managed state and the agent transcript:

- ``kind=manifest`` — orchestrator's per-slug run manifest (Pass 2).
- ``kind=spec-<specialist>`` — per-specialist input envelopes (Pass 2).
- ``kind=apply-<specialist>-result`` — per-specialist result envelopes (Pass 3).
- ``apply_state_log`` rows — agent transcript stream, polled by the
  supervisor's ``_tail_state_log`` (Pass 4).

Specialist content artifacts (``jd-parsed.json``, ``fit-score.json``,
``bullet-selection.json``, ``prose-draft.md`` etc.) remain on disk under
``applications/{slug}/.apply-state/`` so that ``assemble.py`` and the
review readers in ``_state_readers.py`` continue to function. Migrating
each content kind to ``apply_state`` rows is tracked as follow-up after
this track lands; the specialist prompts already write the result
envelope to the DB (Pass 3) which is what the orchestrator and reviewers
need for resume + UI surfacing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from . import headless
from . import plugin_dir as get_plugin_dir
from ._state_readers import ARTIFACT_READERS
from .benchmarks import resolve_benchmark_or_fallback
from .config import CONFIG_FILENAME, find_config, load_config
from .db import get_state, open_pipeline_db
from .guard import check_anchors
from .paths import resolve
from .render import ApplyRenderer

if TYPE_CHECKING:
    from .api.client import JobsmithClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Legacy dual-write — feat-e3d87579 (gather/draft phases shadow-wrote to DB).
#
# 0.8.1 (S4 of trk-144d42b1, feat-9b021f76) flipped the default off. The DB
# is now the primary persistence target for specialist artifacts; FS state in
# .apply-state/ is materialized on demand via the snapshot endpoint for quarto.
# Set JOBSMITH_DUAL_WRITE=1 to re-enable the legacy shadow-write path during
# migration of any installation that hasn't fully cut over.
# ---------------------------------------------------------------------------


def _dual_write_enabled() -> bool:
    """Return True only when JOBSMITH_DUAL_WRITE=1 is explicitly set.

    Default flipped from "1" → "0" in S4 of trk-144d42b1 (feat-9b021f76).
    The DB ingest path is now primary; the legacy shadow-write hook only
    runs when JOBSMITH_DUAL_WRITE=1 is opted in.
    """
    return os.environ.get("JOBSMITH_DUAL_WRITE", "0") == "1"


def _build_client_if_enabled() -> JobsmithClient | None:
    """Construct a JobsmithClient for the dual-write hook, or None when disabled.

    Returns None when JOBSMITH_DUAL_WRITE=0 or when client construction fails
    (typically because no API token is resolvable in the supervisor's env).
    """
    if not _dual_write_enabled():
        return None
    try:
        from .api.client import JobsmithClient as _JsClient

        return _JsClient()
    except Exception as exc:  # noqa: BLE001 — never fail phase on client setup
        logger.warning("dual-write disabled — could not build JobsmithClient: %s", exc)
        return None


def _coerce_to_dict(payload: Any, kind: str) -> dict[str, Any]:
    """Wrap a non-dict artifact payload into the {text: ...} envelope expected
    by the API for text-only kinds."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return {"text": payload}
    return {"value": payload}


def dual_write_phase_artifacts(
    *,
    client: JobsmithClient,
    slug: str,
    run_id: str,
    state_dir: Path,
) -> None:
    """Mirror every loadable artifact in *state_dir* to the DB via *client*.

    Iterates ARTIFACT_READERS. For each reader that returns non-None, calls
    ``client.put_artifact(slug, run_id, kind, output)``. Wraps each PUT in
    try/except — failures log a WARNING but never raise (Phase 1: FS is still
    authoritative; DB is shadow).

    Skipped entirely when ``JOBSMITH_DUAL_WRITE=0``.
    """
    if not _dual_write_enabled():
        return

    for filename, (kind, reader) in ARTIFACT_READERS.items():
        try:
            payload = reader(state_dir)
        except Exception:  # noqa: BLE001 — readers must never fail the phase
            logger.warning(
                "dual-write reader failed for %s (kind=%s); skipping",
                filename,
                kind,
            )
            continue
        if payload is None:
            continue
        # Some readers return {} when the source file is absent — skip those too,
        # otherwise we'd PUT an empty payload for every kind on an empty state dir.
        if isinstance(payload, dict) and not payload:
            continue
        if isinstance(payload, str) and not payload:
            continue
        output = _coerce_to_dict(payload, kind)
        try:
            client.put_artifact(slug, run_id, kind, output)
        except Exception as exc:  # noqa: BLE001 — never fail phase on PUT error
            logger.warning(
                "dual-write PUT failed for kind=%s slug=%s run=%s: %s",
                kind,
                slug,
                run_id,
                exc,
            )

# ---------------------------------------------------------------------------
# Phase definitions
# ---------------------------------------------------------------------------

_PHASES = [
    ("gather", 1),
    ("draft", 2),
    ("render", 3),
]

# render runs 6 specialists sequentially; gather/draft finish well under 30
_PHASE_MAX_TURNS: dict[str, int] = {
    "gather": 30,
    "draft": 30,
    "render": 60,
}


# ---------------------------------------------------------------------------
# PipelineEvent — phase-granular events from run_phase_iter()
#
# Canonical home: jobsmith.core.events (trk-ad6d8227 Slice 1). Re-exported
# here so existing imports of ``jobsmith.apply.PipelineEvent`` keep working
# until callers migrate.
# ---------------------------------------------------------------------------

from jobsmith.core.events import PipelineEvent  # noqa: E402,F401
from jobsmith.core.slug import (  # noqa: E402,F401
    _slugify_part,
    derive_slug,
    reconcile_canonical_slug,
    resolve_canonical_slug,
)

# Back-compat alias — old code calls _reconcile_canonical_slug
_reconcile_canonical_slug = reconcile_canonical_slug

from jobsmith.core.paths import (  # noqa: E402,F401
    apply_state_dir,
    applications_dir,
    build_paths,
    pipeline_db_path,
)

# Back-compat aliases — old code uses leading-underscore names
_apply_state_dir = apply_state_dir
_applications_dir = applications_dir
_build_paths = build_paths
_pipeline_db_path = pipeline_db_path

from jobsmith.core.session import (  # noqa: E402,F401
    claude_session_file_path,
    get_or_create_session_id,
)

# Back-compat aliases — old code uses leading-underscore names
_claude_session_file_path = claude_session_file_path
_get_or_create_session_id = get_or_create_session_id


# ---------------------------------------------------------------------------
# Bootstrap check
# ---------------------------------------------------------------------------


def ensure_bootstrap(cwd: Path) -> None:
    """Ensure `.apply-config.yaml` exists in *cwd*, auto-calling `jobsmith init` if not.

    This is the programmatic path: imports and calls the init logic directly
    without spawning a subprocess, so it shares the same code path as
    `jobsmith init`.  Idempotent — no-op when already bootstrapped.

    Parameters
    ----------
    cwd:
        Directory to check/initialize (typically the current working directory).
    """
    config_path = cwd / CONFIG_FILENAME
    if config_path.exists():
        return

    click.echo(
        f"No {CONFIG_FILENAME} found in {cwd}. Auto-running `jobsmith init`...",
        err=True,
    )
    # Programmatic call to the init module's internals via the cli.init function.
    # We replicate the relevant scaffold logic to avoid Typer's Exit handling.
    _run_init(cwd)


def _run_init(target: Path) -> None:
    """Run the jobsmith scaffold logic programmatically (mirrors cli.init).

    Writes `.apply-config.yaml` and creates the standard directory structure.
    Avoids importing the Typer-decorated command directly (which raises
    SystemExit) — instead calls the underlying helpers.
    """
    from .cli import CONFIG_TEMPLATE, EXAMPLES_DIR, GITIGNORE_ADDITIONS, PROFILE_TEMPLATE

    target.mkdir(parents=True, exist_ok=True)

    # Master YAML stubs from examples (or empty stubs if examples missing)
    content_dir = target / "assets" / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    if EXAMPLES_DIR.exists():
        import shutil
        for src in EXAMPLES_DIR.glob("*.yml"):
            dst = content_dir / src.name
            if not dst.exists():
                shutil.copy(src, dst)
    else:
        for name in ("work.yml", "skill.yml", "education.yml", "author.yml", "publication.yml"):
            stub = content_dir / name
            if not stub.exists():
                stub.write_text("# Populate me with your master content\n")

    # Config file
    config_path = target / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(CONFIG_TEMPLATE)

    # Profile YAML
    profile_path = target / "private" / "capacity" / "profile.yaml"
    if not profile_path.exists():
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(PROFILE_TEMPLATE)

    # Applications dir
    apps_dir = target / "private" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)

    # .gitignore
    gitignore = target / ".gitignore"
    if gitignore.exists():
        existing = gitignore.read_text()
        if "jobsmith" not in existing:
            gitignore.write_text(existing.rstrip() + "\n" + GITIGNORE_ADDITIONS)
    else:
        gitignore.write_text(GITIGNORE_ADDITIONS.lstrip())

    click.echo(
        f"Bootstrapped jobsmith repo at {target}. "
        "Edit assets/content/*.yml and .apply-config.yaml before running apply.",
        err=True,
    )


# ---------------------------------------------------------------------------
# Auto-freeze specialist contracts (feat-385f3405)
# ---------------------------------------------------------------------------


def _auto_freeze_contracts(contracts_path: Path) -> None:
    """Stamp ``frozen_at`` with today's ISO date if it is currently null.

    The gather-phase system prompt checks ``frozen_at`` and emits PHASE_FAILED
    when it is null, blocking every new user.  This function auto-freezes on
    first apply so the pipeline proceeds without manual intervention.

    Idempotent: if ``frozen_at`` is already set the file is not modified.
    No-op if the file does not exist (non-fatal; the agent will handle it).
    """
    if not contracts_path.exists():
        return
    try:
        import yaml as _yaml
        raw = contracts_path.read_text(encoding="utf-8")
        data = _yaml.safe_load(raw)
        if not isinstance(data, dict):
            return
        if data.get("frozen_at") is not None:
            return  # already frozen — leave file untouched
        from datetime import date as _date
        today = _date.today().isoformat()
        # Minimal, targeted replacement: replace the first occurrence of
        # ``frozen_at: null`` so we don't disturb the rest of the YAML
        # (comments, ordering, whitespace).
        if "frozen_at: null" in raw:
            updated = raw.replace("frozen_at: null", f"frozen_at: '{today}'", 1)
        else:
            updated = raw.rstrip("\n") + f"\nfrozen_at: '{today}'\n"
        contracts_path.write_text(updated, encoding="utf-8")
        logger.info("Auto-froze specialist contracts at %s (frozen_at=%s)", contracts_path, today)
    except Exception as exc:  # noqa: BLE001 — never abort apply on freeze failure
        logger.warning("Could not auto-freeze contracts at %s: %s", contracts_path, exc)


# ---------------------------------------------------------------------------
# Phase prompt construction
# ---------------------------------------------------------------------------


def _format_paths_block(paths: dict[str, str]) -> str:
    """Format a deterministic Paths block for injection into a phase prompt.

    Keys are sorted alphabetically so the block is stable across runs.
    Returns an empty string when *paths* is empty (omit block from prompt).
    """
    if not paths:
        return ""
    lines = ["Paths (use these absolute paths verbatim — do NOT search for them):"]
    for key in sorted(paths.keys()):
        lines.append(f"  {key}: {paths[key]}")
    return "\n".join(lines)


def build_phase_prompt(
    phase: str,
    slug: str,
    url: str,
    *,
    paths: dict[str, str] | None = None,
    jd_text_file: Path | None = None,
) -> str:
    """Return the user prompt text for a given phase.

    The system prompt (loaded via ``--system-prompt-file``) carries the full
    phase instructions. This user prompt gives the agent its immediate task.

    Parameters
    ----------
    phase:
        One of ``"gather"``, ``"draft"``, ``"render"``.
    slug:
        Application slug derived from the URL.
    url:
        Original JD URL.
    paths:
        Optional flat string→string mapping of absolute paths to inject.
        When provided (and non-empty) a "Paths" block is appended to the
        prompt so the agent does not need to search the filesystem.
        Defaults to ``{}`` — omits the block (useful for unit tests that
        do not need path injection).
    jd_text_file:
        Gather phase only. When provided, the user has supplied the JD
        body text out-of-band (e.g. pasted from a JS-rendered portal that
        ``WebFetch`` cannot scrape). The prompt directs the gather agent
        to read the file's contents and write them into the spec.json
        ``inputs.jd_text`` field, so ``apply-jd-parser`` skips its
        WebFetch and uses the text directly. Ignored for non-gather
        phases.

    Returns
    -------
    str
        User prompt text, optionally with an injected Paths block.

    Raises
    ------
    ValueError
        If *phase* is not one of the three recognised phases.
    """
    effective_paths: dict[str, str] = paths if paths is not None else {}
    paths_block = _format_paths_block(effective_paths)

    def _with_paths(text: str) -> str:
        if paths_block:
            return f"{text}\n\n{paths_block}"
        return text

    if phase == "gather":
        prompt = (
            f"Process this JD: {url}. Application slug: {slug}. "
            "Begin Phase 1 (gather): jd-parse, fit, HM enrichment, company research, "
            "bullet selection. Pause at the analysis gate as instructed."
        )
        if jd_text_file is not None:
            prompt += (
                "\n\nThe user has provided the JD body text out-of-band "
                f"(useful for JS-rendered portals like Netflix careers). "
                f"Read it from this absolute path and copy the full contents "
                f"into the spec.json `inputs.jd_text` field that you write for "
                f"apply-jd-parser, so the parser skips its WebFetch and uses "
                f"the supplied text directly: {jd_text_file}"
            )
        return _with_paths(prompt)
    if phase == "draft":
        return _with_paths(
            f"Resume Phase 2 (draft) for slug {slug}. "
            "Read .apply-state/ artifacts. Run prose-writer + prose-qa loop until pass."
        )
    if phase == "render":
        return _with_paths(
            f"Resume Phase 3 (render) for slug {slug}. "
            "Render resume PDF, ATS check, visual layout, cover letter, index page, "
            "then jobsmith assemble."
        )
    raise ValueError(f"Unknown phase: {phase!r}. Expected one of: gather, draft, render.")


# ---------------------------------------------------------------------------
# Event rendering
# ---------------------------------------------------------------------------


def _render_event(event: headless.Event) -> str | None:
    """Format a single event as a one-line summary for stderr output.

    Returns None for events that should be skipped (e.g. verbose text).
    """
    if event.type == "tool_use":
        short_input = ""
        if event.tool_input:
            # Grab the most informative key: command, path, url, query, or first key
            for key in ("command", "path", "url", "query"):
                val = event.tool_input.get(key)
                if val:
                    short_input = f"{key}={str(val)[:60]!r}"
                    break
            if not short_input:
                first_key = next(iter(event.tool_input), None)
                if first_key:
                    short_input = f"{first_key}={str(event.tool_input[first_key])[:40]!r}"
        return f"  -> {event.tool_name}({short_input})"

    if event.type == "tool_result":
        preview = (event.tool_result or "")[:80].replace("\n", " ")
        return f"  <- {preview}"

    if event.type == "error":
        return f"  x {event.error}"

    if event.type == "phase_complete":
        return f"  [ok] phase {event.name} complete"

    if event.type == "phase_failed":
        reason = f": {event.error}" if event.error else ""
        return f"  [fail] phase {event.name} failed{reason}"

    # Skip text events (too verbose) and other pass-through types
    return None





def _snapshot_phase_drafts(phase: str, slug: str, cwd: Path) -> None:
    """Persist immutable agent-draft snapshots after a phase completes.

    `jobsmith feedback record` needs a stable agent baseline to diff against
    user edits. Specialists overwrite their drafts on revision (and the user
    may edit the live files), so we snapshot once at phase-completion and
    treat ``*-draft.agent.md`` as read-only thereafter.

    - After phase ``draft`` (phase 2): snapshot
      ``.apply-state/prose-draft.md`` → ``.apply-state/prose-draft.agent.md``.
    - After phase ``render`` (phase 3): snapshot the agent-written cover
      letter — usually at ``<app>/cover-letter-draft.md`` (where
      apply-cover-letter-writer writes), with a fallback to
      ``.apply-state/cover-letter-draft.md`` if the humanizer pass left one
      there — into ``.apply-state/cover-letter-draft.agent.md``.

    Silently no-ops when source files are missing or the state dir cannot
    be resolved; the apply pipeline is the source of truth and we don't
    want to fail a successful phase on a snapshot hiccup.
    """
    state_dir = _apply_state_dir(slug, cwd)
    if state_dir is None or not state_dir.is_dir():
        return
    app_dir = state_dir.parent

    if phase == "draft":
        src = state_dir / "prose-draft.md"
        if src.exists():
            shutil.copy2(src, state_dir / "prose-draft.agent.md")

    elif phase == "render":
        # Prefer the app-root copy (where apply-cover-letter-writer writes
        # per its prompt); fall back to the state-dir humanizer artifact.
        src_root = app_dir / "cover-letter-draft.md"
        src_state = state_dir / "cover-letter-draft.md"
        src = src_root if src_root.exists() else src_state
        if src.exists():
            shutil.copy2(src, state_dir / "cover-letter-draft.agent.md")


# ---------------------------------------------------------------------------
# URL → canonical slug index + per-phase resume helpers
# ---------------------------------------------------------------------------

URL_INDEX_FILENAME = ".url-index.json"

# Specialists whose successful invocation marks each phase as complete.
# A specialist is considered "done" when manifest.json.invocations contains an
# entry with that ``specialist`` name and ``status`` == "ok".
_PHASE_REQUIRED_SPECIALISTS: dict[str, tuple[str, ...]] = {
    "gather": (
        "apply-jd-parser",
        "apply-fit-scorer",
        "apply-hm-enricher",
        "apply-bullet-selector",
        "apply-company-research",
    ),
    "draft": (
        "apply-prose-writer",
        "apply-prose-qa",
    ),
    "render": (
        "apply-resume-renderer",
        "apply-cover-letter-writer",
        "apply-index-writer",
    ),
}


def required_specialists_for_phase(phase: str) -> tuple[str, ...]:
    """Return the tuple of specialist slugs required to mark *phase* complete.

    Public accessor on top of the private ``_PHASE_REQUIRED_SPECIALISTS``
    map so other modules (db_ingest backfill, slice-8 re-runs) can ask
    "did this phase finish?" without depending on apply.py internals.
    Returns an empty tuple for unknown phases.
    """
    return _PHASE_REQUIRED_SPECIALISTS.get(phase, ())


def phase_for_specialist(specialist_name: str) -> str:
    """Return the phase name that contains *specialist_name*.

    Parameters
    ----------
    specialist_name:
        A specialist slug such as ``"apply-fit-scorer"`` or
        ``"apply-prose-writer"``.

    Returns
    -------
    str
        One of ``"gather"``, ``"draft"``, or ``"render"``.

    Raises
    ------
    ValueError
        When *specialist_name* is not found in any phase.
    """
    for phase, specialists in _PHASE_REQUIRED_SPECIALISTS.items():
        if specialist_name in specialists:
            return phase
    raise ValueError(f"unknown specialist: {specialist_name!r}")


def _url_index_path(cwd: Path) -> Path | None:
    """Return the absolute path of ``applications/.url-index.json``."""
    apps_dir = _applications_dir(cwd)
    if apps_dir is None:
        return None
    return apps_dir / URL_INDEX_FILENAME


def _load_url_index(cwd: Path) -> dict[str, str]:
    """Read the URL → canonical-slug index. Returns ``{}`` on missing/malformed."""
    idx_path = _url_index_path(cwd)
    if idx_path is None or not idx_path.exists():
        return {}
    try:
        data = json.loads(idx_path.read_text())
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_url_index(cwd: Path, index: dict[str, str]) -> None:
    """Atomically write the URL → canonical-slug index."""
    idx_path = _url_index_path(cwd)
    if idx_path is None:
        return
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = idx_path.with_suffix(idx_path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    tmp.replace(idx_path)


def _scan_for_url_match(url: str, cwd: Path) -> str | None:
    """Scan ``applications/*/.apply-state/jd-parsed.json`` for one matching *url*.

    Checks ``jd_url``, ``url``, and ``apply_url`` fields in that order.  Returns
    the slug of the matching directory, or None if no candidate matches.
    """
    apps_dir = _applications_dir(cwd)
    if apps_dir is None or not apps_dir.exists():
        return None
    for jd_path in apps_dir.glob("*/.apply-state/jd-parsed.json"):
        try:
            data = json.loads(jd_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        for key in ("jd_url", "url", "apply_url"):
            if data.get(key) == url:
                return jd_path.parent.parent.name
    return None


def _resolve_starting_slug(url: str, cwd: Path) -> tuple[str, bool]:
    """Resolve which slug to start the run under.

    Returns ``(slug, from_index)`` where ``from_index`` is True iff the slug
    came from the persisted URL index or a one-time migration scan.  Falls
    back to the URL-derived slug when neither lookup succeeds.
    """
    index = _load_url_index(cwd)
    if url in index:
        return index[url], True
    # One-time migration: if the URL isn't in the index, scan jd-parsed.json
    # files under applications/* for a matching jd_url/url/apply_url field.
    scanned = _scan_for_url_match(url, cwd)
    if scanned:
        index[url] = scanned
        _save_url_index(cwd, index)
        return scanned, True
    return derive_slug(url), False


def _record_url_mapping(url: str, canonical_slug: str, cwd: Path) -> None:
    """Persist URL → canonical slug into the index, creating it if absent."""
    index = _load_url_index(cwd)
    if index.get(url) == canonical_slug:
        return
    index[url] = canonical_slug
    _save_url_index(cwd, index)


def _load_manifest(app_dir: Path, cwd: Path) -> dict | None:
    """Read the manifest blob for ``app_dir.name`` from ``apply_state`` (DB).

    Pass 2 of trk-60217f9f made the DB the source of truth, but pre-0.8.4
    applications still have only ``app_dir/.apply-state/manifest.json``
    on disk. Roborev job 962 MEDIUM caught the regression: those apps
    would no longer be recognised as resumable and would silently rerun
    from scratch when a user re-applied to the same URL.

    Read order:

    1. ``apply_state`` row, ``slug = app_dir.name``, ``kind = "manifest"``.
    2. Disk fallback at ``app_dir/.apply-state/manifest.json`` when the DB
       row is missing. The disk file is treated as authoritative input
       only (the orchestrator writes new manifests to the DB exclusively
       via Pass 2's prompts), so reads here cover the migration window.

    Returns ``None`` when neither source has a usable dict.
    """
    db_path = _pipeline_db_path(cwd)
    blob: str | None = None
    if db_path is not None and db_path.exists():
        slug = app_dir.name
        conn = open_pipeline_db(db_path)
        try:
            blob = get_state(conn, slug=slug, kind="manifest")
        finally:
            conn.close()
    if not blob:
        # Disk fallback for pre-0.8.4 applications (no DB-backed manifest
        # was ever written). Returns None on missing or malformed file.
        manifest_path = app_dir / ".apply-state" / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            blob = manifest_path.read_text(encoding="utf-8")
        except OSError:
            return None
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _phase_completed(manifest: dict | None, phase_name: str) -> bool:
    """Return True iff every required specialist for *phase_name* is done.

    "Done" means the manifest's ``invocations`` list contains at least one
    entry per required specialist with ``status == "ok"``.  Missing manifest
    or malformed invocations always return False — callers re-run the phase.
    """
    if not manifest:
        return False
    required = _PHASE_REQUIRED_SPECIALISTS.get(phase_name, ())
    if not required:
        return False
    invocations = manifest.get("invocations")
    if not isinstance(invocations, list):
        return False
    completed_specialists = {
        inv.get("specialist")
        for inv in invocations
        if isinstance(inv, dict) and inv.get("status") == "ok"
    }
    return all(spec in completed_specialists for spec in required)


# ---------------------------------------------------------------------------
# Steps 4-5: between-phase anchor guard + relevance inquiry
# ---------------------------------------------------------------------------


def _run_step45_orchestration(slug: str, cwd: Path) -> int:
    """Run Steps 4-5 between gather (phase 1) and draft (phase 2).

    Step 4 (anchor guard): cross-references anchor bullets in the master
    work YAML against the gather phase's ``bullet-selection.json``. Anchor
    bullets dropped without a reason are reported.

    Step 5 (relevance inquiry): currently surfaces unresolved drops to the
    user with a remediation hint. Full automation of the relevance-inquiry
    sub-pipeline is a follow-up; for now this guarantees the draft phase
    cannot start with missing or stale ``bullet-decisions.json``.

    Writes ``bullet-decisions.json`` (an empty mapping when anchors are
    clean — selection-side ``reason_if_dropped`` carries any drop rationale).

    Returns
    -------
    int
        ``0`` on success, ``2`` when anchors require manual resolution, or
        ``1`` when prerequisite artifacts are missing.
    """
    import json as _json

    config_path = find_config(cwd)
    if config_path is None:
        click.echo(
            "Step 4-5: cannot locate .apply-config.yaml — skipping anchor guard.",
            err=True,
        )
        return 1
    config = load_config(config_path)
    repo_root = config_path.parent

    apply_state = (
        resolve(config.output.applications_dir, repo_root) / slug / ".apply-state"
    )
    selection_path = apply_state / "bullet-selection.json"
    decisions_path = apply_state / "bullet-decisions.json"
    master_path = resolve(config.master.work_yml, repo_root)

    if not selection_path.exists():
        click.echo(
            f"Step 4-5: bullet-selection.json missing at {selection_path}. "
            "Phase 1 (gather) must produce it before draft can proceed.",
            err=True,
        )
        return 1

    result = check_anchors(master_path, selection_path)

    if result.exit_code == 0:
        # All anchors preserved (or dropped with reason via selection's
        # reason_if_dropped). Guarantee bullet-decisions.json exists so
        # the draft prompt's "MUST already exist" precondition is met.
        if not decisions_path.exists():
            apply_state.mkdir(parents=True, exist_ok=True)
            decisions_path.write_text(_json.dumps({}) + "\n")
        click.echo(
            f"Step 4: anchor guard passed — {len(result.kept)} kept, "
            f"{len(result.dropped_with_reason)} dropped with reason.",
            err=True,
        )
        return 0

    click.echo(
        f"Step 4: anchor guard FAILED — {len(result.dropped_without_reason)} "
        "anchor bullet(s) dropped without a reason:",
        err=True,
    )
    for bullet in result.dropped_without_reason[:5]:
        click.echo(f"  - {bullet.text[:100]}", err=True)
    if len(result.dropped_without_reason) > 5:
        click.echo(
            f"  ... and {len(result.dropped_without_reason) - 5} more", err=True
        )
    click.echo(
        "\nStep 5 (relevance inquiry) is not yet automated. Manually edit "
        f"{decisions_path} with reasons keyed by bullet_id, then re-invoke "
        "`jobsmith apply` to resume.",
        err=True,
    )
    return 2


# ---------------------------------------------------------------------------
# Generator: run_phase_iter()
# ---------------------------------------------------------------------------


def run_phase_iter(
    url: str,
    *,
    cwd: Path | None = None,
    skip_confirm: bool = False,
    force: bool = False,
    cancel_event: threading.Event | None = None,
    phases: list[str] | None = None,
    jd_text: str | None = None,
) -> Iterator[PipelineEvent]:
    """Yield :class:`PipelineEvent` for each phase of the apply pipeline.

    Phase-granular events ONLY (not per-specialist).  Per-specialist
    granularity is deferred to slice 8 (manifest-polling ingestor).

    Parameters
    ----------
    url:
        Job description URL.
    cwd:
        Working directory (defaults to current directory).
    skip_confirm:
        Bypass phase-gate confirmations.
    force:
        Ignore existing manifest / URL index; re-run all phases.
    cancel_event:
        When set, the generator stops after the current phase completes and
        does not start subsequent phases.  Consumers MUST also propagate the
        event to ``headless.run_phase`` (via the ``cancel_event`` kwarg) so
        a running subprocess is terminated.
    phases:
        When provided, restrict execution to the named phases (in their
        canonical gather → draft → render order). ``None`` means "run all
        not-yet-complete phases" (the default). Used by slice-8 single-
        specialist re-runs to avoid re-running upstream phases (roborev #921).
    jd_text:
        Optional JD body text supplied out-of-band, for cases where
        ``WebFetch`` cannot scrape the URL (JS-rendered career portals
        like Netflix, some Workday tenants, etc.). When provided, the
        text is written to a temp file and the gather-phase user prompt
        instructs the orchestrator to copy its contents into spec.json's
        ``inputs.jd_text`` field, so ``apply-jd-parser`` skips its
        ``WebFetch`` step.

    Yields
    ------
    PipelineEvent
        Events in order: ``phase_started`` → ``phase_complete`` (or
        ``phase_failed`` / ``guard_failed``) per phase, with an optional
        ``slug_changed`` event between gather and draft.  A ``cancelled``
        event is the final event when ``cancel_event`` was set.
    """
    resolved_cwd = cwd or Path.cwd()

    # Materialize jd_text into a temp file once (only for gather phase).
    # The orchestrator agent reads the file and inlines its contents into
    # spec.json. Always unlinked in the finally block at the end of the
    # generator — even if the consumer breaks out of the loop early or
    # garbage-collects the generator (roborev #928 LOW). The user-supplied
    # JD body is potentially sensitive (compensation, named hiring
    # managers from private channels).
    jd_text_file: Path | None = None
    if jd_text is not None and jd_text.strip():
        import tempfile as _tempfile
        _fd, _tmp_path = _tempfile.mkstemp(
            prefix=f"jobsmith-jdtext-{derive_slug(url)}-",
            suffix=".txt",
        )
        import os as _os
        with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(jd_text)
        jd_text_file = Path(_tmp_path)

    try:
        yield from _run_phase_iter_body(
            url=url,
            resolved_cwd=resolved_cwd,
            force=force,
            cancel_event=cancel_event,
            phases=phases,
            jd_text_file=jd_text_file,
        )
    finally:
        if jd_text_file is not None:
            with contextlib.suppress(OSError):
                jd_text_file.unlink()


def _run_phase_iter_body(
    *,
    url: str,
    resolved_cwd: Path,
    force: bool,
    cancel_event: threading.Event | None,
    phases: list[str] | None,
    jd_text_file: Path | None,
) -> Iterator[PipelineEvent]:
    """Generator body for :func:`run_phase_iter`, extracted so the wrapper
    can ``try/finally``-clean the temp file even when the consumer stops
    iterating early (roborev #928 LOW).
    """
    import time as _time

    # Step 1: bootstrap
    ensure_bootstrap(resolved_cwd)

    # Step 1b: seed master_content from disk YAMLs if not already loaded
    # (roborev job 962 MEDIUM). The marimo NotebookRunner drives the
    # apply pipeline through this generator, not through ``run_apply``,
    # so the master-content seed in ``run_apply`` (job 958) does not
    # cover this code path. Mirror the same idempotent ensure here so
    # specialists' ``jobsmith db dump-master --section work`` calls
    # find rows on a fresh DB.
    try:
        _config_path = find_config(resolved_cwd)
        _db_path = _pipeline_db_path(resolved_cwd)
        if _config_path is not None and _db_path is not None:
            from .master_ingest import ensure_master_loaded as _ensure_master

            _ensure_master(_db_path, repo_root=_config_path.parent)
    except Exception as exc:  # noqa: BLE001 — degrade rather than abort.
        logger.warning(
            "master_content seed (run_phase_iter) failed: %s", exc
        )

    # Step 2: resolve starting slug
    started_at = _time.time()
    plugin_directory = get_plugin_dir()

    if force:
        index = _load_url_index(resolved_cwd)
        slug = index[url] if url in index else derive_slug(url)
    else:
        slug, _from_index = _resolve_starting_slug(url, resolved_cwd)

    # Roborev job 959 HIGH + job 960 HIGH: marimo single-phase reruns
    # drive this generator with ``force=True``. Without clearing
    # apply_state for the slug, prior ``apply-<specialist>-result``
    # rows with ``status: ok`` cause the phase prompts to treat the
    # specialists as "already complete" and skip them.
    #
    # SCOPED reset: when the caller scopes ``phases`` to a single
    # downstream phase (e.g. ``phases=["draft"]``), we MUST NOT wipe
    # the whole slug — the manifest, gather-phase result envelopes,
    # and content artifacts the downstream phase reads would all be
    # lost. Drop only the rows scoped to the target phase's
    # specialists. The full ``reset_state`` runs only when the rerun
    # restarts from gather (``phases is None`` or includes "gather").
    if force:
        _force_db_path = _pipeline_db_path(resolved_cwd)
        if _force_db_path is not None and _force_db_path.exists():
            with contextlib.suppress(Exception):
                from .db import open_pipeline_db as _open_pipe_db
                from .db import reset_state as _reset_state

                full_reset = phases is None or "gather" in phases
                _conn = _open_pipe_db(_force_db_path)
                try:
                    if full_reset:
                        _reset_state(_conn, slug=slug)
                    else:
                        # Delete only the per-specialist spec / result rows
                        # for the targeted phases so upstream rows survive.
                        scoped_specs: set[str] = set()
                        for ph in phases:
                            for s in required_specialists_for_phase(ph):
                                scoped_specs.add(s)
                        kinds_to_drop = []
                        for s in scoped_specs:
                            kinds_to_drop.extend(
                                [f"spec-{s}", f"{s}-result"]
                            )
                        if kinds_to_drop:
                            placeholders = ",".join("?" for _ in kinds_to_drop)
                            _conn.execute(
                                f"DELETE FROM apply_state WHERE slug = ? "
                                f"AND kind IN ({placeholders})",
                                (slug, *kinds_to_drop),
                            )
                            _conn.commit()

                        # Roborev job 963 MEDIUM: also strip the manifest's
                        # ``invocations[]`` entries for the targeted phase
                        # specialists. Without this, the manifest still
                        # records ``status: "ok"`` for those specialists
                        # so a later normal ``jobsmith apply`` (no force)
                        # would re-read the manifest, see "phase done",
                        # and skip the phase the user just told us to
                        # rerun. Preserve upstream invocations untouched.
                        from .db import (
                            get_state as _get_state,
                            put_state as _put_state,
                        )

                        manifest_blob = _get_state(
                            _conn, slug=slug, kind="manifest"
                        )
                        if manifest_blob:
                            try:
                                manifest_data = json.loads(manifest_blob)
                            except json.JSONDecodeError:
                                manifest_data = None
                            if isinstance(manifest_data, dict):
                                invocations = manifest_data.get("invocations")
                                if isinstance(invocations, list):
                                    pruned = [
                                        inv
                                        for inv in invocations
                                        if not (
                                            isinstance(inv, dict)
                                            and inv.get("specialist")
                                            in scoped_specs
                                        )
                                    ]
                                    if len(pruned) != len(invocations):
                                        manifest_data["invocations"] = pruned
                                        _put_state(
                                            _conn,
                                            slug=slug,
                                            kind="manifest",
                                            content_blob=json.dumps(
                                                manifest_data
                                            ),
                                        )
                finally:
                    _conn.close()

    # Step 3: phase-completion gating
    apps_dir = _applications_dir(resolved_cwd)
    app_dir = apps_dir / slug if apps_dir is not None else None
    manifest = None if force or app_dir is None else _load_manifest(app_dir, resolved_cwd)

    session_id = headless.deterministic_session_id(slug)

    phase_done: dict[str, bool] = {
        name: _phase_completed(manifest, name) for name, _ in _PHASES
    }

    # All done → no events to yield
    if all(phase_done.values()) and app_dir is not None:
        return

    # Filter to the requested phases (preserving canonical ordering).
    # phases=None means "run every phase that's not yet complete" — the
    # historical behavior. phases=[name] from slice-8 re-runs scopes work
    # to a single phase so re-running apply-prose-writer (draft) does NOT
    # re-fire the gather phase first (roborev #921 HIGH).
    if phases is not None:
        requested = set(phases)
        active_phases = [(n, num) for n, num in _PHASES if n in requested]
    else:
        active_phases = list(_PHASES)

    # Auto-freeze specialist contracts on first apply (feat-385f3405).
    # Must run before the gather phase so the agent sees frozen_at non-null.
    _auto_freeze_contracts(
        plugin_directory / "agents" / "apply" / "specialist-contracts.yaml"
    )

    for phase_name, phase_num in active_phases:
        # Check cancel before starting each phase
        if cancel_event is not None and cancel_event.is_set():
            yield PipelineEvent(kind="cancelled", phase=phase_name)
            return

        # Step 3pre: anchor guard before draft
        if phase_name == "draft" and not phase_done["draft"]:
            state_dir = _apply_state_dir(slug, resolved_cwd)
            if state_dir is not None and not (state_dir / "bullet-decisions.json").exists():
                rc = _run_step45_orchestration(slug, resolved_cwd)
                if rc != 0:
                    yield PipelineEvent(
                        kind="guard_failed",
                        phase=phase_name,
                        payload={"rc": rc},
                    )
                    return

        # Skip completed phases
        if phase_done[phase_name]:
            continue

        yield PipelineEvent(kind="phase_started", phase=phase_name)

        # Step 3a: session continuity
        resume = (phase_name != "gather") and headless.session_exists(
            session_id, cwd=resolved_cwd
        )

        # Step 3b: system prompt
        system_prompt = (
            plugin_directory / "system-prompts" / f"phase-{phase_num}-{phase_name}.md"
        )
        if not system_prompt.exists():
            raise FileNotFoundError(
                f"System prompt not found: {system_prompt}"
            )

        # Step 3c: paths + prompt
        phase_paths = _build_paths(slug, resolved_cwd, plugin_directory)
        # jd_text_file is only meaningful for the gather phase; build_phase_prompt
        # ignores it for draft / render. We still pass it so the kwarg flows
        # cleanly through the iteration loop.
        prompt_text = build_phase_prompt(
            phase_name,
            slug,
            url,
            paths=phase_paths,
            jd_text_file=jd_text_file if phase_name == "gather" else None,
        )

        # Step 3f: stream events from headless
        phase_succeeded = False
        try:
            for event in headless.run_phase(
                phase=phase_name,
                session_id=session_id,
                prompt=prompt_text,
                plugin_dir=plugin_directory,
                system_prompt=system_prompt,
                resume=resume,
                cwd=resolved_cwd,
                max_turns=_PHASE_MAX_TURNS[phase_name],
                cancel_event=cancel_event,
            ):
                if event.type == "phase_complete":
                    phase_succeeded = True
                    break
                if event.type == "phase_failed":
                    yield PipelineEvent(
                        kind="phase_failed",
                        phase=phase_name,
                        payload={"error": event.error},
                    )
                    return
                if event.type == "error":
                    yield PipelineEvent(
                        kind="phase_failed",
                        phase=phase_name,
                        payload={"error": event.error},
                    )
                    return

                # Stop draining if cancelled mid-phase
                if cancel_event is not None and cancel_event.is_set():
                    yield PipelineEvent(kind="cancelled", phase=phase_name)
                    return
        except Exception as exc:  # noqa: BLE001
            # Uncaught exception from headless.run_phase (subprocess crash,
            # OOM, SDK bug, etc.).  Surface it as a terminal phase_failed
            # event so SSE consumers see a clear error marker instead of
            # a silent stream end (bug-84db2d3c / GitHub #61).
            yield PipelineEvent(
                kind="phase_failed",
                phase=phase_name,
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
            return

        if not phase_succeeded:
            yield PipelineEvent(
                kind="phase_failed",
                phase=phase_name,
                payload={"error": "no phase_complete signal emitted"},
            )
            return

        # Step 3f-snap: snapshot agent drafts
        _snapshot_phase_drafts(phase_name, slug, resolved_cwd)

        # Step 3g: between-phase orchestration after gather
        if phase_name == "gather":
            new_slug, reconciled = _reconcile_canonical_slug(
                slug, resolved_cwd, started_at
            )
            if new_slug != slug:
                old_slug = slug
                slug = new_slug
                session_id = headless.deterministic_session_id(slug)
                # Emit slug_changed and pause up to 1s for consumer ack.
                # The consumer MUST rebind their slug variable; the runner
                # does not hold any file handles to the app dir across the rename.
                ev = PipelineEvent(
                    kind="slug_changed",
                    phase=phase_name,
                    payload={"old_slug": old_slug, "new_slug": slug},
                )
                yield ev
                # 1-second timeout: we cannot block indefinitely waiting for
                # the consumer to ack. The event has already been yielded; if
                # the consumer cares it reads the event synchronously. The
                # timeout here is just a documentation-level signal.
                _time.sleep(0)  # cooperative yield point
            if reconciled:
                _record_url_mapping(url, slug, resolved_cwd)

        yield PipelineEvent(kind="phase_complete", phase=phase_name)

        # Check cancel after phase completes (before starting next)
        if cancel_event is not None and cancel_event.is_set():
            yield PipelineEvent(kind="cancelled", phase=phase_name)
            return


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------


def run_apply(
    url: str,
    *,
    cwd: Path | None = None,
    skip_confirm: bool = False,
    force: bool = False,
    verbosity: int = 0,
    renderer: ApplyRenderer | None = None,
    jd_text: str | None = None,
    slug: str | None = None,
    run_id: str | None = None,
) -> int:
    """Run the three-phase apply pipeline.

    Parameters
    ----------
    url:
        Job description URL.
    cwd:
        Working directory (defaults to current directory).
    skip_confirm:
        When True, phase-gate confirmations are bypassed (--yes flag).
    force:
        When True, ignore ``.url-index.json`` and any prior ``manifest.json``;
        start fresh from phase 1 even if a canonical directory already
        contains completed work.  Maps to the ``--force`` CLI flag.
    verbosity:
        0 = quiet (default), 1 = -v (filtered tool calls), 2 = -vv (all).
    renderer:
        Optional :class:`~jobsmith.render.ApplyRenderer` instance.  When None,
        one is constructed automatically (TTY-aware, using *skip_confirm* for
        the ``yes`` flag).
    jd_text:
        Optional JD body text supplied out-of-band, for cases where
        ``WebFetch`` cannot scrape the URL (JS-rendered career portals like
        Netflix careers, some Workday tenants). Maps to the
        ``--jd-text`` / ``--jd-text-file`` CLI flags. Written to a temp
        file so the gather orchestrator agent can read its contents and
        copy them into spec.json's ``inputs.jd_text`` field, letting
        ``apply-jd-parser`` skip its WebFetch.

    Returns
    -------
    int
        Exit code: 0 on success or clean user abort, non-zero on error.
    """
    resolved_cwd = cwd or Path.cwd()
    rdr = renderer or ApplyRenderer(yes=skip_confirm, verbosity=verbosity)

    # Materialize jd_text into a temp file once. The orchestrator agent
    # reads the file inside the gather phase and inlines its contents
    # into spec.json. Cleanup is best-effort (temp dir is OS-managed).
    jd_text_file: Path | None = None
    if jd_text is not None and jd_text.strip():
        import tempfile as _tempfile
        _fd, _tmp_path = _tempfile.mkstemp(
            prefix="jobsmith-jdtext-",
            suffix=".txt",
        )
        import os as _os
        with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(jd_text)
        jd_text_file = Path(_tmp_path)

    # Step 1: ensure bootstrap
    try:
        ensure_bootstrap(resolved_cwd)
    except Exception as exc:
        rdr.print_error(f"Bootstrap failed: {exc}")
        return 1

    # Step 1b: seed master_content from disk YAMLs if not already loaded
    # (roborev job 958 HIGH + job 959 MEDIUM). The FastAPI ``api serve``
    # lifespan handler does this at startup, but a direct ``jobsmith
    # apply`` invocation never seeded the table — Pass 3 specialists
    # then crash with ``no master_content row`` when they try to
    # ``jobsmith db dump-master --section work`` because the rows were
    # never inserted. Idempotent: skips when rows already exist.
    #
    # ``repo_root`` MUST be the project root (the directory holding
    # ``.apply-config.yaml``), not the supervisor's ``cwd``. When
    # ``jobsmith apply`` is invoked from a subdirectory, ``cwd != root``
    # and ``ensure_master_loaded`` would resolve ``master.work_yml``
    # against the wrong base, find nothing, and seed zero rows.
    try:
        _config_path = find_config(resolved_cwd)
        _db_path = _pipeline_db_path(resolved_cwd)
        if _config_path is not None and _db_path is not None:
            from .master_ingest import ensure_master_loaded as _ensure_master

            _ensure_master(_db_path, repo_root=_config_path.parent)
    except Exception as exc:  # noqa: BLE001 — degrade rather than abort.
        logger.warning("master_content seed failed: %s", exc)

    # Step 2: resolve starting slug. With --force, bypass the URL index and
    # use the URL-derived slug (a fresh run). Otherwise look up the URL in
    # the persisted index, falling back to a one-time migration scan, and
    # finally to the URL-derived slug.
    import time

    started_at = time.time()
    plugin_directory = get_plugin_dir()

    if slug is not None:
        # Caller (typically the API) supplied an explicit slug — honor it
        # without touching the URL→slug index. The supervisor tracks runs
        # under this slug, so files must be written under it too.
        from_index = False
        if force:
            rdr.print_force_banner()
    elif force:
        # --force restarts the pipeline but must still target the existing
        # canonical directory if we know about it; otherwise phase 1 writes
        # under the URL slug and the post-phase-1 reconcile would refuse to
        # merge into the non-empty canonical dir, leaving us in a broken
        # state that corrupts the URL index. Consult the persisted index
        # first; fall back to URL-derived slug only when the URL is unknown.
        index = _load_url_index(resolved_cwd)
        if url in index:
            slug = index[url]
            from_index = True
        else:
            slug = derive_slug(url)
            from_index = False
        rdr.print_force_banner()
    else:
        slug, from_index = _resolve_starting_slug(url, resolved_cwd)

    # Step 3: phase-completion gating. Read manifest at the resolved app dir
    # (when not --force) and decide which phases to skip. Manifest absence,
    # malformed JSON, or missing invocations all fall through to "rerun".
    apps_dir = _applications_dir(resolved_cwd)
    app_dir = apps_dir / slug if apps_dir is not None else None
    manifest = None if force or app_dir is None else _load_manifest(app_dir, resolved_cwd)

    # Compute (or create) the session ID from the persisted per-application
    # file.  This replaces the old uuid5-based deterministic_session_id so
    # that a retry after a failed gather gets a fresh ID the Claude Code SDK
    # will accept.  When there is no config (app_dir is None) fall back to
    # the deterministic ID so the no-config path is unchanged.
    #
    # Finding 2 fix: when --force is set, the entire pipeline reruns from
    # gather.  The persisted session-id may point to a previous successful
    # run whose JSONL still exists in ~/.claude/projects/ — the SDK would
    # reject it with "Session ID already in use".  Unlink it here so
    # _get_or_create_session_id always mints a fresh uuid4 on a forced run.
    if force and app_dir is not None:
        _session_id_file = app_dir / ".apply-state" / "session-id"
        _session_id_file.unlink(missing_ok=True)
        # Roborev job 958 HIGH: ``--force`` bypassed the manifest gate
        # (so phases re-run) but left every Pass-3 result envelope —
        # ``apply-fit-scorer-result``, ``apply-bullet-selector-result``,
        # etc. — sitting in apply_state. The phase-1 prompt's resume
        # rule treats a prior ``status: ok`` envelope as "skip — already
        # complete," so a forced re-run would silently reuse stale
        # outputs. Wipe both apply_state and apply_state_log for the
        # slug before any agent dispatch.
        _force_db_path = _pipeline_db_path(resolved_cwd)
        if _force_db_path is not None and _force_db_path.exists():
            with contextlib.suppress(Exception):
                from .db import open_pipeline_db as _open_pipe_db
                from .db import reset_state as _reset_state

                _conn = _open_pipe_db(_force_db_path)
                try:
                    _reset_state(_conn, slug=slug)
                finally:
                    _conn.close()
    session_id = (
        _get_or_create_session_id(app_dir, resolved_cwd)
        if app_dir is not None
        else headless.deterministic_session_id(slug)
    )

    rdr.print_info(f"jobsmith apply: slug={slug!r}  session={session_id}")

    phase_done: dict[str, bool] = {
        name: _phase_completed(manifest, name) for name, _ in _PHASES
    }

    # All phases done → print summary and exit cleanly unless --force.
    if all(phase_done.values()) and app_dir is not None:
        rdr.print_already_complete(app_dir)
        return 0

    # Resume banner above the first phase that will actually run, when
    # phases earlier than that one are being skipped.
    first_to_run = next(name for name, _ in _PHASES if not phase_done[name])
    if first_to_run != "gather":
        first_phase_num = next(
            num for name, num in _PHASES if name == first_to_run
        )
        rdr.print_resume_banner(slug, first_phase_num, first_to_run)

    total_phases = len(_PHASES)

    # roborev #923 HIGH 2: persist apply_runs row + post-phase ingest from the
    # CLI path too. Previously only the marimo runner wrote to the DB, so
    # `jobsmith apply <url>` followed by `jobsmith review <slug>` would fail
    # with "slug not found". Mirror the marimo runner's pattern: insert one
    # apply_runs row per CLI run, ingest after each phase_complete, finalize
    # the row's status in the wrapper finally-block. Wrapped in suppress so a
    # missing/locked DB never aborts the apply pipeline itself — DB writes are
    # canonical for review but secondary to the pipeline's primary work.
    # Roborev job 955 HIGH: when the API supervisor launches this
    # subprocess it generates its own run_id (used to filter the
    # apply_state_log tailer). If the subprocess minted a different
    # uuid4, the supervisor's tailer would see zero rows. Accept the
    # caller-supplied run_id and only fall back to a fresh uuid4 when
    # invoked directly from a terminal.
    db_run_id = run_id or str(uuid.uuid4())
    db_phase_label = "unknown"  # full pipeline; matches marimo runner convention
    db_started_at_iso = _db_now_iso()
    db_conn = _open_pipeline_db_for_run(resolved_cwd)
    db_final_status = "failed"  # default; overridden on success/decline/etc.
    db_slug_ref = [slug]
    # Roborev job 967 MEDIUM: distinguish user-declined partial stops
    # ("Stopped at user request" — rc=0 but should not be marked
    # "done") from full pipeline success. The phase loop sets this to
    # ``"cancelled"`` before returning 0 from a confirm-gate decline.
    db_status_ref = ["unset"]
    if db_conn is not None:
        with contextlib.suppress(Exception):
            from .db import insert_apply_run as _insert_apply_run

            _insert_apply_run(
                db_conn,
                run_id=db_run_id,
                slug=slug,
                phase=db_phase_label,
                started_at=db_started_at_iso,
                finished_at=None,
                status="running",
            )

    try:
        rc = _run_apply_phases(
            url=url,
            resolved_cwd=resolved_cwd,
            rdr=rdr,
            plugin_directory=plugin_directory,
            slug=slug,
            apps_dir=apps_dir,
            session_id=session_id,
            phase_done=phase_done,
            total_phases=total_phases,
            skip_confirm=skip_confirm,
            started_at=started_at,
            db_conn=db_conn,
            db_run_id=db_run_id,
            db_slug_ref=db_slug_ref,
            db_status_ref=db_status_ref,
            jd_text_file=jd_text_file,
        )
        if rc != 0:
            db_final_status = "failed"
        elif db_status_ref[0] == "cancelled":
            db_final_status = "cancelled"
        else:
            db_final_status = "done"
        return rc
    finally:
        # Best-effort cleanup of the jd_text temp file. The user-supplied
        # JD body may include sensitive content (compensation expectations,
        # named hiring managers from private channels) so unlink it as
        # soon as the pipeline no longer needs it (roborev #928 LOW).
        if jd_text_file is not None:
            with contextlib.suppress(OSError):
                jd_text_file.unlink()
        if db_conn is not None:
            try:
                # db_slug_ref[0] reflects the canonical slug after gather
                # reconciliation (run_apply_phases mutates it in place); use
                # that for the final UPDATE so the apply_runs row points at the
                # actual application directory.
                with contextlib.suppress(Exception):
                    db_conn.execute(
                        "UPDATE apply_runs "
                        "SET status=?, finished_at=?, slug=? "
                        "WHERE run_id=?",
                        (
                            db_final_status,
                            _db_now_iso(),
                            db_slug_ref[0],
                            db_run_id,
                        ),
                    )
                    db_conn.commit()
            finally:
                db_conn.close()


def _db_now_iso() -> str:
    """ISO-8601 UTC timestamp; matches marimo runner's apply_runs format."""
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()


def _open_pipeline_db_for_run(cwd: Path):
    """Open the pipeline DB if config is present; otherwise return None.

    Returns None silently when ``.apply-config.yaml`` is missing — the apply
    pipeline must keep working in scratch directories without a config (the
    bootstrap path will create one, but unit tests stub a minimal config that
    still resolves a default ``private/jobsmith.db`` path beneath ``cwd``).
    """
    config_path = find_config(cwd)
    if config_path is None:
        return None
    try:
        from .db import open_pipeline_db as _open_pipeline_db
        config = load_config(config_path)
        db_path = resolve(config.output.jobsmith_db, config_path.parent)
        return _open_pipeline_db(db_path)
    except Exception:  # noqa: BLE001 — DB is secondary to the pipeline
        return None


def _run_apply_phases(
    *,
    url: str,
    resolved_cwd: Path,
    rdr: ApplyRenderer,
    plugin_directory: Path,
    slug: str,
    apps_dir: Path | None,
    session_id: str,
    phase_done: dict[str, bool],
    total_phases: int,
    skip_confirm: bool,
    started_at: float,
    db_conn,
    db_run_id: str,
    db_slug_ref: list[str],
    db_status_ref: list[str],
    jd_text_file: Path | None = None,
) -> int:
    """Phase-loop body extracted so the surrounding ``run_apply`` can wrap it
    with the apply_runs DB lifecycle (insert before, UPDATE after, with the
    canonical slug reflected via ``db_slug_ref[0]``).

    ``db_status_ref`` (single-element list) is mutated to ``"cancelled"``
    when the user declines an inter-phase confirm gate, distinguishing
    that case from a full pipeline completion (``run_apply``'s finally
    maps ``rc=0 + status_ref="cancelled"`` to apply_runs.status=cancelled
    and ``rc=0 + status_ref!="cancelled"`` to status=done).

    All parameters are pre-resolved by ``run_apply``; this helper performs no
    bootstrap or slug resolution of its own.
    """
    # Auto-freeze specialist contracts on first apply (feat-385f3405).
    # Must run before the gather phase so the agent sees frozen_at non-null.
    _auto_freeze_contracts(
        plugin_directory / "agents" / "apply" / "specialist-contracts.yaml"
    )

    for phase_name, phase_num in _PHASES:
        # Step 3pre: just-in-time wrapper-owned prerequisites that must run
        # regardless of whether the prior phase ran or was skipped.  Step 4/5
        # produces ``bullet-decisions.json`` (a wrapper artifact, not a
        # specialist artifact); a prior run can mark all gather specialists
        # as ``ok`` but stop at the anchor guard, so we re-run step 4/5
        # before draft any time it is missing.
        if phase_name == "draft" and not phase_done["draft"]:
            state_dir = _apply_state_dir(slug, resolved_cwd)
            if state_dir is not None and not (state_dir / "bullet-decisions.json").exists():
                rc = _run_step45_orchestration(slug, resolved_cwd)
                if rc != 0:
                    return rc

        # Skip phases already marked complete in manifest.json.
        if phase_done[phase_name]:
            rdr.print_phase_skipped(phase_num, phase_name)
            continue

        # Finding 1 fix (Option A): each non-gather phase gets its own fresh
        # session at the phase boundary.  Deleting the persisted session-id
        # forces _get_or_create_session_id to mint a new uuid4.  Because the
        # new JSONL does not yet exist in ~/.claude/projects/, session_exists()
        # returns False → resume stays False → run_phase uses --session-id
        # (not --resume), giving every phase a clean turn budget.
        # For draft this runs after Step 3g has already reconciled the slug,
        # so the new ID is minted under the correct (canonical) app_dir.
        if phase_name != "gather" and apps_dir is not None:
            (apps_dir / slug / ".apply-state" / "session-id").unlink(
                missing_ok=True
            )
            session_id = _get_or_create_session_id(apps_dir / slug, resolved_cwd)

        # Step 3a: determine resume flag (Claude session continuity).
        # Invariant: session_id was freshly minted just above for phase 2/3,
        # so its JSONL does not yet exist in ~/.claude/projects/…
        # session_exists() therefore returns False, resume stays False, and
        # _build_command uses --session-id (claim a new session) rather than
        # --resume.  This gives each phase a clean turn budget and avoids
        # inheriting any prior phase's spent turns.
        resume = (phase_name != "gather") and headless.session_exists(
            session_id, cwd=resolved_cwd
        )

        # Step 3b: resolve system prompt path
        system_prompt = plugin_directory / "system-prompts" / f"phase-{phase_num}-{phase_name}.md"
        if not system_prompt.exists():
            rdr.print_error(f"ERROR: system prompt not found: {system_prompt}")
            return 1

        # Step 3c: build paths dict for this phase (uses current slug — after reconcile
        # for draft/render, slug may have changed from the URL-derived value).
        phase_paths = _build_paths(slug, resolved_cwd, plugin_directory)

        # Step 3d: build prompt text. jd_text_file is gather-only; pass
        # None for other phases so the kwarg propagates cleanly.
        prompt_text = build_phase_prompt(
            phase_name,
            slug,
            url,
            paths=phase_paths,
            jd_text_file=jd_text_file if phase_name == "gather" else None,
        )

        # Step 3e: render phase header and start spinner
        rdr.print_header(phase_num, total_phases, phase_name)
        rdr.start_phase(phase_name)

        # Step 3e2: open transcript for this phase (always, regardless of verbosity)
        state_dir_for_transcript = _apply_state_dir(slug, resolved_cwd)
        if state_dir_for_transcript is not None:
            transcript_path = state_dir_for_transcript / "transcript.jsonl"
            # trk-60217f9f Pass 4: dual-write to apply_state_log so the
            # supervisor can tail by row id. Disk file remains canonical
            # until Pass 5.
            rdr.open_transcript(
                transcript_path,
                phase_name,
                slug=slug,
                db_path=_pipeline_db_path(resolved_cwd),
                run_id=db_run_id,
            )

        # Step 3f: stream events
        phase_succeeded = False
        try:
            for event in headless.run_phase(
                phase=phase_name,
                session_id=session_id,
                prompt=prompt_text,
                plugin_dir=plugin_directory,
                system_prompt=system_prompt,
                resume=resume,
                cwd=resolved_cwd,
                max_turns=_PHASE_MAX_TURNS[phase_name],
            ):
                rdr.render_event(event)

                if event.type == "phase_complete":
                    phase_succeeded = True
                    break

                if event.type == "phase_failed":
                    rdr.close_transcript()
                    rdr.print_error(
                        "Aborting before subsequent phases. "
                        "If the error mentions contracts not frozen, run: "
                        "jobsmith doctor  (or set frozen_at in specialist-contracts.yaml)"
                    )
                    return 3

                if event.type == "error":
                    rdr.stop_phase()
                    rdr.close_transcript()
                    rdr.print_error(f"Phase {phase_name} encountered an error. Aborting.")
                    return 2
        except Exception as exc:
            rdr.stop_phase()
            rdr.close_transcript()
            rdr.print_error(f"Unexpected error in phase {phase_name}: {exc}")
            return 2

        rdr.close_transcript()

        if not phase_succeeded:
            rdr.stop_phase()
            rdr.print_error(
                f"Phase {phase_name} did not emit a phase_complete signal. "
                "Check output above for errors."
            )
            return 2

        # Step 3f-snap: snapshot agent drafts so `jobsmith feedback record`
        # has a stable baseline to diff user edits against. Done immediately
        # after the phase succeeds, before the user can edit anything.
        _snapshot_phase_drafts(phase_name, slug, resolved_cwd)

        # Step 3f-dual-write (feat-e3d87579): mirror FS artifacts to the DB.
        # Phase 1 dual-write — FS remains authoritative; PUT failures log a
        # WARNING but never fail the phase. Skipped when JOBSMITH_DUAL_WRITE=0.
        client = _build_client_if_enabled()
        if client is not None:
            phase_state_dir = _apply_state_dir(slug, resolved_cwd)
            if phase_state_dir is not None:
                try:
                    dual_write_phase_artifacts(
                        client=client,
                        slug=slug,
                        run_id=db_run_id,
                        state_dir=phase_state_dir,
                    )
                except Exception as exc:  # noqa: BLE001 — never fail phase
                    logger.warning("dual-write hook failed: %s", exc)

        # Step 3f-snapshot (feat-60be8c3a): before the render phase invokes
        # quarto, materialise DB-only artifacts back to FS so quarto can read
        # _quarto.yml / _variables.yml / prose-draft.md / cover-letter-draft.md.
        # Once specialists drop FS writes (follow-up), this is the only path
        # that puts those files on disk for the render step.
        if phase_name == "render" and client is not None:
            try:
                client.snapshot_run(slug, db_run_id)
            except Exception as exc:  # noqa: BLE001 — never fail render
                logger.warning(
                    "render-phase snapshot failed (FS may be stale): %s", exc
                )

        # Step 3g: between-phase orchestration. After gather we reconcile the
        # canonical slug (phase 1 may have written artifacts under a different
        # directory than the URL-derived slug), and recompute session_id
        # (Option A: phase 2/3 run under a fresh session keyed on the
        # canonical slug — phase prompts read .apply-state/* directly so
        # conversation continuity is not required).  The URL → slug mapping
        # is persisted only when reconciliation actually produced a canonical
        # slug; otherwise we'd corrupt the index by recording a fallback
        # (URL-derived) slug as if it were canonical.  Step 4/5 runs before
        # phase 2 (in Step 3pre above) regardless of how gather got "done".
        if phase_name == "gather":
            pre_reconcile_slug = slug
            new_slug, reconciled = _reconcile_canonical_slug(
                slug, resolved_cwd, started_at
            )
            if new_slug != slug:
                slug = new_slug
                # When reconcile renames the directory the session-id file
                # is copied verbatim into the new location.  Remove it now so
                # the per-phase fresh-session block at the top of each
                # non-gather iteration (Finding 1 fix) generates a clean
                # uuid4 rather than re-using the carried-over value.
                if apps_dir is not None:
                    carried_session_file = (
                        apps_dir / slug / ".apply-state" / "session-id"
                    )
                    carried_session_file.unlink(missing_ok=True)
            if reconciled:
                _record_url_mapping(url, slug, resolved_cwd)
            # Propagate the canonical slug to the outer apply_runs row so the
            # final UPDATE in run_apply's finally-block records the correct
            # application directory (roborev #923 HIGH 2).
            db_slug_ref[0] = slug

            # Roborev job 956 MEDIUM: catch any apply_state_log row the
            # renderer wrote between the orchestrator's mid-phase
            # ``rekey-slug`` and the gather event-loop close (the
            # renderer's ``_transcript_slug`` is captured at
            # ``open_transcript`` time and is the URL-derived starting
            # slug for phase 1; transcript rows after rekey continue
            # tagging that slug). Re-rekey idempotently from the old
            # slug to the canonical one so apply_state and apply_state_log
            # are fully consolidated under a single slug per run; subsequent
            # ``jobsmith db list-state --slug`` and ``reset-state --slug``
            # operations cover everything.
            if (
                reconciled
                and pre_reconcile_slug != slug
                and db_conn is not None
            ):
                with contextlib.suppress(Exception):
                    from .db import rekey_slug as _rekey_slug

                    _rekey_slug(
                        db_conn, from_slug=pre_reconcile_slug, to_slug=slug
                    )

            # Render per-phase summary panel before the confirm gate
            state_dir = _apply_state_dir(slug, resolved_cwd)
            if state_dir is not None:
                rdr.render_phase_summary("gather", state_dir)

        # Post-phase ingest into specialist_outputs. Mirrors the marimo
        # runner's behavior so `jobsmith review <slug>` sees rows immediately
        # after `jobsmith apply <url>` (roborev #923 HIGH 2). Wrapped in
        # suppress: a single broken artifact must not abort the pipeline.
        # Also runs ingest_standalone_artifacts so the 4 orphaned kinds
        # (cover-letter-draft, _quarto.yml, _variables.yml, .agent.md) land
        # in the DB on live runs — without this, JOBSMITH_DUAL_WRITE=0
        # (S4 default) leaves them invisible until manual backfill.
        # Closes roborev branch-review HIGH (feat-b1a883a1).
        if db_conn is not None:
            with contextlib.suppress(Exception):
                from .db_ingest import (
                    ingest_phase_outputs as _ingest_phase_outputs,
                )
                from .db_ingest import (
                    ingest_standalone_artifacts as _ingest_standalone_artifacts,
                )

                state_dir_for_ingest = _apply_state_dir(slug, resolved_cwd)
                if state_dir_for_ingest is not None:
                    _ingest_phase_outputs(
                        db_conn,
                        slug=slug,
                        run_id=db_run_id,
                        phase=phase_name,
                        state_dir=state_dir_for_ingest,
                    )
                    _ingest_standalone_artifacts(
                        db_conn,
                        run_id=db_run_id,
                        state_dir=state_dir_for_ingest,
                    )

        # Step 3h: confirm gate (not after the last phase, and not after a
        # phase that was skipped — only fresh-run phases prompt).
        if not skip_confirm and phase_name != "render":
            rdr.pause_before_confirm()
            if not click.confirm(f"Phase {phase_num} ({phase_name}) complete. Proceed to next phase?"):
                rdr.print_info("Stopped at user request. Partial work saved.")
                # Roborev job 967 MEDIUM: signal user-decline to the
                # outer ``run_apply`` so apply_runs.status is recorded
                # as "cancelled" rather than "done". rc stays 0
                # because the user's choice is not a CLI failure.
                db_status_ref[0] = "cancelled"
                return 0

    rdr.print_complete()
    return 0
