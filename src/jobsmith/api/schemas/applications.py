"""Pydantic models for the applications API endpoints."""

from __future__ import annotations

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
    role: str | None = None
    company: str | None = None


class ApplicationDetail(BaseModel):
    """Full application detail: latest run metadata + latest artifacts."""

    slug: str
    run_id: str
    phase: str
    status: str
    started_at: str | None
    finished_at: str | None
    role: str | None = None
    company: str | None = None
    artifacts: list[ArtifactEnvelope]


class ApplicationCreate(BaseModel):
    """Request body for POST /applications."""

    url: str
    slug: str | None = None


class ApplicationCreated(BaseModel):
    """Response body for POST /applications (201 Created)."""

    slug: str
    run_id: str


__all__ = [
    "Application",
    "ApplicationCreate",
    "ApplicationCreated",
    "ApplicationDetail",
    "ArtifactEnvelope",
]
