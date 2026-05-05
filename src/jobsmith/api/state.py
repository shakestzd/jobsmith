"""DB-only application state derivation (S3 of trk-144d42b1).

``derive_application_state(slug)`` queries the pipeline DB exclusively to
determine the phase and status of an application.  When required artifacts
are missing from the DB, callers should treat that as a backfill gap to be
surfaced via ``jobsmith db backfill --slug <slug>`` rather than silently
reading from the filesystem.

Phase derivation rules (DB only)
--------------------------------
0 (queued):  no apply_runs row, OR run.status = 'queued'
1 (gather):  specialist_outputs has kind 'jd-parsed' or 'bullet-selection'
2 (draft):   specialist_outputs has kind 'prose-draft'
3 (render):  specialist_outputs has kind 'cover-letter-draft'

History
-------
0.8 shipped FS-fallback behind ``JOBSMITH_FS_FALLBACK=1``.  0.8.1 removed it
(closes #63) — the silent fallback masked backfill gaps.  Callers that hit a
missing kind should now see DB-only state and decide explicitly whether to
backfill.
"""
from __future__ import annotations

import logging

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


def _derive_phase_from_kinds(present_kinds: set[str]) -> int:
    """Return the highest phase number whose trigger kind is present."""
    for kind, phase in _KIND_PHASE_MAP:
        if kind in present_kinds:
            return phase
    return 0


def derive_application_state(slug: str) -> dict:
    """Derive phase and status for *slug* from the pipeline DB.

    Returns a dict with keys: slug, run_id, phase, status.
    When no apply_runs row exists, returns phase=0, status='queued'.
    """
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

        if db_status == "queued":
            return {"slug": slug, "run_id": run_id, "phase": 0, "status": db_status}

        kind_rows = conn.execute(
            "SELECT DISTINCT kind FROM specialist_outputs WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    present_kinds: set[str] = {r["kind"] for r in kind_rows}
    phase = _derive_phase_from_kinds(present_kinds)
    return {"slug": slug, "run_id": run_id, "phase": phase, "status": db_status}


__all__ = ["derive_application_state"]
