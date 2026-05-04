"""Bearer-token authentication for the jobsmith HTTP API.

Token resolution order:
1. ``JOBSMITH_API_TOKEN`` environment variable (highest priority)
2. ``private/jobsmith.token`` file in cwd-relative private/ directory
   (written on first server start with mode 0600; auto-generated via
   ``secrets.token_urlsafe`` if neither source is found).

Usage
-----
Apply as a FastAPI dependency at the router include level::

    app.include_router(router, prefix="/api", dependencies=[Depends(verify_token)])

The health/readiness endpoints are excluded from this dependency.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN_ENV_VAR = "JOBSMITH_API_TOKEN"

# Default path for the auto-generated token file.  Resolved relative to cwd
# at import time so tests can override via ``patch("jobsmith.api.auth.PRIVATE_TOKEN_PATH", ...)``.
PRIVATE_TOKEN_PATH: Path = Path("private") / "jobsmith.token"

_bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_private_dir(path: Path) -> None:
    """Create parent directory with restricted permissions if it does not exist."""
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        logger.info("Created private/ directory at %s", parent)


def _write_token_file(path: Path, token: str) -> None:
    """Write *token* to *path* atomically with mode 0600."""
    _ensure_private_dir(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token, encoding="utf-8")
    # Set mode before rename so the file is never world-readable.
    tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    tmp.rename(path)
    logger.info("Wrote API token to %s (mode 0600)", path)


def _read_token_file(path: Path) -> str | None:
    """Return the token stored in *path*, or None if unavailable."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


@lru_cache(maxsize=1)
def _get_expected_token() -> str:
    """Return the expected API token, generating one if necessary.

    Resolution order:
    1. ``JOBSMITH_API_TOKEN`` env var
    2. ``PRIVATE_TOKEN_PATH`` file
    3. Generate a new token and persist it to ``PRIVATE_TOKEN_PATH``

    The result is cached after the first call.  Tests that need different
    tokens should call ``_get_expected_token.cache_clear()`` after patching.
    """
    # 1. Environment variable
    env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token

    # 2. File
    file_token = _read_token_file(PRIVATE_TOKEN_PATH)
    if file_token:
        return file_token

    # 3. Auto-generate and persist
    new_token = secrets.token_urlsafe(32)
    try:
        _write_token_file(PRIVATE_TOKEN_PATH, new_token)
        logger.warning(
            "No API token configured. Generated one at %s — keep it secret.",
            PRIVATE_TOKEN_PATH,
        )
    except OSError as exc:
        logger.error("Could not write token file: %s", exc)
    return new_token


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),  # noqa: B008
) -> None:
    """FastAPI dependency that validates the Bearer token.

    Raises 401 if the token is missing or incorrect.
    Attach via ``dependencies=[Depends(verify_token)]`` on include_router.
    """
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
