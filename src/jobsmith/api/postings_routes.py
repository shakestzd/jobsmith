"""/api/postings router — postings inbox endpoints.

Endpoints
---------
GET  /postings
    Ranked list of postings (llm_score desc, fast_score desc, first_seen_at desc).
    Optional query filters: status, source (prefix match), specialty, min_score.

POST /postings/{id}/status
    Transition the status of a posting (dismiss, queue, etc.).

POST /postings/{id}/promote
    Promote a posting to the apply pipeline. Creates an apply_runs row.
    If the posting has no jd_text, attempts to fetch from the URL.
    NEVER blocks on JD fetch failure — sets jd_fetch_failed=True in response.

Auth is enforced via the top-level include_router dependency in main.py.
The ``_get_db_path`` helper is module-level so tests can monkeypatch it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db
from jobsmith.paths import repo_root_for
from jobsmith.sourcing.store import (
    POSTING_STATUSES,
    get_posting_by_id,
    promote_posting,
    set_posting_status,
)

from .schemas.postings import PostingPromoteResponse, PostingRow, PostingStatusUpdate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["postings"])


# ---------------------------------------------------------------------------
# DB path helper — module-level so tests can monkeypatch it
# ---------------------------------------------------------------------------


def _get_db_path() -> Path:
    """Resolve the pipeline DB path from the nearest .apply-config.yaml.

    Uses the shared ``repo_root_for()`` resolver (settings-aware, env-tier-2).
    Raises 503 when no config is found.
    """
    search_start = repo_root_for()
    config_path = find_config(search_start)
    if config_path is None:
        raise HTTPException(
            status_code=503,
            detail="No .apply-config.yaml found; cannot open pipeline DB.",
        )
    config = load_config(path=config_path)
    repo_root = config_path.parent
    return (repo_root / config.output.jobsmith_db).resolve()


# ---------------------------------------------------------------------------
# JD fetch helper — async, module-level so tests can monkeypatch it
# ---------------------------------------------------------------------------


async def _fetch_jd_text(url: str) -> str | None:
    """Attempt to fetch raw JD text from *url*.

    Uses httpx with a short timeout. Returns None on any error so that
    callers can set jd_fetch_failed=True and continue.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; jobsmith-sourcing/1.0)"
                    )
                },
            )
            if resp.status_code == 200:
                return resp.text[:50_000]  # cap at 50 KB
    except Exception:
        logger.debug("JD fetch failed for %s", url, exc_info=True)
    return None


# ---------------------------------------------------------------------------
# GET /postings
# ---------------------------------------------------------------------------


@router.get("/postings", response_model=list[PostingRow])
def list_postings(
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter by posting status"),
    ] = None,
    source: Annotated[
        str | None,
        Query(description="Filter by source prefix (e.g. 'greenhouse')"),
    ] = None,
    specialty: Annotated[
        str | None,
        Query(description="Filter by specialty"),
    ] = None,
    min_score: Annotated[
        float | None,
        Query(
            description=(
                "Minimum score threshold. Uses llm_score when available, "
                "falls back to fast_score."
            )
        ),
    ] = None,
) -> list[PostingRow]:
    """Return ranked postings matching the given filters.

    Ranking: llm_score DESC NULLS LAST, fast_score DESC NULLS LAST,
    first_seen_at DESC (stable tie-breaker).
    """
    db_path = _get_db_path()
    try:
        conn = open_pipeline_db(db_path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc

    try:
        where_clauses: list[str] = []
        params: list[object] = []

        if status_filter is not None:
            where_clauses.append("status = ?")
            params.append(status_filter)

        if source is not None:
            # Prefix match on source (e.g. "greenhouse" matches "greenhouse/stripe")
            where_clauses.append("source LIKE ?")
            params.append(f"{source}%")

        if specialty is not None:
            where_clauses.append("specialty = ?")
            params.append(specialty)

        if min_score is not None:
            # Use llm_score when not NULL, otherwise fall back to fast_score
            where_clauses.append("COALESCE(llm_score, fast_score) >= ?")
            params.append(min_score)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        sql = f"""
            SELECT * FROM postings
            {where_sql}
            ORDER BY
                llm_score DESC NULLS LAST,
                fast_score DESC NULLS LAST,
                first_seen_at DESC
        """
        rows = conn.execute(sql, params).fetchall()
        return [PostingRow(**dict(row)) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /postings/{id}/status
# ---------------------------------------------------------------------------


@router.post("/postings/{posting_id}/status", response_model=PostingRow)
def update_posting_status(
    posting_id: int,
    body: PostingStatusUpdate,
) -> PostingRow:
    """Update the status of a posting.

    Returns 404 if the posting does not exist, 422 if the status is invalid.
    """
    if body.status not in POSTING_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid status {body.status!r}. "
                f"Must be one of: {sorted(POSTING_STATUSES)}"
            ),
        )

    db_path = _get_db_path()
    try:
        conn = open_pipeline_db(db_path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc

    try:
        row = get_posting_by_id(conn, posting_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Posting id={posting_id} not found.",
            )
        set_posting_status(conn, posting_id=posting_id, status=body.status)
        # Re-read after update
        updated = get_posting_by_id(conn, posting_id)
        return PostingRow(**dict(updated))  # type: ignore[arg-type]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /postings/{id}/promote
# ---------------------------------------------------------------------------


@router.post("/postings/{posting_id}/promote", response_model=PostingPromoteResponse)
async def promote(
    posting_id: int,
) -> PostingPromoteResponse:
    """Promote a posting to the apply pipeline.

    Behaviour
    ---------
    1. Looks up the posting; returns 404 when not found.
    2. If jd_text is absent, attempts a quick httpx fetch from the posting URL.
       Fetch failure never blocks promote; sets jd_fetch_failed=True in the response.
    3. Calls promote_posting() (idempotent) to create / link the apply_runs row.
    4. Returns {run_id, slug, jd_fetch_failed}.
    """
    db_path = _get_db_path()
    try:
        conn = open_pipeline_db(db_path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc

    try:
        row = get_posting_by_id(conn, posting_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Posting id={posting_id} not found.",
            )

        jd_fetch_failed = False

        # If no jd_text and URL is available, attempt a background fetch before
        # creating the apply_runs row so the run has context.
        if not row["jd_text"] and row["url"]:
            fetched = await _fetch_jd_text(row["url"])
            if fetched:
                conn.execute(
                    "UPDATE postings SET jd_text = ? WHERE id = ?",
                    (fetched, posting_id),
                )
                conn.commit()
            else:
                jd_fetch_failed = True

        run_id = promote_posting(conn, posting_id=posting_id)

        # Derive slug from the apply_runs row
        run_row = conn.execute(
            "SELECT slug FROM apply_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        slug = run_row["slug"] if run_row else None

        return PostingPromoteResponse(
            run_id=run_id,
            slug=slug,
            jd_fetch_failed=jd_fetch_failed,
        )
    finally:
        conn.close()


__all__ = ["router"]
