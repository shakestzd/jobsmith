"""Pydantic models for the /api/doctor endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DoctorCheckResult(BaseModel):
    """One preflight check result returned by GET /api/doctor."""

    name: str
    status: Literal["pass", "warn", "fail"]
    message: str


__all__ = ["DoctorCheckResult"]
