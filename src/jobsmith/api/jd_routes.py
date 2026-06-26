"""GET /api/jd/fetch — browser-backed JD text retrieval."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from jobsmith.api.auth import current_user
from jobsmith.api.schemas.auth import UserRecord
from jobsmith.jd.fetcher import FetchMethod, fetch_jd

router = APIRouter(prefix="/jd", tags=["jd"])


class JdFetchResponse(BaseModel):
    text: str
    method: FetchMethod
    char_count: int
    # feat-0c74180d: True when the Playwright/Chromium fallback could not run.
    # The request still succeeds (httpx text is returned); detail is a clear,
    # retryable message the UI can surface ("install browser and retry").
    browser_unavailable: bool = False
    detail: str | None = None


@router.get("/fetch", response_model=JdFetchResponse)
async def fetch_jd_endpoint(
    url: str = Query(..., description="Job posting URL to fetch"),
    _user: UserRecord = Depends(current_user),
) -> JdFetchResponse:
    """Fetch JD text via httpx (fast) or Playwright (JS-rendered fallback)."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    try:
        result = await fetch_jd(url)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JdFetchResponse(
        text=result.text,
        method=result.method,
        char_count=len(result.text),
        browser_unavailable=result.browser_unavailable,
        detail=result.detail,
    )


__all__ = ["router"]
