"""DB-first application state derivation.

``derive_application_state(slug)`` queries the pipeline DB to determine the
phase and status of an application, falling back to the filesystem only when
``JOBSMITH_FS_FALLBACK=1`` (default ON during slice 8).

Phase derivation rules (DB-based)
----------------------------------
0 (queued):  no apply_runs row, OR run.status = 'queued'
1 (gather):  specialist_outputs has kind 'jd-parsed' or 'bullet-selection'
2 (draft):   specialist_outputs has kind 'prose-draft'
3 (render):  specialist_outputs has kind 'cover-letter-draft'

Status is pulled from apply_runs.status when a row exists.

FS fallback
-----------
When ``JOBSMITH_FS_FALLBACK=1`` (default) and a kind is absent from the DB,
``_fs_fallback_load(state_dir, kind)`` is called and a WARNING is logged with
slug + kind.  Slice 9 will remove FS entirely.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from jobsmith.api.artifacts import _get_db_path
from jobsmith.db import open_pipeline_db

_log = logging.getLogger(__name__)

# Ordered list of (kind, phase_number) — first match wins.
# Evaluated from highest phase down so the highest present kind wins.
_KIND_PHASE_MAP: list[tuple[str, int]] = [
    ("cover-letter-draft", 3),
    ("prose-draft", 2),
    ("jd-parsed", 1),
    ("bullet-selection", 1),
]

# Kinds to probe during FS fallback (checked in phase-descending order).
_FS_PROBE_KINDS: tuple[str, ...] = (
    "cover-letter-draft",
    "prose-draft",
    "jd-parsed",
    "bullet-selection",
)


def _fs_fallback_load(state_dir: Path | None, kind: str) -> str | None:
    """Attempt to read a kind from the filesystem.

    Returns the raw text content when found, None otherwise.
    This function is module-level so tests can monkeypatch it.
    ``state_dir`` may be None when the application directory cannot be resolved.
    """
    if state_dir is None:
        return None

    filename_map = {
        "jd-parsed": "jd-parsed.json",
        "bullet-selection": "bullet-selection.json",
        "prose-draft": "prose-draft.md",
        # cover-letter-draft lives at app root, not inside .apply-state/
        "cover-letter-draft": None,
    }
    filename = filename_map.get(kind)
    if filename is None:
        # cover-letter-draft is at state_dir.parent; or kind not mapped
        if kind == "cover-letter-draft":
            path = state_dir.parent / "cover-letter-draft.md"
        else:
            return None
    else:
        path = state_dir / filename

    if path.exists():
        return path.read_text()
    return None


def _derive_phase_from_kinds(present_kinds: set[str]) -> int:
    """Return the highest phase number whose trigger kind is present."""
    for kind, phase in _KIND_PHASE_MAP:
        if kind in present_kinds:
            return phase
    return 0


def derive_application_state(slug: str) -> dict:
    """Derive phase and status for *slug* using the pipeline DB as primary source.

    Returns a dict with keys: slug, run_id, phase, status.
    When no apply_runs row exists, returns phase=0, status='queued'.

    Parameters
    ----------
    slug:
        The application slug (e.g. ``'acme-swe'``).

    Returns
    -------
    dict with keys:
        slug (str), run_id (str | None), phase (int), status (str)
    """
    fs_fallback_enabled = os.environ.get("JOBSMITH_FS_FALLBACK", "1") == "1"

    db_path = _get_db_path()
    try:
        conn = open_pipeline_db(db_path)
    except Exception:
        _log.exception("Cannot open pipeline DB for state derivation of %r", slug)
        return {"slug": slug, "run_id": None, "phase": 0, "status": "queued"}

    try:
        run_row = conn.execute(
            "SELECT * FROM apply_runs WHERE slug = ? ORDER BY started_at DESC LIMIT 1",
            (slug,),
        ).fetchone()

        if run_row is None:
            return {"slug": slug, "run_id": None, "phase": 0, "status": "queued"}

        run_id: str = run_row["run_id"]
        db_status: str = run_row["status"]

        # Phase 0 forced when status is 'queued'
        if db_status == "queued":
            return {"slug": slug, "run_id": run_id, "phase": 0, "status": db_status}

        # Fetch all output kinds for this run
        kind_rows = conn.execute(
            "SELECT DISTINCT kind FROM specialist_outputs WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    present_kinds: set[str] = {r["kind"] for r in kind_rows}

    # FS fallback: for each probe kind absent from DB, attempt FS read
    if fs_fallback_enabled:
        # Resolve state_dir lazily — we don't fail hard if it's missing
        state_dir = _resolve_state_dir(slug, db_path)
        for kind in _FS_PROBE_KINDS:
            if kind not in present_kinds:
                _log.warning(
                    "FS fallback: kind %r absent from DB for slug %r — reading filesystem",
                    kind,
                    slug,
                )
                value = _fs_fallback_load(state_dir, kind)
                if value is not None:
                    present_kinds.add(kind)

    phase = _derive_phase_from_kinds(present_kinds)
    return {"slug": slug, "run_id": run_id, "phase": phase, "status": db_status}


def _resolve_state_dir(slug: str, db_path: Path) -> Path | None:
    """Best-effort resolution of the .apply-state/ directory for slug.

    Returns None when the directory cannot be found. This is non-fatal;
    callers degrade gracefully.
    """
    # The convention is <applications_dir>/<slug>/.apply-state/
    # applications_dir defaults to db_path.parent.parent (project root)
    app_dir = db_path.parent.parent / slug
    state_dir = app_dir / ".apply-state"
    if state_dir.is_dir():
        return state_dir
    return None


__all__ = ["derive_application_state"]
