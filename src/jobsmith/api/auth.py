"""Bearer-token + JWT authentication for the jobsmith HTTP API.

Two auth modes coexist:

1. **Static bearer token** (legacy, for headless/automation).  Resolved from
   ``JOBSMITH_API_TOKEN`` env var or ``private/jobsmith.token`` file.  Used
   by ``verify_token`` / ``verify_token_or_query`` and accepted as a fallback
   by ``current_user`` (resolves to the configured user from the DB).

2. **JWT access tokens** (slice 4, feat-901b79a7).  Issued by
   ``POST /api/auth/login``; verified by ``current_user``.  HS256 over a
   per-install secret stored at ``{db_dir}/.jwt_secret`` (mode 0o600) or
   the ``JOBSMITH_JWT_SECRET`` env var.

SSE endpoints (browser EventSource cannot set headers) get
``current_user_or_query`` which also accepts the access token via ?token=.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import bcrypt
import jwt
from fastapi import HTTPException, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from jobsmith.api.schemas.auth import UserRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN_ENV_VAR = "JOBSMITH_API_TOKEN"
JWT_SECRET_ENV_VAR = "JOBSMITH_JWT_SECRET"
JWT_ALGORITHM = "HS256"
DEFAULT_ACCESS_TOKEN_MINUTES = 60
DEFAULT_REFRESH_TOKEN_DAYS = 30

PRIVATE_TOKEN_PATH: Path = Path("private") / "jobsmith.token"

_bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Static bearer token (legacy)
# ---------------------------------------------------------------------------


def _ensure_private_dir(path: Path) -> None:
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        logger.info("Created private/ directory at %s", parent)


def _write_secret_file(path: Path, value: str) -> None:
    _ensure_private_dir(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    tmp.rename(path)
    logger.info("Wrote secret to %s (mode 0600)", path)


def _read_secret_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


@lru_cache(maxsize=1)
def _get_expected_token() -> str:
    """Return the legacy static bearer token, generating one if necessary."""
    env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token
    file_token = _read_secret_file(PRIVATE_TOKEN_PATH)
    if file_token:
        return file_token
    new_token = secrets.token_urlsafe(32)
    try:
        _write_secret_file(PRIVATE_TOKEN_PATH, new_token)
        logger.warning(
            "No API token configured. Generated one at %s — keep it secret.",
            PRIVATE_TOKEN_PATH,
        )
    except OSError as exc:
        logger.error("Could not write token file: %s", exc)
    return new_token


# ---------------------------------------------------------------------------
# JWT secret + token helpers
# ---------------------------------------------------------------------------


def load_jwt_secret(db_dir: Path) -> str:
    """Return the JWT signing secret, generating + persisting one if needed.

    Resolution order:
    1. ``JOBSMITH_JWT_SECRET`` env var.
    2. ``{db_dir}/.jwt_secret`` file.
    3. Generate a random 32-byte urlsafe secret and write it (mode 0o600).
    """
    env_secret = os.environ.get(JWT_SECRET_ENV_VAR, "").strip()
    if env_secret:
        return env_secret
    secret_path = db_dir / ".jwt_secret"
    on_disk = _read_secret_file(secret_path)
    if on_disk:
        return on_disk
    new_secret = secrets.token_urlsafe(32)
    try:
        _write_secret_file(secret_path, new_secret)
    except OSError as exc:
        logger.error("Could not write JWT secret file: %s", exc)
    return new_secret


def create_access_token(
    user_id: str,
    secret: str,
    expire_minutes: int = DEFAULT_ACCESS_TOKEN_MINUTES,
) -> str:
    """Issue a short-lived HS256 access token for *user_id*."""
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_refresh_token() -> tuple[str, str]:
    """Generate a refresh token and its sha256 hash for storage.

    Refresh tokens are already cryptographically random (~256 bits of
    entropy) so a fast hash is sufficient and indexable; bcrypt's slow
    KDF is reserved for low-entropy passwords.
    """
    raw = secrets.token_urlsafe(48)
    return raw, _sha256_hex(raw)


def verify_refresh_token(raw: str, hashed: str) -> bool:
    """Constant-time comparison of sha256(raw) against the stored hash."""
    return secrets.compare_digest(_sha256_hex(raw), hashed)


def _password_bytes(password: str) -> bytes:
    """Pre-digest with sha256 (raw 32 bytes) so bcrypt's 72-byte cap never bites."""
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_password_bytes(password), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def decode_access_token(token: str, secret: str) -> str | None:
    """Decode *token* and return the ``sub`` (user_id), or None on any failure."""
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


# ---------------------------------------------------------------------------
# Legacy verify_token deps — preserved so older mounts/tests keep working
# ---------------------------------------------------------------------------


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),  # noqa: B008
) -> None:
    """FastAPI dep validating the legacy static Bearer token."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    expected = _get_expected_token()
    if not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def verify_token_or_query(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),  # noqa: B008
    token: str | None = Query(None),
) -> None:
    expected = _get_expected_token()
    if (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and secrets.compare_digest(credentials.credentials, expected)
    ):
        return
    if token is not None and secrets.compare_digest(token, expected):
        return
    raise HTTPException(
        status_code=401,
        detail="Bearer token required (header or ?token= query param)",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# JWT-aware current_user deps (slice 4)
# ---------------------------------------------------------------------------


def _open_db(request: Request) -> sqlite3.Connection:
    """Open the pipeline DB resolved from the cached repo_root + on-disk config."""
    from jobsmith.config import find_config, load_config
    from jobsmith.db import open_pipeline_db

    repo_root: Path = request.app.state.repo_root
    config_path = find_config(repo_root)
    if config_path is None:
        raise HTTPException(status_code=503, detail="No jobsmith config found")
    config = load_config(path=config_path)
    db_path = (config_path.parent / config.output.jobsmith_db).resolve()
    return open_pipeline_db(db_path)


def _row_to_user(row: Any) -> UserRecord:
    return UserRecord(
        user_id=row["user_id"],
        email=row["email"],
        name=row["name"],
        created_at=row["created_at"],
    )


def _lookup_user_by_id(db: sqlite3.Connection, user_id: str) -> UserRecord | None:
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT user_id, email, name, created_at FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return _row_to_user(row) if row else None


def _lookup_user_from_config(request: Request, db: sqlite3.Connection) -> UserRecord | None:
    """Resolve the legacy static-token caller to the configured user row."""
    from jobsmith.config import find_config, load_config

    config_path = find_config(request.app.state.repo_root)
    if config_path is None:
        return None
    config = load_config(path=config_path)
    email = (getattr(config.user, "email", "") or "").strip()
    if not email:
        return None
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT user_id, email, name, created_at FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    return _row_to_user(row) if row else None


_MACHINE_USER = UserRecord(
    user_id="static-token",
    email="static-token@local",
    name="Static Token Caller",
    created_at="1970-01-01T00:00:00Z",
)


def _safe_open_db(request: Request) -> sqlite3.Connection | None:
    """Best-effort DB open. Returns None when no repo_root/config/DB is set up."""
    if not hasattr(request.app.state, "repo_root"):
        return None
    try:
        return _open_db(request)
    except (HTTPException, OSError, sqlite3.Error):
        return None


def _resolve_user_from_token(
    request: Request, raw_token: str | None
) -> UserRecord | None:
    """Decode *raw_token* and return the matching user, or None.

    JWT tokens require a matching ``users`` row; the legacy static token
    falls back to the configured user when present, otherwise to a synthetic
    machine user so headless callers without a DB still authenticate.
    """
    if raw_token is None:
        return None
    # Static-token fast path — does not require DB access.
    expected = _get_expected_token()
    if secrets.compare_digest(raw_token, expected):
        db = _safe_open_db(request)
        if db is None:
            return _MACHINE_USER
        try:
            configured = _lookup_user_from_config(request, db)
        finally:
            db.close()
        return configured if configured is not None else _MACHINE_USER
    # JWT path — requires DB.
    db = _safe_open_db(request)
    if db is None:
        return None
    try:
        secret = load_jwt_secret(_db_dir(request))
        user_id = decode_access_token(raw_token, secret)
        if user_id is None:
            return None
        return _lookup_user_by_id(db, user_id)
    finally:
        db.close()


def _db_dir(request: Request) -> Path:
    """Directory that holds the pipeline DB (used for .jwt_secret co-location)."""
    from jobsmith.config import find_config, load_config

    config_path = find_config(request.app.state.repo_root)
    if config_path is None:
        return request.app.state.repo_root
    config = load_config(path=config_path)
    return (config_path.parent / config.output.jobsmith_db).resolve().parent


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),  # noqa: B008
) -> UserRecord:
    """FastAPI dep returning the authenticated user (JWT or legacy static token)."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = _resolve_user_from_token(request, credentials.credentials)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def current_user_or_query(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),  # noqa: B008
    token: str | None = Query(None),
) -> UserRecord:
    """current_user variant that also accepts the access token via ?token=.

    Required by SSE endpoints — browser EventSource cannot set custom headers.
    Do NOT switch to OAuth2PasswordBearer here: it would trigger 401 before
    the query-param fallback runs.
    """
    raw_token = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        raw_token = credentials.credentials
    elif token is not None:
        raw_token = token
    user = _resolve_user_from_token(request, raw_token)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Bearer token required (header or ?token= query param)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


__all__ = [
    "DEFAULT_ACCESS_TOKEN_MINUTES",
    "DEFAULT_REFRESH_TOKEN_DAYS",
    "JWT_ALGORITHM",
    "JWT_SECRET_ENV_VAR",
    "PRIVATE_TOKEN_PATH",
    "TOKEN_ENV_VAR",
    "create_access_token",
    "create_refresh_token",
    "current_user",
    "current_user_or_query",
    "decode_access_token",
    "hash_password",
    "load_jwt_secret",
    "verify_password",
    "verify_refresh_token",
    "verify_token",
    "verify_token_or_query",
]
