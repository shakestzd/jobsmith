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
    ui_phase: str
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
    ui_phase: str
    started_at: str | None
    finished_at: str | None
    role: str | None = None
    company: str | None = None
    # The original job-description URL stored in the jd-parsed artifact.
    # Null when the run predates URL persistence or jd-parsed is absent.
    apply_url: str | None = None
    artifacts: list[ArtifactEnvelope]


class ApplicationCreate(BaseModel):
    """Request body for POST /applications."""

    url: str
    slug: str | None = None
    # When true, the launched `jobsmith apply` is invoked with --force so it
    # restarts the pipeline from phase 1 even if prior artifacts exist for
    # this slug. Required to re-run any application that already completed.
    force: bool = False
    # Pasted job-description text (frontend "paste text" mode). When non-empty
    # the supervisor writes it to a temp file and passes --jd-text-file so the
    # apply pipeline uses it instead of fetching ``url`` (needed for JS-rendered
    # ATS portals like Microsoft Eightfold). bug-1c800e09.
    jd_text: str | None = None
    # When set to "gather", "draft", or "render", treat all earlier phases as
    # complete and run only from the named phase onward. Lets callers re-run
    # a single phase without --force resetting the whole pipeline.
    start_from_phase: str | None = None
    # When False, skip apply-company-research and apply-cover-letter-writer.
    # When None (default), the server resolves via config (cover_letter.framework).
    # When True, generate the cover letter regardless of config.
    cover_letter: bool | None = None


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
