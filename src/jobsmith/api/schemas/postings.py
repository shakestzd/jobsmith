"""Pydantic models for the /api/postings endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class PostingRow(BaseModel):
    """One posting row returned by GET /api/postings.

    jd_text is intentionally excluded from the list response (can be large;
    only needed when rendering a single posting detail).
    """

    id: int
    source: str
    external_id: str | None = None
    url: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    comp_text: str | None = None
    posted_date: str | None = None
    fast_score: float | None = None
    llm_score: float | None = None
    specialty: str | None = None
    rationale: str | None = None
    status: str
    promoted_application_id: str | None = None
    dedup_key: str
    first_seen_at: str
    last_seen_at: str


class PostingStatusUpdate(BaseModel):
    """Body for POST /api/postings/{id}/status."""

    status: str


class PostingPromoteResponse(BaseModel):
    """Response from POST /api/postings/{id}/promote."""

    run_id: str
    slug: str | None = None
    jd_fetch_failed: bool = False
    # True when the apply pipeline was launched by this promote
    # (bug-fa863c68); False on repeat promotes or launch failure.
    launched: bool = False


__all__ = ["PostingRow", "PostingStatusUpdate", "PostingPromoteResponse"]
