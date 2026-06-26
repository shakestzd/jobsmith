"""Desktop-only Playwright Chromium management router (feat-0c74180d, slice 4).

Mounted in :mod:`jobsmith.api.main` ONLY when ``JOBSMITH_DESKTOP=1`` (set by the
desktop sidecar). On a normal server these routes do not exist, so
``GET /api/desktop/browser/status`` returns 404 — the Goal-4 regression guard.

Endpoints (the ``/api`` prefix is applied at include time)::

    GET  /api/desktop/browser/status          installed?: bool, path
    POST /api/desktop/browser/install         kick off a one-time download
    GET  /api/desktop/browser/install/events  SSE progress stream

Auth mirrors the rest of the API: header routes use ``current_user``; the SSE
route uses ``current_user_or_query`` because a browser ``EventSource`` cannot
set an Authorization header (see how ``events_router`` is wired in main.py).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from jobsmith.api.auth import current_user, current_user_or_query
from jobsmith.api.schemas.auth import UserRecord
from jobsmith.desktop import deps
from jobsmith.desktop import playwright_bootstrap as pw

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/desktop/browser", tags=["desktop"])

# Separate router for external-tool dependency detection (feat-dac00175,
# slice 6). Same JOBSMITH_DESKTOP gating + header auth as the browser routes;
# mounted alongside `router` in api.main.create_app. Slice 7 will extend the
# deps probe (LLM runtime) behind this same endpoint.
deps_router = APIRouter(prefix="/desktop/deps", tags=["desktop"])


class BrowserStatus(BaseModel):
    installed: bool
    path: str


class InstallAck(BaseModel):
    # "started" | "in_progress" | "already_installed"
    status: str


@router.get("/status", response_model=BrowserStatus)
def browser_status(_user: UserRecord = Depends(current_user)) -> BrowserStatus:
    """Report whether Chromium is present in the app-data browsers dir."""
    snapshot = pw.status()
    return BrowserStatus(installed=snapshot["installed"], path=snapshot["path"])


@router.post("/install", response_model=InstallAck)
async def browser_install(_user: UserRecord = Depends(current_user)) -> InstallAck:
    """Kick off a one-time Chromium download (single-flight, non-blocking)."""
    state = await pw.get_installer().ensure_started()
    return InstallAck(status=state)


@router.get("/install/events")
async def browser_install_events(
    request: Request,
    _user: UserRecord = Depends(current_user_or_query),
) -> EventSourceResponse:
    """Stream Chromium download progress as Server-Sent Events."""
    installer = pw.get_installer()

    async def _gen():
        async for event in installer.subscribe():
            if await request.is_disconnected():
                break
            yield ServerSentEvent(data=json.dumps(event), event="progress")

    return EventSourceResponse(_gen())


class DepsStatus(BaseModel):
    claude_installed: bool
    version: str | None = None
    path: str | None = None


@deps_router.get("/status", response_model=DepsStatus)
def deps_status(_user: UserRecord = Depends(current_user)) -> DepsStatus:
    """Report whether the ``claude`` Claude Code CLI is available on PATH."""
    snapshot = deps.claude_status()
    return DepsStatus(
        claude_installed=snapshot["installed"],
        version=snapshot["version"],
        path=snapshot["path"],
    )


__all__ = ["deps_router", "router"]
