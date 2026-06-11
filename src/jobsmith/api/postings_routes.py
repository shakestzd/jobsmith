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
import re as _re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

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


async def _launch_apply_run(request: Request, *, slug: str, url: str, jd_text: str | None):
    """Launch the in-process apply pipeline for a freshly promoted posting.

    Routes through the same supervisor path as POST /api/applications
    (bug-fa863c68: promote previously only created the apply_runs row, so
    the pipeline never started). Module-level so tests can monkeypatch.
    Skips silently when a run for the slug is already active.
    """
    from jobsmith.api.applications import _launch_run, _resolve_supervisor

    supervisor = _resolve_supervisor(request)
    if supervisor.get_active_for_slug(slug) is not None:
        logger.info("promote: run already active for %s — not relaunching", slug)
        return
    await _launch_run(supervisor, slug, url, repo_root_for(), jd_text=jd_text or None)


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
# URL allow-list for JD fetch (branch-review finding #3)
# ---------------------------------------------------------------------------

# Patterns that must NOT be fetched (loopback / private ranges / non-http).
# This is a best-effort client-side guard; a full DNS-resolution check is out
# of scope.  The regex matches the *host* portion of the URL.
_BLOCKED_HOST_RE = _re.compile(
    r"""
    ^(?:
        localhost                       |   # localhost
        127(?:\.\d{1,3}){3}            |   # 127.x.x.x
        0\.0\.0\.0                      |   # 0.0.0.0
        ::1                             |   # IPv6 loopback
        10(?:\.\d{1,3}){3}             |   # 10.x.x.x
        172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}  |  # 172.16-31.x.x
        192\.168(?:\.\d{1,3}){2}        # 192.168.x.x
    )$
    """,
    _re.VERBOSE | _re.IGNORECASE,
)


def _is_safe_jd_url(url: str) -> bool:
    """Return True only when *url* is safe to fetch for JD content.

    Accepted: http:// or https:// scheme with a non-private, non-loopback host.
    Rejected: any other scheme (file://, ftp://, …), localhost, loopback IPs,
    and RFC-1918 private ranges.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.hostname or ""
    if not host:
        return False

    return not _BLOCKED_HOST_RE.match(host)


async def _fetch_jd_text(url: str) -> str | None:
    """Attempt to fetch raw JD text from *url*.

    Returns None (without raising) on any error so that callers can set
    jd_fetch_failed=True and continue promoting.

    Safety: URLs that fail :func:`_is_safe_jd_url` are silently skipped — the
    caller treats None exactly the same as a network failure.
    """
    if not _is_safe_jd_url(url):
        logger.debug("JD fetch skipped — URL failed safety check: %s", url)
        return None

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


_POSTINGS_LIST_COLUMNS = (
    "id, source, external_id, url, title, company, location, comp_text, "
    "posted_date, fast_score, llm_score, specialty, rationale, status, "
    "promoted_application_id, dedup_key, first_seen_at, last_seen_at"
)
_POSTINGS_DEFAULT_LIMIT = 200
_POSTINGS_MAX_LIMIT = 1000


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
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=_POSTINGS_MAX_LIMIT,
            description=f"Maximum rows to return (default {_POSTINGS_DEFAULT_LIMIT}, max {_POSTINGS_MAX_LIMIT}).",
        ),
    ] = _POSTINGS_DEFAULT_LIMIT,
    offset: Annotated[
        int,
        Query(ge=0, description="Row offset for pagination (default 0)."),
    ] = 0,
) -> list[PostingRow]:
    """Return ranked postings matching the given filters.

    Ranking: llm_score DESC NULLS LAST, fast_score DESC NULLS LAST,
    first_seen_at DESC (stable tie-breaker).

    jd_text is excluded from the list response to keep payload sizes small.
    Use limit/offset for pagination (default limit 200, max 1000).
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

        # jd_text is excluded — it can be tens of KB per row and is not needed
        # for the list view.  Use the /postings/{id} detail endpoint for full text.
        sql = f"""
            SELECT {_POSTINGS_LIST_COLUMNS}
            FROM postings
            {where_sql}
            ORDER BY
                llm_score DESC NULLS LAST,
                fast_score DESC NULLS LAST,
                first_seen_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
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
    request: Request,
) -> PostingPromoteResponse:
    """Promote a posting to the apply pipeline.

    Behaviour
    ---------
    1. Looks up the posting; returns 404 when not found.
    2. If jd_text is absent, attempts a quick httpx fetch from the posting URL.
       Fetch failure never blocks promote; sets jd_fetch_failed=True in the response.
    3. Calls promote_posting() (idempotent) to create / link the apply_runs row.
    4. Launches the apply pipeline via the supervisor (bug-fa863c68) on first
       promote — launch failure never blocks promote; sets launched=False.
    5. Returns {run_id, slug, jd_fetch_failed, launched}.
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

        already_promoted = row["status"] == "promoted"
        jd_fetch_failed = False
        jd_text_val = row["jd_text"]

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
                jd_text_val = fetched
            else:
                jd_fetch_failed = True

        run_id = promote_posting(conn, posting_id=posting_id)

        # Derive slug from the apply_runs row
        run_row = conn.execute(
            "SELECT slug FROM apply_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        slug = run_row["slug"] if run_row else None

        # Launch the pipeline on first promote (bug-fa863c68). A repeat
        # promote of an already-promoted posting never relaunches.
        launched = False
        if slug and row["url"] and not already_promoted:
            try:
                await _launch_apply_run(
                    request, slug=slug, url=row["url"], jd_text=jd_text_val
                )
                launched = True
            except Exception:
                logger.warning(
                    "promote: apply-run launch failed for %s — application row "
                    "created; use re-run apply to start the pipeline",
                    slug,
                    exc_info=True,
                )

        return PostingPromoteResponse(
            run_id=run_id,
            slug=slug,
            jd_fetch_failed=jd_fetch_failed,
            launched=launched,
        )
    finally:
        conn.close()


__all__ = ["router"]
