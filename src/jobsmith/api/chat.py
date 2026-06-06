"""/api/chat router — global and per-application chat powered by claude headless.

Endpoints
---------
GET  /chat/history           Load message history for a slug (default: __global__).
POST /chat/send              Stream an SSE response to a user message.
POST /chat/session/reset     Clear the stored session UUID for a slug.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
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
# Proposal extraction
# ---------------------------------------------------------------------------

# Matches a fenced ```jobsmith-proposal ... ``` block (case-insensitive tag).
_PROPOSAL_RE = re.compile(
    r"```jobsmith-proposal\s*\n(?P<body>.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)


def extract_proposal(text: str) -> tuple[str, dict | None]:
    """Split *text* into (human_readable_text, proposal_dict | None).

    The agent is instructed to append a fenced ``jobsmith-proposal`` block
    containing JSON when (and only when) it proposes an asset edit.  This
    helper strips that block out of the visible/persisted text and returns the
    parsed JSON proposal (or None when absent / unparseable).

    On parse failure the original text is returned unchanged with None so the
    caller falls back to streaming the raw text.
    """
    match = _PROPOSAL_RE.search(text)
    if match is None:
        return text, None
    body = match.group("body").strip()
    try:
        proposal = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return text, None  # malformed → fall back to raw text
    if not isinstance(proposal, dict):
        return text, None
    clean = (text[: match.start()] + text[match.end() :]).strip()
    return clean, proposal


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

        messages = []
        for r in reversed(rows):
            content = r["text"]
            if r["role"] == "assistant":
                # Strip any persisted jobsmith-proposal block so reloaded
                # history shows only the human-readable summary, not raw JSON.
                content, _ = extract_proposal(content)
            messages.append(
                {"role": r["role"], "content": content, "created_at": r["created_at"]}
            )
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

    from jobsmith.api.claude_chat import ClaudeChatBackend

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

        # Accumulate the full assistant text so we can extract a trailing
        # jobsmith-proposal block.  We stream chunks live until the proposal
        # fence opener appears, then hold back the remainder (the raw JSON
        # block must never reach the user).  After the stream completes we
        # parse the buffered block and emit a dedicated ``proposal`` event.
        proposal_fence = "```jobsmith-proposal"
        full_text = ""
        proposal_started = False
        errored = False

        while True:
            item = await q.get()
            if item is None:
                break
            if "error" in item:
                yield ServerSentEvent(data=json.dumps({"error": item["error"]}))
                errored = True
                break

            chunk = item["chunk"]
            full_text += chunk

            if proposal_started:
                # We've already entered the proposal block — buffer silently.
                continue

            # Detect the fence opener spanning the accumulated text.  Once seen,
            # stream only the portion of *this chunk* that precedes the fence,
            # then suppress the rest of the stream.
            fence_idx = full_text.find(proposal_fence)
            if fence_idx == -1:
                yield ServerSentEvent(data=json.dumps({"chunk": chunk}))
            else:
                proposal_started = True
                visible_total = full_text[:fence_idx]
                already_streamed = len(full_text) - len(chunk)
                if already_streamed < len(visible_total):
                    tail = visible_total[already_streamed:]
                    if tail:
                        yield ServerSentEvent(data=json.dumps({"chunk": tail}))

        if not errored:
            clean_text, proposal = extract_proposal(full_text)
            if proposal is not None:
                # Normalise/validate minimally before emitting.
                payload = {
                    "asset": proposal.get("asset"),
                    "slug": proposal.get("slug") or slug,
                    "summary": proposal.get("summary", ""),
                    "rationale": proposal.get("rationale", ""),
                    "new_content": proposal.get("new_content", ""),
                }
                if payload["new_content"]:
                    yield ServerSentEvent(
                        data=json.dumps({"proposal": payload})
                    )

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


# Instructions appended to the slug-scoped system prompt that teach the
# (read-only) agent how to PROPOSE a cover-letter edit.  The agent never writes
# files; it emits a structured proposal that the UI renders as a diff and the
# user explicitly applies.
_PROPOSAL_INSTRUCTIONS = """\
## Editing the cover letter (propose-only)
You CANNOT write files. If and ONLY IF the user asks you to change, edit, \
rewrite, shorten, or otherwise revise the cover letter, do the following:
1. First write a short plain-text summary of what you changed and why.
2. THEN append a single fenced code block tagged `jobsmith-proposal` containing \
JSON with exactly these keys:
```jobsmith-proposal
{{"asset":"cover_letter","slug":"{slug}","summary":"<one-line summary>",\
"rationale":"<why these changes>","new_content":"<COMPLETE revised cover \
letter markdown>"}}
```
The `new_content` value MUST be the COMPLETE revised cover letter (the full \
document), not a partial excerpt and not a diff. Only make claims that are \
supported by the work.yml / cover letter content provided below — do not \
invent companies, numbers, dates, or metrics, as the proposal is fact-checked \
before it can be applied.
For normal questions (anything that is not a request to edit the cover \
letter), answer normally and do NOT emit a `jobsmith-proposal` block.
"""


def _resolve_work_yml(app_dir: Path) -> Path | None:
    """Resolve work.yml for an app, trying app_dir first, then documents/."""
    for candidate in (app_dir / "work.yml", app_dir / "documents" / "work.yml"):
        if candidate.is_file():
            return candidate
    return None


def _resolve_cover_letter_draft(app_dir: Path) -> Path | None:
    """Resolve cover-letter-draft.md, trying root, .apply-state/, then documents/.

    This mirrors the multi-location resolution used elsewhere in the codebase
    (e.g., _cover_letter_candidates in applications.py) to handle both new
    drafts and backfilled applications.
    """
    for candidate in (
        app_dir / "cover-letter-draft.md",
        app_dir / ".apply-state" / "cover-letter-draft.md",
        app_dir / "documents" / "cover-letter.md",
    ):
        if candidate.is_file():
            return candidate
    return None


def _build_system_prompt(slug: str) -> str | None:
    """Load work.yml and cover letter content for a slug to inject as context."""
    from jobsmith.api.applications import _get_app_dir

    app_dir = _get_app_dir(slug)
    if app_dir is None:
        return None

    # Resolve work.yml robustly: try root first, then documents/
    work_content = ""
    work_path = _resolve_work_yml(app_dir)
    if work_path is not None:
        with contextlib.suppress(OSError):
            work_content = work_path.read_text(encoding="utf-8")[:3000]

    # Include the FULL current cover-letter-draft.md so the agent can revise it
    # accurately (it must output the complete revised draft in new_content).
    # Resolve robustly: try root, .apply-state/, then documents/
    cover_content = ""
    cover_path = _resolve_cover_letter_draft(app_dir)
    if cover_path is not None:
        with contextlib.suppress(OSError):
            cover_content = cover_path.read_text(encoding="utf-8")

    return (
        f"Application: {slug}\n\n"
        f"## work.yml\n{work_content}\n\n"
        f"## Cover Letter (current cover-letter-draft.md)\n{cover_content}\n\n"
        + _PROPOSAL_INSTRUCTIONS.format(slug=slug)
    )


__all__ = ["router"]
