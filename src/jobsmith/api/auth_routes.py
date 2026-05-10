"""/api/auth router — user profile + session management.

This slice (feat-ddd98f7d) ships GET /me only, protected by the existing
bearer-token dep. Slice 4 (feat-901b79a7) layers JWT login/refresh/logout
on top.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from jobsmith.api.auth import verify_token
from jobsmith.api.schemas.auth import UserRecord
from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


def _resolve_db_path(request: Request) -> Path:
    """Locate the pipeline DB via the cached repo_root + on-disk config."""
    repo_root: Path = request.app.state.repo_root
    config_path = find_config(repo_root)
    if config_path is None:
        raise HTTPException(status_code=503, detail="No jobsmith config found")
    config = load_config(path=config_path)
    return (config_path.parent / config.output.jobsmith_db).resolve()


@router.get("/me", response_model=UserRecord)
def get_me(
    request: Request,
    _auth: None = Depends(verify_token),
) -> UserRecord:
    """Return the active user profile.

    Resolves the user from the on-disk config's email; if no row exists in
    the users table yet, the lifespan upsert was skipped or failed —
    surface a 404 rather than fabricating a record.
    """
    db_path = _resolve_db_path(request)
    config_path = find_config(request.app.state.repo_root)
    if config_path is None:
        raise HTTPException(status_code=503, detail="No jobsmith config found")
    config = load_config(path=config_path)
    email = (getattr(config.user, "email", "") or "").strip()
    if not email:
        raise HTTPException(status_code=404, detail="No user configured")

    conn = open_pipeline_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT user_id, email, name, created_at FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserRecord(
        user_id=row["user_id"],
        email=row["email"],
        name=row["name"],
        created_at=row["created_at"],
    )


__all__ = ["router"]
