"""/api/sourcing/run-health router (feat-80affa8a).

Endpoint
--------
GET /sourcing/run-health
    Returns the current health state of the sourcing pipeline based on
    the most recent sourcing_runs record.

Response schema::

    {
        "state": "ok" | "failed" | "degraded" | "stale" | "no_runs" | "unknown",
        "last_run_id": "<uuid>" | null,
        "last_run_status": "done" | "failed" | "degraded" | "running" | null,
        "finished_at": "<iso8601>" | null,
        "error": "<message>" | null,
        "degraded_sources": ["<source-key>", ...] | null,
        "age_hours": <float> | null
    }

State semantics
---------------
- ``ok``        — last run done/degraded-free within 25 hours
- ``failed``    — last run status=failed
- ``degraded``  — last run status=degraded (some sources errored)
- ``stale``     — last successful run > 25 hours ago
- ``no_runs``   — sourcing_runs table is empty (first run not yet done)
- ``unknown``   — DB not found or query failed

Auth is enforced via the top-level include_router dependency in main.py.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel

_log = logging.getLogger(__name__)

router = APIRouter(tags=["sourcing"])

_STALE_HOURS = 25


class RunHealthResponse(BaseModel):
    """Response model for GET /sourcing/run-health."""

    state: str  # ok | failed | degraded | stale | no_runs | unknown
    last_run_id: str | None = None
    last_run_status: str | None = None
    finished_at: str | None = None
    error: str | None = None
    degraded_sources: list[str] | None = None
    age_hours: float | None = None


def _resolve_db_path(request: Request) -> Path | None:
    """Resolve jobsmith.db from the request app state.

    Returns None when the DB cannot be located (API-only / fresh install).
    """
    try:
        repo_root = getattr(request.app.state, "repo_root", None)
        if repo_root is None:
            return None

        from jobsmith.config import find_config, load_config

        config_path = find_config(Path(repo_root))
        if config_path is None:
            return None
        config = load_config(path=config_path)
        db_path = (config_path.parent / config.output.jobsmith_db).resolve()
        return db_path if db_path.exists() else None
    except Exception:
        _log.debug("run_health: DB path resolution failed", exc_info=True)
        return None


@router.get("/sourcing/run-health", response_model=RunHealthResponse)
def get_run_health(request: Request) -> RunHealthResponse:
    """Return the health state of the most recent sourcing run.

    Always returns HTTP 200 — the ``state`` field carries the semantic
    result. This design lets the inbox banner poll without needing to
    handle 4xx/5xx for missing-data cases.
    """
    db_path = _resolve_db_path(request)
    if db_path is None:
        return RunHealthResponse(state="unknown")

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT run_id, status, finished_at, degraded_sources_json, error "
                "FROM sourcing_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        _log.warning("run_health: DB query failed: %s", exc)
        return RunHealthResponse(state="unknown")

    if row is None:
        return RunHealthResponse(state="no_runs")

    status = row["status"]
    run_id = row["run_id"]
    finished_at_str = row["finished_at"]
    error = row["error"]
    degraded_json = row["degraded_sources_json"]

    # Parse degraded sources
    degraded_sources: list[str] | None = None
    if degraded_json:
        try:
            degraded_sources = json.loads(degraded_json)
        except Exception:
            degraded_sources = [degraded_json]

    # Compute age
    age_hours: float | None = None
    if finished_at_str:
        try:
            finished_at = datetime.fromisoformat(finished_at_str)
            if finished_at.tzinfo is None:
                finished_at = finished_at.replace(tzinfo=timezone.utc)
            age = datetime.now(tz=timezone.utc) - finished_at
            age_hours = round(age.total_seconds() / 3600, 2)
        except Exception:
            pass

    if status == "failed":
        return RunHealthResponse(
            state="failed",
            last_run_id=run_id,
            last_run_status=status,
            finished_at=finished_at_str,
            error=error,
            age_hours=age_hours,
        )

    if status == "degraded":
        return RunHealthResponse(
            state="degraded",
            last_run_id=run_id,
            last_run_status=status,
            finished_at=finished_at_str,
            degraded_sources=degraded_sources,
            age_hours=age_hours,
        )

    # done or running: check freshness
    if age_hours is not None and age_hours > _STALE_HOURS:
        return RunHealthResponse(
            state="stale",
            last_run_id=run_id,
            last_run_status=status,
            finished_at=finished_at_str,
            age_hours=age_hours,
        )

    return RunHealthResponse(
        state="ok",
        last_run_id=run_id,
        last_run_status=status,
        finished_at=finished_at_str,
        age_hours=age_hours,
    )


__all__ = ["router"]
