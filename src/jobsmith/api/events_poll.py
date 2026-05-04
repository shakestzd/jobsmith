"""DB polling helpers for the SSE events stream.

Extracted from ``events.py`` to keep that module focused on the FastAPI
router + stream generator. These helpers operate on a borrowed sqlite3
connection (or open their own in :func:`_db_poll_once`) and are pure
data-access functions — no FastAPI / SSE concerns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jobsmith.db import open_pipeline_db


def _max_rowid(conn: sqlite3.Connection, table: str) -> int:
    """Return the current MAX(rowid) for ``table`` (0 when empty)."""
    row = conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _fetch_new_runs(
    conn: sqlite3.Connection, slug: str, after_rowid: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT rowid AS rowid, run_id, slug, phase, status,
               started_at, finished_at
        FROM apply_runs
        WHERE slug = ? AND rowid > ?
        ORDER BY rowid ASC
        LIMIT 50
        """,
        (slug, after_rowid),
    ).fetchall()


def _fetch_new_specialists(
    conn: sqlite3.Connection, slug: str, after_rowid: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT so.rowid AS rowid, so.run_id, so.specialist, so.kind,
               so.finished_at, so.transcript_ref,
               so.version,
               ar.phase AS phase, ar.status AS status
        FROM specialist_outputs so
        JOIN apply_runs ar ON ar.run_id = so.run_id
        WHERE ar.slug = ? AND so.rowid > ?
        ORDER BY so.rowid ASC
        LIMIT 50
        """,
        (slug, after_rowid),
    ).fetchall()


def _fetch_current_run_statuses(
    conn: sqlite3.Connection, slug: str
) -> list[sqlite3.Row]:
    """Return the current ``(run_id, phase, status, finished_at)`` snapshot.

    Used to detect terminal state transitions: ``apply_runs`` rows are UPDATED
    in place when a run completes (rowid stays the same), so a rowid-only poll
    never sees the transition. Caller compares against a per-stream snapshot
    and emits a ``phase`` event on any change.
    """
    return conn.execute(
        """
        SELECT rowid AS rowid, run_id, phase, status,
               started_at, finished_at
        FROM apply_runs
        WHERE slug = ?
        ORDER BY rowid DESC
        LIMIT 50
        """,
        (slug,),
    ).fetchall()


def _db_poll_once(
    db_path: Path,
    slug: str,
    after_run_rowid: int,
    after_specialist_rowid: int,
) -> tuple[
    list[sqlite3.Row],
    list[sqlite3.Row],
    list[sqlite3.Row],
    int,
    int,
]:
    """Open a fresh connection, run all three queries, close.

    Returning everything from one ``asyncio.to_thread`` worker avoids the
    cross-thread connection issue that arises when the same ``sqlite3.Connection``
    is touched from multiple thread-pool workers (default ``check_same_thread=True``
    rejects this).

    Returns: (new_runs, new_specialists, current_run_snapshot,
              max_run_rowid, max_specialist_rowid).
    """
    conn = open_pipeline_db(db_path)
    try:
        if after_run_rowid < 0:
            after_run_rowid = _max_rowid(conn, "apply_runs")
        if after_specialist_rowid < 0:
            after_specialist_rowid = _max_rowid(conn, "specialist_outputs")
        new_runs = _fetch_new_runs(conn, slug, after_run_rowid)
        new_specs = _fetch_new_specialists(conn, slug, after_specialist_rowid)
        current = _fetch_current_run_statuses(conn, slug)
    finally:
        conn.close()
    return new_runs, new_specs, current, after_run_rowid, after_specialist_rowid
