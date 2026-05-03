"""Typed Pydantic models for ``specialist_outputs.output_json`` rows.

Each pipeline specialist writes a JSON artifact in ``.apply-state/``; the
ingest hook serialises that artifact into the ``output_json`` column.  This
module owns the *shape* of those artifacts so that ``deserialize_output``
returns a typed model regardless of caller.

Adding a new specialist
-----------------------
1. Add a Pydantic class describing the output shape.
2. Register it in ``KIND_MODELS`` under the kind string.
3. Add the artifact filename → (kind, reader) entry to
   ``jobsmith._state_readers.ARTIFACT_READERS`` so the ingest hook knows
   how to load the file.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel, Field


class JDParsed(BaseModel):
    """kind=jd-parsed."""

    company: str | None = None
    position: str | None = None
    location: str | None = None
    location_type: str | None = None
    salary_range: str | None = None
    req_id: str | None = None
    apply_url: str | None = None
    role_type: str | None = None
    jd_text_clean: str | None = None
    must_haves: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    top_keywords: list[str] = Field(default_factory=list)


class FitScore(BaseModel):
    """kind=fit-score."""

    score: float | None = None
    score_raw: float | None = None
    rationale: str | None = None
    specialty: str | None = None
    confidence: str | None = None
    must_have_table: list[dict[str, Any]] = Field(default_factory=list)
    matched_evidence: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    pitch: str | None = None


class BulletSelection(BaseModel):
    """kind=bullet-selection."""

    positions: list[dict[str, Any]] = Field(default_factory=list)
    anchor_bullets_master: list[Any] = Field(default_factory=list)
    anchor_bullets_kept: list[Any] = Field(default_factory=list)
    anchor_bullets_dropped: list[Any] = Field(default_factory=list)


class HMSnippet(BaseModel):
    """kind=hm-snippet."""

    detected: bool = False
    name: str | None = None
    source: str | None = None
    one_specific_signal: str | None = None
    suggested_hook: str | None = None


class TextArtifact(BaseModel):
    """Generic text-only artifact (prose-draft, company-research, outreach-snippets)."""

    text: str | None = None


class AITellReport(BaseModel):
    """kind=ai-tell-report."""

    iterations: list[dict[str, Any]] = Field(default_factory=list)


class ATSCheck(BaseModel):
    """kind=ats-check."""

    score: float | None = None
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


#: Maps the ``kind`` column to the Pydantic class used by ``deserialize_output``.
KIND_MODELS: dict[str, type[BaseModel]] = {
    "jd-parsed": JDParsed,
    "fit-score": FitScore,
    "bullet-selection": BulletSelection,
    "hm-snippet": HMSnippet,
    "prose-draft": TextArtifact,
    "ai-tell-report": AITellReport,
    "ats-check": ATSCheck,
    "company-research": TextArtifact,
    "outreach-snippets": TextArtifact,
}


def deserialize_output(row: sqlite3.Row) -> BaseModel:
    """Deserialise a ``specialist_outputs`` row into a typed model.

    Falls back to :class:`TextArtifact` for unknown kinds so callers never
    need to handle ``None``.
    """
    model_cls = KIND_MODELS.get(row["kind"], TextArtifact)
    return model_cls.model_validate_json(row["output_json"])


__all__ = [
    "AITellReport",
    "ATSCheck",
    "BulletSelection",
    "FitScore",
    "HMSnippet",
    "JDParsed",
    "KIND_MODELS",
    "TextArtifact",
    "deserialize_output",
]
