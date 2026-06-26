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
from fastapi.responses import JSONResponse
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

# Local LLM backend detection for offline mode (feat-aaa91b6d, slice 7). Same
# JOBSMITH_DESKTOP gating + header auth; mounted alongside the others in
# api.main.create_app. REDUCED SCOPE: status only — the "enable offline mode"
# action that writes the pluggable-backend `llm` config is deferred to
# plan-938f735b (the POST below is a loud 501 placeholder that writes nothing).
llm_router = APIRouter(prefix="/desktop/llm", tags=["desktop"])


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


# --- Local LLM backend status (feat-aaa91b6d, slice 7) ---------------------


class LlmBackendStatus(BaseModel):
    reachable: bool
    base_url: str
    runtime_installed: bool
    model: str | None = None


class LlmStatus(BaseModel):
    mlx: LlmBackendStatus
    ollama: LlmBackendStatus


# Returned (with HTTP 501) by the deferred enable action so the UI degrades
# loudly instead of silently no-opping. The real offline-mode wiring (writing
# the pluggable-backend `llm` config + routing chat/scoring) lands in
# plan-938f735b.
_OFFLINE_MODE_PENDING = {
    "status": "unavailable",
    "reason": "offline backend config pending plan-938f735b",
}


@llm_router.get("/status", response_model=LlmStatus)
def llm_status(_user: UserRecord = Depends(current_user)) -> LlmStatus:
    """Report reachability + runtime presence for local MLX / Ollama backends."""
    return LlmStatus(**deps.llm_status())


@llm_router.post("/offline-mode")
def enable_offline_mode(_user: UserRecord = Depends(current_user)) -> JSONResponse:
    """Deferred placeholder for "enable offline mode".

    Writes NO config. Returns 501 + a clear pending reason so the desktop UI can
    surface "coming soon — pending plan-938f735b" instead of failing silently.
    The actual config write + chat/scoring routing is scoped to plan-938f735b.
    """
    return JSONResponse(status_code=501, content=_OFFLINE_MODE_PENDING)


__all__ = ["deps_router", "llm_router", "router"]
