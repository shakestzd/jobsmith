"""Pydantic models for the /api/config endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class ValidationError(BaseModel):
    """One validation error from POST /api/config/validate or PUT /api/config."""

    field: str
    message: str


class ConfigValidateResponse(BaseModel):
    """Response body for POST /api/config/validate."""

    ok: bool
    errors: list[ValidationError]


__all__ = ["ConfigValidateResponse", "ValidationError"]
