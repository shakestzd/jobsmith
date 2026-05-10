from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Request


def get_repo_root(request: Request) -> Path:
    """Return the repo root cached in app.state at lifespan startup."""
    return request.app.state.repo_root


def upsert_or_load_user(
    db: sqlite3.Connection, config: Any | None
) -> dict[str, Any] | None:
    """Insert (or refresh) the user row from a loaded jobsmith config.

    Reads ``config.user.email`` and ``config.user.name``; INSERT OR IGNORE
    so a duplicate email is treated as already-present, then UPDATE name if
    it has drifted. Returns the row as a dict, or None when the config or
    its required fields are missing — startup must never crash here.
    """
    if config is None:
        return None
    user_cfg = getattr(config, "user", None)
    if user_cfg is None:
        return None
    email = (getattr(user_cfg, "email", "") or "").strip()
    name = (getattr(user_cfg, "name", "") or "").strip()
    if not email or not name:
        return None

    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT OR IGNORE INTO users (email, name) VALUES (?, ?)",
        (email, name),
    )
    db.execute(
        "UPDATE users "
        "SET name = ?, "
        "    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE email = ? AND name != ?",
        (name, email, name),
    )
    db.commit()
    row = db.execute(
        "SELECT user_id, email, name, hashed_pw, created_at, updated_at "
        "FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    return dict(row) if row is not None else None
