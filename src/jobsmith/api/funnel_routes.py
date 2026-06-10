"""/api/funnel router — sourcing-to-apply funnel dashboard data.

Endpoint
--------
GET /funnel
    Per-stage counts, adjacent-stage conversion rates, and per-source yield.

Stage definitions (cohort = posting first_seen_at within the window):
  sourced   : postings with status IN ('sourced')
  queued    : postings with status IN ('queued')
  promoted  : postings with status IN ('promoted')
  interview : apply_runs linked via promoted_application_id whose status = 'interview'
  offer     : apply_runs linked via promoted_application_id whose status = 'offer'

Conversion rates are computed between ADJACENT stages:
  sourced_to_queued       = queued / sourced
  queued_to_promoted      = promoted / (queued + promoted)  [queued-or-beyond]
  promoted_to_interview   = interview / promoted
  interview_to_offer      = offer / interview

Window filter (days): 7 | 30 | 90 | all. Default: 30.
Cohort = posting first_seen_at within the window.

Per-source yield:
  For each distinct source: postings (within cohort), applied (promoted count),
  interview count, offer count (joining through promoted_application_id).

Auth is enforced via the top-level include_router dependency in main.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db
from jobsmith.paths import repo_root_for

logger = logging.getLogger(__name__)

router = APIRouter(tags=["funnel"])

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

WindowDays = Literal[7, 30, 90]


class FunnelStages(BaseModel):
    sourced: int
    queued: int
    promoted: int
    interview: int
    offer: int


class FunnelConversions(BaseModel):
    sourced_to_queued: float | None
    queued_to_promoted: float | None
    promoted_to_interview: float | None
    interview_to_offer: float | None


class PerSourceRow(BaseModel):
    source: str
    postings: int
    applied: int
    interview: int
    offer: int


class FunnelResponse(BaseModel):
    window: int | None  # None means 'all'
    stages: FunnelStages
    conversions: FunnelConversions
    per_source: list[PerSourceRow]


# ---------------------------------------------------------------------------
# DB path helper — module-level so tests can monkeypatch it
# ---------------------------------------------------------------------------


def _get_db_path() -> Path:
    """Resolve the pipeline DB path from the nearest .apply-config.yaml."""
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
# Query logic
# ---------------------------------------------------------------------------


def _safe_rate(numerator: int, denominator: int) -> float | None:
    """Return numerator/denominator as float, or None when denominator == 0."""
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _build_window_clause(window: int | None, col: str = "p.first_seen_at") -> str:
    """Return a SQL WHERE fragment for the time window (empty string = no filter)."""
    if window is None:
        return ""
    return f"AND {col} >= datetime('now', '-{window} days')"


def compute_funnel(db_path: Path, window: int | None) -> FunnelResponse:
    """Run funnel queries against the DB and return structured results."""
    try:
        conn = open_pipeline_db(db_path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc

    try:
        win_clause = _build_window_clause(window)

        # ── Stage counts ────────────────────────────────────────────────────
        # sourced, queued, promoted: from postings table
        stage_sql = f"""
            SELECT
                SUM(CASE WHEN p.status = 'sourced'   THEN 1 ELSE 0 END) AS sourced,
                SUM(CASE WHEN p.status = 'queued'    THEN 1 ELSE 0 END) AS queued,
                SUM(CASE WHEN p.status = 'promoted'  THEN 1 ELSE 0 END) AS promoted
            FROM postings p
            WHERE 1=1 {win_clause}
        """
        row = conn.execute(stage_sql).fetchone()
        sourced = row["sourced"] or 0
        queued = row["queued"] or 0
        promoted = row["promoted"] or 0

        # interview and offer: from apply_runs whose linked posting is in cohort
        outcome_sql = f"""
            SELECT
                SUM(CASE WHEN ar.status = 'interview' THEN 1 ELSE 0 END) AS interview,
                SUM(CASE WHEN ar.status = 'offer'     THEN 1 ELSE 0 END) AS offer
            FROM apply_runs ar
            JOIN postings p ON p.promoted_application_id = ar.run_id
            WHERE 1=1 {win_clause}
        """
        orow = conn.execute(outcome_sql).fetchone()
        interview = orow["interview"] or 0
        offer = orow["offer"] or 0

        # ── Conversion rates ────────────────────────────────────────────────
        # sourced_to_queued: queued / sourced
        sourced_to_queued = _safe_rate(queued, sourced)
        # queued_to_promoted: promoted / (queued + promoted)
        queued_to_promoted = _safe_rate(promoted, queued + promoted)
        # promoted_to_interview: interview / promoted
        promoted_to_interview = _safe_rate(interview, promoted)
        # interview_to_offer: offer / interview
        interview_to_offer = _safe_rate(offer, interview)

        # ── Per-source yield ────────────────────────────────────────────────
        per_source_sql = f"""
            SELECT
                p.source,
                COUNT(*) AS postings,
                SUM(CASE WHEN p.status = 'promoted' THEN 1 ELSE 0 END) AS applied,
                SUM(CASE WHEN ar.status = 'interview' THEN 1 ELSE 0 END) AS interview,
                SUM(CASE WHEN ar.status = 'offer'     THEN 1 ELSE 0 END) AS offer
            FROM postings p
            LEFT JOIN apply_runs ar ON ar.run_id = p.promoted_application_id
            WHERE 1=1 {win_clause}
            GROUP BY p.source
            ORDER BY postings DESC, p.source ASC
        """
        per_source_rows = conn.execute(per_source_sql).fetchall()
        per_source = [
            PerSourceRow(
                source=r["source"],
                postings=r["postings"] or 0,
                applied=r["applied"] or 0,
                interview=r["interview"] or 0,
                offer=r["offer"] or 0,
            )
            for r in per_source_rows
        ]

        return FunnelResponse(
            window=window,
            stages=FunnelStages(
                sourced=sourced,
                queued=queued,
                promoted=promoted,
                interview=interview,
                offer=offer,
            ),
            conversions=FunnelConversions(
                sourced_to_queued=sourced_to_queued,
                queued_to_promoted=queued_to_promoted,
                promoted_to_interview=promoted_to_interview,
                interview_to_offer=interview_to_offer,
            ),
            per_source=per_source,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /funnel
# ---------------------------------------------------------------------------

_VALID_WINDOWS = {7, 30, 90}


@router.get("/funnel", response_model=FunnelResponse)
def get_funnel(
    window: Annotated[
        str,
        Query(
            description=(
                "Time window in days for the cohort (posting first_seen_at). "
                "Accepted values: 7, 30, 90, all. Default: 30."
            )
        ),
    ] = "30",
) -> FunnelResponse:
    """Return funnel stage counts, conversion rates, and per-source yield.

    The cohort is postings whose ``first_seen_at`` falls within the window.
    """
    # Validate and parse window
    if window == "all":
        window_days: int | None = None
    else:
        try:
            w = int(window)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid window {window!r}. Must be 7, 30, 90, or 'all'.",
            ) from exc
        if w not in _VALID_WINDOWS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid window {w}. Must be one of {sorted(_VALID_WINDOWS)} or 'all'.",
            )
        window_days = w

    db_path = _get_db_path()
    return compute_funnel(db_path, window_days)


__all__ = ["router"]
