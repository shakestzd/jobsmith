"""Pydantic models for the /api/feedback endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class FeedbackRecord(BaseModel):
    """One feedback record returned by GET /api/feedback."""

    slug: str
    timestamp: str
    kind: str
    before: str
    after: str
    lesson: str
    context: dict[str, Any] | None = None


__all__ = ["FeedbackRecord"]
