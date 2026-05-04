"""Pydantic models for the applications API endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .artifacts import ArtifactEnvelope


class Application(BaseModel):
    """Summary row from apply_runs — one entry per unique slug (latest run)."""

    slug: str
    run_id: str
    phase: str
    status: str
    started_at: str | None
    finished_at: str | None


class ApplicationDetail(BaseModel):
    """Full application detail: latest run metadata + latest artifacts."""

    slug: str
    run_id: str
    phase: str
    status: str
    started_at: str | None
    finished_at: str | None
    artifacts: list[ArtifactEnvelope]


# ---------------------------------------------------------------------------
# Create endpoint schemas (feat-3c354917)
# ---------------------------------------------------------------------------


class CreateApplicationRequest(BaseModel):
    """Body for POST /api/applications.

    Exactly one of jd_url, jd_text, or jd_file_b64 must be set.
    """

    jd_url: str | None = None
    jd_text: str | None = None
    jd_file_b64: str | None = None
    skip_confirmations: bool = True
    force: bool = False
    verbosity: Literal["-v", "-vv", "-vvv"] = "-v"


class CreateApplicationResponse(BaseModel):
    """Response body for a successful POST /api/applications (201)."""

    slug: str
    run_id: str
    events_url: str


# ---------------------------------------------------------------------------
# Re-run endpoint schemas (feat-3c354917)
# ---------------------------------------------------------------------------


class RerunRequest(BaseModel):
    """Body for POST /api/applications/{slug}/run.  All fields optional."""

    force: bool = False
    verbosity: Literal["-v", "-vv", "-vvv"] = "-v"


class RerunResponse(BaseModel):
    """Response body for a successful re-run request (202)."""

    slug: str
    run_id: str
    events_url: str


class RerunConflictResponse(BaseModel):
    """Embedded in 409 detail when a run is already in progress."""

    slug: str
    run_id: str
    status: str
    events_url: str


__all__ = [
    "Application",
    "ApplicationDetail",
    "ArtifactEnvelope",
    "CreateApplicationRequest",
    "CreateApplicationResponse",
    "RerunConflictResponse",
    "RerunRequest",
    "RerunResponse",
]
