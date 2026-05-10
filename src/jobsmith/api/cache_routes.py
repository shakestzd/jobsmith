"""/api/cache router — cache management endpoints (feat-ff4ccde2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from jobsmith.api.auth import current_user
from jobsmith.api.schemas.auth import UserRecord
from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db
from jobsmith.llm.sqlite_cache import invalidate_all

router = APIRouter(tags=["cache"])


@router.post("/cache/invalidate")
def invalidate_cache(
    request: Request,
    _user: UserRecord = Depends(current_user),
) -> dict:
    """Drop every row from ``llm_cache``. Returns the number deleted."""
    repo_root = getattr(request.app.state, "repo_root", None)
    if repo_root is None:
        raise HTTPException(status_code=503, detail="No repo root configured")
    config_path = find_config(repo_root)
    if config_path is None:
        raise HTTPException(status_code=503, detail="No jobsmith config found")
    config = load_config(path=config_path)
    db_path = (config_path.parent / config.output.jobsmith_db).resolve()
    conn = open_pipeline_db(db_path)
    try:
        deleted = invalidate_all(conn)
    finally:
        conn.close()
    return {"deleted": deleted}


__all__ = ["router"]
