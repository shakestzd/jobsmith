"""/api/feedback router — list feedback records.

Endpoints
---------
GET /feedback   List feedback records, optionally filtered by kind and since.

The endpoint is idempotent (read-only) so GET is the correct verb.
Auth is enforced via the top-level include_router dependency in main.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from jobsmith.feedback import list_records

from .schemas.feedback import FeedbackRecord

router = APIRouter(tags=["feedback"])


@router.get("/feedback", response_model=list[FeedbackRecord])
def get_feedback(
    kind: Annotated[str | None, Query(description="Filter by feedback kind")] = None,
    since: Annotated[
        datetime | None,
        Query(description="Only return records at or after this datetime"),
    ] = None,
) -> list[FeedbackRecord]:
    """List feedback records.

    Optionally filters by ``kind`` and/or ``since``.
    Returns HTTP 200 with the full list (may be empty).
    """
    records = list_records(kind, since)
    return [FeedbackRecord(**r) for r in records]


__all__ = ["router"]
