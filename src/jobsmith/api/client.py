"""JobsmithClient — httpx-based read-only SDK for the jobsmith HTTP API.

Auth resolution
---------------
Token precedence (highest to lowest):
1. ``token`` constructor argument
2. ``JOBSMITH_API_TOKEN`` environment variable
3. ``~/.jobsmith/token`` file (mode 0600)
4. ``AuthError`` is raised if none of the above is available

Base URL resolution:
1. ``base_url`` constructor argument
2. ``JOBSMITH_API_BASE_URL`` environment variable
3. Default ``http://127.0.0.1:8000``

Usage example::

    client = JobsmithClient()
    health = client.health()
    work = client.get_master_work()
    artifacts = client.list_artifacts("acme-swe", "run-abc123")
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from jobsmith.api.schemas.applications import Application, ApplicationDetail
from jobsmith.api.schemas.artifacts import ArtifactEnvelope
from jobsmith.api.schemas.master import Author, EducationEntry, SkillEntry, WorkEntry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOKEN_ENV_VAR = "JOBSMITH_API_TOKEN"
_BASE_URL_ENV_VAR = "JOBSMITH_API_BASE_URL"
_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_TOKEN_FILE = Path.home() / ".jobsmith" / "token"


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Raised when no token is available or the server rejects it."""


class NotFoundError(Exception):
    """Raised when the server returns 404."""


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def _resolve_token(token: str | None) -> str:
    """Resolve an API token from constructor arg → env var → file → raise."""
    if token:
        return token
    env_token = os.environ.get(_TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token
    try:
        file_token = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if file_token:
            return file_token
    except OSError:
        pass
    raise AuthError(
        f"No API token found. Set {_TOKEN_ENV_VAR} env var or write token to {_TOKEN_FILE}"
    )


def _resolve_base_url(base_url: str | None) -> str:
    """Resolve the API base URL from constructor arg → env var → default."""
    if base_url:
        return base_url.rstrip("/")
    env_url = os.environ.get(_BASE_URL_ENV_VAR, "").strip()
    if env_url:
        return env_url.rstrip("/")
    return _DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _check_response(resp: httpx.Response) -> None:
    """Raise SDK-level errors for 401 and 404; re-raise others as httpx errors."""
    if resp.status_code == 401:
        raise AuthError(f"Authentication failed: {resp.text}")
    if resp.status_code == 404:
        raise NotFoundError(f"Not found: {resp.text}")
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# JobsmithClient
# ---------------------------------------------------------------------------


class JobsmithClient:
    """Read-only httpx client for the jobsmith HTTP API.

    Parameters
    ----------
    base_url:
        API base URL. Defaults to JOBSMITH_API_BASE_URL env or http://127.0.0.1:8000.
    token:
        Bearer token. Defaults to JOBSMITH_API_TOKEN env or ~/.jobsmith/token file.
    http_client:
        Optional pre-built httpx.Client (or fastapi.testclient.TestClient subclass).
        When provided, the SDK uses it directly and applies the Authorization header
        per-request rather than configuring it on the client.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._token = _resolve_token(token)
        self._base_url = _resolve_base_url(base_url)
        self._headers = {"Authorization": f"Bearer {self._token}"}
        if http_client is not None:
            self._http = http_client
            self._http.headers.update(self._headers)
            self._owns_client = False
        else:
            self._http = httpx.Client(base_url=self._base_url, headers=self._headers)
            self._owns_client = True

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> HealthResponse:
        """Check the API liveness endpoint. No auth required."""
        resp = self._http.get("/health")
        resp.raise_for_status()
        return HealthResponse.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Master reads
    # ------------------------------------------------------------------

    def get_master_work(self) -> list[WorkEntry]:
        """Return the work history list from /api/master/work."""
        resp = self._http.get("/api/master/work")
        _check_response(resp)
        return [WorkEntry.model_validate(item) for item in resp.json()]

    def get_master_skill(self) -> list[SkillEntry]:
        """Return the skill categories from /api/master/skill."""
        resp = self._http.get("/api/master/skill")
        _check_response(resp)
        return [SkillEntry.model_validate(item) for item in resp.json()]

    def get_master_education(self) -> list[EducationEntry]:
        """Return the education list from /api/master/education."""
        resp = self._http.get("/api/master/education")
        _check_response(resp)
        return [EducationEntry.model_validate(item) for item in resp.json()]

    def get_master_author(self) -> Author | None:
        """Return the author block from /api/master/author, or None."""
        resp = self._http.get("/api/master/author")
        _check_response(resp)
        data = resp.json()
        if data is None:
            return None
        return Author.model_validate(data)

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def list_artifacts(self, slug: str, run_id: str) -> list[ArtifactEnvelope]:
        """Return all specialist outputs for *slug* / *run_id*."""
        resp = self._http.get(f"/api/applications/{slug}/runs/{run_id}/artifacts")
        _check_response(resp)
        return [ArtifactEnvelope.model_validate(item) for item in resp.json()]

    def get_artifact(self, slug: str, run_id: str, kind: str) -> ArtifactEnvelope:
        """Return a single specialist output by *kind*. Raises NotFoundError if absent."""
        resp = self._http.get(f"/api/applications/{slug}/runs/{run_id}/artifacts/{kind}")
        _check_response(resp)
        return ArtifactEnvelope.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------

    def list_applications(self) -> list[Application]:
        """Return the latest run summary for each known slug."""
        resp = self._http.get("/api/applications")
        _check_response(resp)
        return [Application.model_validate(item) for item in resp.json()]

    def get_application(self, slug: str) -> ApplicationDetail:
        """Return the latest run + all artifacts for *slug*. Raises NotFoundError if absent."""
        resp = self._http.get(f"/api/applications/{slug}")
        _check_response(resp)
        return ApplicationDetail.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> JobsmithClient:
        return self

    def __exit__(self, *_: Any) -> None:
        if self._owns_client:
            self._http.close()

    def close(self) -> None:
        """Close the underlying HTTP client (no-op for injected clients)."""
        if self._owns_client:
            self._http.close()


__all__ = [
    "ArtifactEnvelope",
    "Application",
    "ApplicationDetail",
    "AuthError",
    "HealthResponse",
    "JobsmithClient",
    "NotFoundError",
]
