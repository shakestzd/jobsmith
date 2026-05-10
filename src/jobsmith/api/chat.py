"""/api/chat router — global and per-application chat powered by claude headless.

Endpoints
---------
GET  /chat/history           Load message history for a slug (default: __global__).
POST /chat/send              Stream an SSE response to a user message.
POST /chat/session/reset     Clear the stored session UUID for a slug.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    from jobsmith.config import find_config

    repo_root_env = os.environ.get("JOBSMITH_REPO_ROOT", "").strip()
    search_start = Path(repo_root_env).resolve() if repo_root_env else Path.cwd()
    config_path = find_config(search_start)
    return config_path.parent if config_path else search_start


def _review_db_dir() -> Path:
    return _project_root() / "private" / ".review"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/chat/history")
def get_chat_history(
    slug: Annotated[str, Query()] = "__global__",
    limit: Annotated[int, Query()] = 50,
) -> dict:
    """Return recent chat messages for *slug*.

    Returns ``{"messages": [...], "session_id": str | null}``.
    Falls back to empty list + null session_id if the DB does not exist yet.
    """
    try:
        from jobsmith.db import open_review_db

        conn = open_review_db(slug, _review_db_dir())
        try:
            rows = conn.execute(
                "SELECT role, text, created_at FROM chat_messages "
                "WHERE slug=? ORDER BY created_at DESC LIMIT ?",
                (slug, limit),
            ).fetchall()
            session_row = conn.execute(
                "SELECT session_uuid FROM chat_sessions WHERE slug=? "
                "ORDER BY rowid DESC LIMIT 1",
                (slug,),
            ).fetchone()
        finally:
            conn.close()

        messages = [
            {"role": r["role"], "content": r["text"], "created_at": r["created_at"]}
            for r in reversed(rows)
        ]
        session_id: str | None = session_row["session_uuid"] if session_row else None
        return {"messages": messages, "session_id": session_id}
    except Exception:  # noqa: BLE001 — DB may not exist yet; return empty
        return {"messages": [], "session_id": None}


class ChatSendRequest(BaseModel):
    message: str
    slug: str | None = None


@router.post("/chat/send")
async def send_chat_message(body: ChatSendRequest) -> EventSourceResponse:
    """Stream an SSE response to *message*.

    Yields ``data: {"chunk": "..."}`` events as Claude streams its reply,
    followed by ``data: {"done": true, "session_id": "..."}`` on completion.
    """
    slug = body.slug if body.slug is not None else "__global__"
    message = body.message

    # Build context-aware system prompt for non-global slugs.
    system_prompt: str | None = None
    if slug != "__global__":
        system_prompt = _build_system_prompt(slug)

    from jobsmith.marimo.claude_chat import ClaudeChatBackend

    backend = ClaudeChatBackend(
        slug=slug,
        project_root=_project_root(),
        review_db_dir=_review_db_dir(),
        system_prompt=system_prompt,
    )

    async def generator():
        yield ServerSentEvent(comment="connected")

        loop = asyncio.get_event_loop()
        q: asyncio.Queue[dict | None] = asyncio.Queue()

        def producer() -> None:
            try:
                for chunk in backend.send(message):
                    asyncio.run_coroutine_threadsafe(
                        q.put({"chunk": chunk}), loop
                    ).result()
            except Exception as exc:  # noqa: BLE001
                asyncio.run_coroutine_threadsafe(
                    q.put({"error": str(exc)}), loop
                ).result()
            finally:
                asyncio.run_coroutine_threadsafe(q.put(None), loop).result()

        threading.Thread(target=producer, daemon=True).start()

        while True:
            item = await q.get()
            if item is None:
                break
            if "error" in item:
                yield ServerSentEvent(data=json.dumps({"error": item["error"]}))
                break
            yield ServerSentEvent(data=json.dumps({"chunk": item["chunk"]}))

        yield ServerSentEvent(
            data=json.dumps({"done": True, "session_id": backend.session_id})
        )

    return EventSourceResponse(generator())


class ChatResetRequest(BaseModel):
    slug: str | None = None


@router.post("/chat/session/reset")
def reset_chat_session(body: ChatResetRequest) -> dict:
    """Clear the stored session UUID for *slug* so the next send starts fresh."""
    slug = body.slug if body.slug is not None else "__global__"
    try:
        from jobsmith.db import open_review_db

        conn = open_review_db(slug, _review_db_dir())
        try:
            conn.execute("DELETE FROM chat_sessions WHERE slug=?", (slug,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — DB may not exist; treat as already reset
        pass
    return {"ok": True}


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def _build_system_prompt(slug: str) -> str | None:
    """Load work.yml and cover letter content for a slug to inject as context."""
    from jobsmith.api.applications import _get_app_dir

    app_dir = _get_app_dir(slug)
    if app_dir is None:
        return None

    work_content = ""
    work_yml = app_dir / "work.yml"
    if work_yml.exists():
        try:
            work_content = work_yml.read_text(encoding="utf-8")[:3000]
        except OSError:
            pass

    cover_content = ""
    docs_dir = app_dir / "documents"
    if docs_dir.exists():
        for suffix in (".md", ".txt"):
            matches = sorted(
                (f for f in docs_dir.iterdir() if f.name.lower().startswith("cover") and f.suffix == suffix),
                key=lambda p: p.name,
            )
            if matches:
                try:
                    cover_content = matches[0].read_text(encoding="utf-8")[:2000]
                except OSError:
                    pass
                break

    return (
        f"Application: {slug}\n\n"
        f"## work.yml\n{work_content}\n\n"
        f"## Cover Letter\n{cover_content}"
    )


__all__ = ["router"]
