"""Amendment persistence helpers for the jobsmith review notebook.

All functions reuse :mod:`jobsmith.db` helpers — no duplicate SQL.
Per-slug review DBs live in ``review_db_dir/<slug>.db``.

Public API
----------
persist_amendment  — dedup-by-content insert; returns stored amendment_id.
set_status         — UPDATE status for a single amendment.
archive_pending_for_run — flip older pending amendments to 'stale' when a
                          fresh apply run starts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jobsmith.db import insert_amendment, open_review_db
from jobsmith.marimo.directive_parser import Amendment


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def persist_amendment(
    slug: str,
    amendment: Amendment,
    review_db_dir: Path,
    *,
    run_id: str | None = None,
) -> str:
    """Insert *amendment* into the per-slug review DB, deduplicating by content.

    Dedup key: ``(slug, section, op, value, status='pending')``.
    If a matching pending row already exists, its ``amendment_id`` is returned
    and no new row is inserted.

    Parameters
    ----------
    slug:
        Application slug (determines the DB file).
    amendment:
        Parsed :class:`~jobsmith.marimo.directive_parser.Amendment` to persist.
    review_db_dir:
        Parent directory for per-slug review DBs.
    run_id:
        Optional run UUID to link this amendment to the triggering apply run.

    Returns
    -------
    str
        The ``amendment_id`` (UUID4) of the stored row (existing or newly inserted).
    """
    conn = open_review_db(slug, review_db_dir)
    try:
        # Dedup by content (slug + section + op + value) regardless of status.
        # Earlier code only matched status='pending', which let the same
        # directive be re-inserted as a fresh pending row after the user
        # accepted or rejected it (roborev #920 MEDIUM). Stale amendments
        # are still re-inserted because they belong to a prior run cycle.
        row = conn.execute(
            "SELECT amendment_id FROM amendments "
            "WHERE slug=? AND section=? AND op=? AND value=? "
            "AND status IN ('pending','accepted','rejected')",
            (slug, amendment.section, amendment.op, amendment.value),
        ).fetchone()

        if row is not None:
            return str(row["amendment_id"])

        # No duplicate found — insert the new amendment
        insert_amendment(
            conn,
            amendment_id=amendment.id,
            slug=slug,
            run_id=run_id,
            section=amendment.section,
            op=amendment.op,
            value=amendment.value,
            status="pending",
            created_at=_now_iso(),
        )
        return amendment.id
    finally:
        conn.close()


def set_status(
    slug: str,
    amendment_id: str,
    status: str,
    review_db_dir: Path,
) -> None:
    """Update the ``status`` column for a single amendment.

    Parameters
    ----------
    slug:
        Application slug (determines the DB file).
    amendment_id:
        UUID4 primary key of the amendment to update.
    status:
        New status value (e.g. ``'accepted'``, ``'rejected'``, ``'stale'``).
    review_db_dir:
        Parent directory for per-slug review DBs.
    """
    conn = open_review_db(slug, review_db_dir)
    try:
        conn.execute(
            "UPDATE amendments SET status=? WHERE amendment_id=? AND slug=?",
            (status, amendment_id, slug),
        )
        conn.commit()
    finally:
        conn.close()


def archive_pending_for_run(
    slug: str,
    current_run_id: str,
    review_db_dir: Path,
) -> None:
    """Flip all pending amendments that do NOT belong to *current_run_id* to 'stale'.

    Called at the start of a fresh full apply run so that prior pending
    amendments (which targeted content from an older run) are visually
    distinguished in the sidebar.

    Amendments with ``run_id IS NULL`` are also archived because they were
    created before any run was associated and are now superseded.

    Parameters
    ----------
    slug:
        Application slug (determines the DB file).
    current_run_id:
        The UUID of the run just started.
    review_db_dir:
        Parent directory for per-slug review DBs.
    """
    conn = open_review_db(slug, review_db_dir)
    try:
        conn.execute(
            "UPDATE amendments SET status='stale' "
            "WHERE slug=? AND status='pending' AND (run_id IS NULL OR run_id != ?)",
            (slug, current_run_id),
        )
        conn.commit()
    finally:
        conn.close()


__all__ = ["archive_pending_for_run", "persist_amendment", "set_status"]
