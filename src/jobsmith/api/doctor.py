"""/api/doctor router — run preflight environment checks.

Endpoints
---------
GET /doctor              Run all checks; returns list[DoctorCheckResult].
GET /doctor/llm-cache    Return llm_cache aggregate counters (feat-ff4ccde2).

The endpoints are idempotent (read-only). Auth is enforced via the top-level
include_router dependency in main.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request

from jobsmith.doctor import run_all_checks
from jobsmith.llm.sqlite_cache import cache_stats

from .schemas.doctor import DoctorCheckResult

router = APIRouter(tags=["doctor"])


def _map_status(ok: bool) -> str:
    """Map a CheckResult.ok bool to the API status string."""
    return "pass" if ok else "fail"


def _resolve_cwd() -> Path | None:
    """Resolve the project root from JOBSMITH_REPO_ROOT env var, if set."""
    repo_root = os.environ.get("JOBSMITH_REPO_ROOT", "").strip()
    return Path(repo_root).resolve() if repo_root else None


@router.get("/doctor", response_model=list[DoctorCheckResult])
def get_doctor() -> list[DoctorCheckResult]:
    """Run all preflight checks and return the results.

    Always returns HTTP 200 with the full list — callers inspect ``status``
    per item to determine if action is required.
    """
    results = run_all_checks(cwd=_resolve_cwd())
    return [
        DoctorCheckResult(
            name=r.name,
            status=_map_status(r.ok),
            message=r.message,
        )
        for r in results
    ]


@router.get("/doctor/llm-cache")
def get_llm_cache_stats(request: Request) -> dict:
    """Return ``{total_entries, total_hits}`` for the llm_cache table.

    Returns zeros when no DB is wired up so the dashboard renders cleanly
    on a fresh install.
    """
    from jobsmith.config import find_config, load_config
    from jobsmith.db import open_pipeline_db

    repo_root = getattr(request.app.state, "repo_root", None)
    if repo_root is None:
        return {"total_entries": 0, "total_hits": 0}
    config_path = find_config(repo_root)
    if config_path is None:
        return {"total_entries": 0, "total_hits": 0}
    config = load_config(path=config_path)
    db_path = (config_path.parent / config.output.jobsmith_db).resolve()
    if not db_path.exists():
        return {"total_entries": 0, "total_hits": 0}
    conn = open_pipeline_db(db_path)
    try:
        return cache_stats(conn)
    finally:
        conn.close()


__all__ = ["router"]
