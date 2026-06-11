"""jobsmith.sourcing.store — typed read/write helpers for postings and sourcing_runs.

Tables (created by migration 010_postings):
  postings        — one row per unique job posting, keyed by dedup_key
  sourcing_runs   — one row per crawl/ingest cycle (retain last 90 by default)

Design decisions
----------------
- upsert_posting: INSERT OR IGNORE + separate UPDATE of last_seen_at only.
  This preserves all original columns (including status) for re-sighted rows,
  satisfying the "dismissed/promoted/expired are NEVER resurrected" rule.
- promote_posting: creates an apply_runs row with status='in-progress', links
  promoted_application_id, and sets posting status=promoted. Idempotent:
  a second call returns the existing run_id without creating a new row.
- sourcing_runs: upsert on INSERT OR IGNORE; finish updates counts + timestamps.
- purge_old_sourcing_runs: deletes rows beyond the last *keep* by started_at ASC.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POSTING_STATUSES: frozenset[str] = frozenset(
    {"sourced", "queued", "dismissed", "promoted", "expired"}
)

_DEFAULT_KEEP_RUNS: int = 90


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _slugify(text: str) -> str:
    """Convert *text* to a lowercase hyphen-separated slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


# ---------------------------------------------------------------------------
# postings — upsert / read
# ---------------------------------------------------------------------------


def upsert_posting(
    conn: sqlite3.Connection,
    *,
    source: str,
    dedup_key: str,
    external_id: str | None = None,
    url: str | None = None,
    title: str | None = None,
    company: str | None = None,
    location: str | None = None,
    comp_text: str | None = None,
    posted_date: str | None = None,
    jd_text: str | None = None,
    fast_score: float | None = None,
    llm_score: float | None = None,
    specialty: str | None = None,
    rationale: str | None = None,
    evidence_json: str | None = None,
) -> int:
    """Insert or re-sight a posting by *dedup_key*.

    - First sight: inserts with status='sourced', first_seen_at=last_seen_at=now.
    - Re-sight: bumps last_seen_at ONLY — all other columns (including status)
      are left untouched.  dismissed/promoted/expired rows are never resurrected.

    Returns the posting's ``id``.
    """
    now = _now_iso()

    # Attempt insert (ignored if dedup_key already exists)
    conn.execute(
        """
        INSERT OR IGNORE INTO postings (
            source, external_id, url, title, company, location, comp_text,
            posted_date, jd_text, fast_score, llm_score, specialty, rationale,
            evidence_json, status, dedup_key, first_seen_at, last_seen_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, 'sourced', ?, ?, ?
        )
        """,
        (
            source,
            external_id,
            url,
            title,
            company,
            location,
            comp_text,
            posted_date,
            jd_text,
            fast_score,
            llm_score,
            specialty,
            rationale,
            evidence_json,
            dedup_key,
            now,
            now,
        ),
    )

    # Re-sight: bump last_seen_at regardless of whether the INSERT fired
    conn.execute(
        "UPDATE postings SET last_seen_at = ? WHERE dedup_key = ?",
        (now, dedup_key),
    )
    conn.commit()

    row = conn.execute(
        "SELECT id FROM postings WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return int(row["id"])


def get_posting_by_id(
    conn: sqlite3.Connection, posting_id: int
) -> sqlite3.Row | None:
    """Return the postings row for *posting_id*, or None if not found."""
    return conn.execute(
        "SELECT * FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()


def get_posting_by_dedup_key(
    conn: sqlite3.Connection, *, dedup_key: str
) -> sqlite3.Row | None:
    """Return the postings row for *dedup_key*, or None if not found."""
    return conn.execute(
        "SELECT * FROM postings WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()


# ---------------------------------------------------------------------------
# postings — re-sight without insert
# ---------------------------------------------------------------------------


def touch_posting_by_dedup_key(
    conn: sqlite3.Connection,
    *,
    dedup_key: str,
) -> bool:
    """Bump last_seen_at for an existing posting without changing any other column.

    Used to re-sight a posting that was filtered (not upserted) so that
    expire_stale_postings does not expire it while the live job is still
    visible.  Only updates rows that already exist; does nothing if the
    dedup_key is not found.

    Returns True if the row was found and updated, False otherwise.
    """
    now = _now_iso()
    cursor = conn.execute(
        "UPDATE postings SET last_seen_at = ? WHERE dedup_key = ?",
        (now, dedup_key),
    )
    conn.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# postings — status transitions
# ---------------------------------------------------------------------------


def set_posting_status(
    conn: sqlite3.Connection,
    *,
    posting_id: int,
    status: str,
) -> None:
    """Update the status of posting *posting_id*.

    Raises ``ValueError`` if *status* is not in ``POSTING_STATUSES``.
    """
    if status not in POSTING_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; must be one of {sorted(POSTING_STATUSES)}"
        )
    conn.execute(
        "UPDATE postings SET status = ? WHERE id = ?", (status, posting_id)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# promote_posting — create apply_runs row + link
# ---------------------------------------------------------------------------


def promote_posting(
    conn: sqlite3.Connection,
    *,
    posting_id: int,
) -> str:
    """Promote a posting to the apply pipeline.

    1. Looks up the posting row; raises ``ValueError`` if not found.
    2. Idempotent: if ``promoted_application_id`` is already set, returns it.
    3. Creates an ``apply_runs`` row (status='in-progress', phase='gather').
    4. Sets posting status=promoted and promoted_application_id=run_id.

    Returns the ``run_id`` of the linked apply_runs row.
    """
    row = get_posting_by_id(conn, posting_id)
    if row is None:
        raise ValueError(f"posting id={posting_id} not found")

    # Idempotent: already promoted
    if row["promoted_application_id"]:
        return str(row["promoted_application_id"])

    # Derive a slug from company + title (fallback to dedup_key)
    company = row["company"] or ""
    title = row["title"] or ""
    if company or title:
        slug = f"{_slugify(company)}-{_slugify(title)}".strip("-")
    else:
        slug = _slugify(row["dedup_key"])

    run_id = str(uuid.uuid4())
    now = _now_iso()

    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, NULL, ?)",
        (run_id, slug, "gather", now, "in-progress"),
    )
    conn.execute(
        "UPDATE postings SET status = 'promoted', promoted_application_id = ? WHERE id = ?",
        (run_id, posting_id),
    )
    conn.commit()

    return run_id


# ---------------------------------------------------------------------------
# sourcing_runs helpers
# ---------------------------------------------------------------------------


def upsert_sourcing_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> None:
    """Create a sourcing_runs row with status='running' (INSERT OR IGNORE)."""
    now = _now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO sourcing_runs (run_id, started_at, status) "
        "VALUES (?, ?, 'running')",
        (run_id, now),
    )
    conn.commit()


def finish_sourcing_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    new_count: int = 0,
    updated_count: int = 0,
    skipped_count: int = 0,
    filtered_count: int = 0,
    degraded_sources: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Mark a sourcing run as finished with counts and optional error info."""
    now = _now_iso()
    degraded_json = json.dumps(degraded_sources) if degraded_sources else None
    conn.execute(
        """
        UPDATE sourcing_runs SET
            finished_at = ?,
            status = ?,
            new_count = ?,
            updated_count = ?,
            skipped_count = ?,
            filtered_count = ?,
            degraded_sources_json = ?,
            error = ?
        WHERE run_id = ?
        """,
        (
            now,
            status,
            new_count,
            updated_count,
            skipped_count,
            filtered_count,
            degraded_json,
            error,
            run_id,
        ),
    )
    conn.commit()


def purge_old_sourcing_runs(
    conn: sqlite3.Connection,
    *,
    keep: int = _DEFAULT_KEEP_RUNS,
) -> int:
    """Delete oldest sourcing_runs rows beyond the last *keep*, ordered by started_at.

    Returns the number of rows deleted.
    """
    total = conn.execute("SELECT COUNT(*) FROM sourcing_runs").fetchone()[0]
    to_delete = total - keep
    if to_delete <= 0:
        return 0

    conn.execute(
        """
        DELETE FROM sourcing_runs WHERE run_id IN (
            SELECT run_id FROM sourcing_runs
            ORDER BY started_at ASC
            LIMIT ?
        )
        """,
        (to_delete,),
    )
    conn.commit()
    return to_delete


__all__ = [
    "POSTING_STATUSES",
    # postings
    "upsert_posting",
    "get_posting_by_id",
    "get_posting_by_dedup_key",
    "set_posting_status",
    "promote_posting",
    # sourcing_runs
    "upsert_sourcing_run",
    "finish_sourcing_run",
    "purge_old_sourcing_runs",
]
