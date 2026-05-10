"""Pydantic schemas for the auth/user routes."""

from __future__ import annotations

from pydantic import BaseModel


class UserRecord(BaseModel):
    """Public-facing user profile returned by GET /api/auth/me."""

    user_id: str
    email: str
    name: str
    created_at: str
