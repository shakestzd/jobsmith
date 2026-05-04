"""JobsmithClient — httpx-based SDK for the jobsmith HTTP API.

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
    # Write an artifact (first write — no If-Match needed)
    envelope = client.put_jd_parsed("acme-swe", "run-abc123", {"company": "Acme"})
    # Overwrite with version check
    envelope2 = client.put_jd_parsed(
        "acme-swe", "run-abc123", {"company": "Acme Corp"}, if_match=1
    )
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
from jobsmith.api.schemas.snapshots import SnapshotResult

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


class ConflictError(Exception):
    """Raised when the server returns 409 (version mismatch on concurrent write)."""


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
    """Raise SDK-level errors for 401, 404, and 409; re-raise others as httpx errors."""
    if resp.status_code == 401:
        raise AuthError(f"Authentication failed: {resp.text}")
    if resp.status_code == 404:
        raise NotFoundError(f"Not found: {resp.text}")
    if resp.status_code == 409:
        raise ConflictError(f"Conflict: {resp.text}")
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

    def put_artifact(
        self,
        slug: str,
        run_id: str,
        kind: str,
        output: dict[str, Any],
        *,
        specialist: str = "api",
        transcript_ref: str | None = None,
        finished_at: str | None = None,
        if_match: int | None = None,
    ) -> ArtifactEnvelope:
        """Write a specialist output for *(slug, run_id, kind)*.

        Parameters
        ----------
        slug:
            Application slug (e.g. ``"acme-swe"``).
        run_id:
            Pipeline run identifier.
        kind:
            Artifact kind (must be in ``KIND_MODELS``).
        output:
            Typed payload dict, validated server-side against the kind's Pydantic model.
        specialist:
            Name of the writing agent. Defaults to ``"api"``.
        transcript_ref:
            Optional path to the specialist's transcript file.
        finished_at:
            Optional ISO-8601 completion timestamp.
        if_match:
            Current version for optimistic-concurrency check.  Required when
            a row for *(run_id, kind)* already exists; omit on first write.
            Raises :exc:`ConflictError` on mismatch.
        """
        headers: dict[str, str] = {}
        if if_match is not None:
            headers["If-Match"] = str(if_match)
        body = {
            "output": output,
            "specialist": specialist,
            "transcript_ref": transcript_ref,
            "finished_at": finished_at,
        }
        resp = self._http.put(
            f"/api/applications/{slug}/runs/{run_id}/artifacts/{kind}",
            json=body,
            headers=headers,
        )
        _check_response(resp)
        return ArtifactEnvelope.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Artifact convenience wrappers
    # ------------------------------------------------------------------

    def _put_kind(
        self,
        slug: str,
        run_id: str,
        kind: str,
        output: dict[str, Any],
        **kwargs: Any,
    ) -> ArtifactEnvelope:
        """Internal helper used by all per-kind wrappers."""
        return self.put_artifact(slug, run_id, kind, output, **kwargs)

    def put_jd_parsed(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``jd-parsed``."""
        return self._put_kind(slug, run_id, "jd-parsed", output, **kwargs)

    def put_fit_score(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``fit-score``."""
        return self._put_kind(slug, run_id, "fit-score", output, **kwargs)

    def put_bullet_selection(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``bullet-selection``."""
        return self._put_kind(slug, run_id, "bullet-selection", output, **kwargs)

    def put_hm_snippet(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``hm-snippet``."""
        return self._put_kind(slug, run_id, "hm-snippet", output, **kwargs)

    def put_prose_draft(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``prose-draft``."""
        return self._put_kind(slug, run_id, "prose-draft", output, **kwargs)

    def put_ai_tell_report(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``ai-tell-report``."""
        return self._put_kind(slug, run_id, "ai-tell-report", output, **kwargs)

    def put_ats_check(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``ats-check``."""
        return self._put_kind(slug, run_id, "ats-check", output, **kwargs)

    def put_company_research(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``company-research``."""
        return self._put_kind(slug, run_id, "company-research", output, **kwargs)

    def put_anchor_check(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``anchor-check``."""
        return self._put_kind(slug, run_id, "anchor-check", output, **kwargs)

    def put_fact_check(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``fact-check``."""
        return self._put_kind(slug, run_id, "fact-check", output, **kwargs)

    def put_cover_letter_draft(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``cover-letter-draft``."""
        return self._put_kind(slug, run_id, "cover-letter-draft", output, **kwargs)

    def put_variables(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``variables``."""
        return self._put_kind(slug, run_id, "variables", output, **kwargs)

    def put_quarto_config(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``quarto-config``."""
        return self._put_kind(slug, run_id, "quarto-config", output, **kwargs)

    def put_manifest(
        self, slug: str, run_id: str, output: dict[str, Any], **kwargs: Any
    ) -> ArtifactEnvelope:
        """PUT artifact for kind ``manifest``."""
        return self._put_kind(slug, run_id, "manifest", output, **kwargs)

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
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot_run(
        self,
        slug: str,
        run_id: str,
        *,
        kinds: list[str] | None = None,
        target: str = "both",
    ) -> SnapshotResult:
        """Materialise DB artifacts for *run_id* to canonical FS paths.

        Parameters
        ----------
        slug:
            Application slug (e.g. ``"acme-swe"``).
        run_id:
            Pipeline run identifier.
        kinds:
            Optional list of artifact kinds to snapshot. When None, all
            artifacts in the run are written.
        target:
            Which directory tree(s) to write to.  One of ``'apply-state'``,
            ``'slug-root'``, or ``'both'`` (default).

        Returns
        -------
        SnapshotResult
            Files written with absolute paths and byte counts.
        """
        body: dict[str, Any] = {"target": target}
        if kinds is not None:
            body["kinds"] = kinds
        resp = self._http.post(
            f"/api/applications/{slug}/runs/{run_id}/snapshot",
            json=body,
        )
        _check_response(resp)
        return SnapshotResult.model_validate(resp.json())

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
    "ConflictError",
    "HealthResponse",
    "JobsmithClient",
    "NotFoundError",
    "SnapshotResult",
]
