"""/api/auth router — JWT login/refresh/logout + user profile.

Slice 4 (feat-901b79a7) wires the JWT pipeline. The legacy bearer token
remains accepted on every protected route via :func:`current_user` so
headless automation keeps working without a registered user account.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from jobsmith.api.auth import (
    DEFAULT_ACCESS_TOKEN_MINUTES,
    DEFAULT_REFRESH_TOKEN_DAYS,
    PRIVATE_TOKEN_PATH,
    create_access_token,
    create_refresh_token,
    current_user,
    hash_password,
    load_jwt_secret,
    verify_password,
    verify_refresh_token,
    verify_token,
)
from jobsmith.api.schemas.auth import UserRecord
from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class SetPasswordRequest(BaseModel):
    new_password: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_paths(request: Request) -> tuple[Path, Path]:
    """Return (db_path, db_dir) resolved via the cached repo_root + config."""
    repo_root: Path = request.app.state.repo_root
    config_path = find_config(repo_root)
    if config_path is None:
        raise HTTPException(status_code=503, detail="No jobsmith config found")
    config = load_config(path=config_path)
    db_path = (config_path.parent / config.output.jobsmith_db).resolve()
    return db_path, db_path.parent


def _open_db(request: Request) -> sqlite3.Connection:
    db_path, _ = _resolve_paths(request)
    conn = open_pipeline_db(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_default_user(conn: sqlite3.Connection, request: Request) -> sqlite3.Row | None:
    """Resolve the configured user (single-tenant install)."""
    config_path = find_config(request.app.state.repo_root)
    if config_path is None:
        return None
    config = load_config(path=config_path)
    email = (getattr(config.user, "email", "") or "").strip()
    if not email:
        return None
    return conn.execute(
        "SELECT user_id, email, name, hashed_pw, created_at FROM users WHERE email = ?",
        (email,),
    ).fetchone()


def _issue_token_pair(
    conn: sqlite3.Connection, user_id: str, secret: str
) -> TokenPair:
    """Mint an access + refresh token pair and persist the refresh hash."""
    access = create_access_token(user_id, secret, expire_minutes=DEFAULT_ACCESS_TOKEN_MINUTES)
    raw_refresh, hashed_refresh = create_refresh_token()
    expires_at = (
        datetime.now(tz=timezone.utc) + timedelta(days=DEFAULT_REFRESH_TOKEN_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) "
        "VALUES (?, ?, ?)",
        (user_id, hashed_refresh, expires_at),
    )
    conn.commit()
    return TokenPair(access_token=access, refresh_token=raw_refresh)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/me", response_model=UserRecord)
def get_me(user: UserRecord = Depends(current_user)) -> UserRecord:
    """Return the active user profile."""
    return user


@router.post("/login", response_model=TokenPair)
def login(body: LoginRequest, request: Request) -> TokenPair:
    """Exchange a password for an access + refresh token pair.

    On a fresh install (``hashed_pw == ''``) the password is matched against
    the legacy ``private/jobsmith.token`` content so the first sign-in works
    before the user calls ``set-password``.
    """
    _, db_dir = _resolve_paths(request)
    conn = _open_db(request)
    try:
        row = _load_default_user(conn, request)
        if row is None:
            raise HTTPException(status_code=404, detail="No user provisioned")

        ok = False
        if row["hashed_pw"]:
            ok = verify_password(body.password, row["hashed_pw"])
        else:
            try:
                expected = PRIVATE_TOKEN_PATH.read_text(encoding="utf-8").strip()
            except OSError:
                expected = ""
            ok = bool(expected) and body.password == expected
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        secret = load_jwt_secret(db_dir)
        return _issue_token_pair(conn, row["user_id"], secret)
    finally:
        conn.close()


@router.post("/refresh", response_model=TokenPair)
def refresh(body: RefreshRequest, request: Request) -> TokenPair:
    _, db_dir = _resolve_paths(request)
    conn = _open_db(request)
    try:
        rows = conn.execute(
            "SELECT session_id, user_id, refresh_token_hash, expires_at, revoked "
            "FROM user_sessions WHERE revoked = 0"
        ).fetchall()
        match = None
        for row in rows:
            if verify_refresh_token(body.refresh_token, row["refresh_token_hash"]):
                match = row
                break
        if match is None:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        # Reject expired sessions.
        try:
            exp_dt = datetime.strptime(match["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid session expiry") from None
        if exp_dt < datetime.now(tz=timezone.utc):
            raise HTTPException(status_code=401, detail="Refresh token expired")

        # Revoke the old row, issue a new pair (rotation).
        conn.execute(
            "UPDATE user_sessions SET revoked = 1 WHERE session_id = ?",
            (match["session_id"],),
        )
        conn.commit()
        secret = load_jwt_secret(db_dir)
        return _issue_token_pair(conn, match["user_id"], secret)
    finally:
        conn.close()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshRequest, request: Request) -> None:
    conn = _open_db(request)
    try:
        rows = conn.execute(
            "SELECT session_id, refresh_token_hash FROM user_sessions WHERE revoked = 0"
        ).fetchall()
        for row in rows:
            if verify_refresh_token(body.refresh_token, row["refresh_token_hash"]):
                conn.execute(
                    "UPDATE user_sessions SET revoked = 1 WHERE session_id = ?",
                    (row["session_id"],),
                )
                conn.commit()
                return
        # Idempotent: unknown/already-revoked refresh tokens still 204.
        return
    finally:
        conn.close()


@router.post("/set-password", status_code=status.HTTP_204_NO_CONTENT)
def set_password(
    body: SetPasswordRequest,
    request: Request,
    _legacy_auth: None = Depends(verify_token),
) -> None:
    """First-time password setup, gated on the legacy static bearer token."""
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(
            status_code=400, detail="new_password must be at least 8 characters"
        )
    conn = _open_db(request)
    try:
        row = _load_default_user(conn, request)
        if row is None:
            raise HTTPException(status_code=404, detail="No user provisioned")
        if row["hashed_pw"]:
            raise HTTPException(status_code=409, detail="Password already set")
        conn.execute(
            "UPDATE users SET hashed_pw = ?, "
            "    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE user_id = ?",
            (hash_password(body.new_password), row["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()


__all__ = ["router"]
