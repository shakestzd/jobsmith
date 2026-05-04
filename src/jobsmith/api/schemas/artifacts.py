"""Pydantic models for artifact-related API responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ArtifactEnvelope(BaseModel):
    """One specialist output row, deserialised for API consumers."""

    run_id: str
    specialist: str
    kind: str
    output: dict[str, Any]
    finished_at: str | None
    transcript_ref: str | None


__all__ = ["ArtifactEnvelope"]
