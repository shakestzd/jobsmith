"""ClaudeChatBackend — headless Claude CLI chat with streaming and session persistence.

Port from moplan/plugin/notebooks/claude_chat.py, adapted for jobsmith:
- subprocess cwd = project_root (NOT the slug dir)
- system_prompt wrapped in <context>...</context> XML tags
- session UUID persisted per-slug in per-slug review DB (private/.review/<slug>.db)
- retry logic inspects stderr for 'unknown option'/'unrecognized' before retrying
- on --append-system-prompt flag-error: fallback to embedding context as first user turn
  (does NOT retry same command — avoids infinite loop)
- Anthropic SDK fallback when claude not on PATH and ANTHROPIC_API_KEY is set

Pure logic module — no marimo imports. Fully testable standalone.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

from jobsmith.db import (
    insert_chat_message,
    insert_chat_session,
    open_review_db,
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class ClaudeChatBackend:
    """Headless Claude chat backend using subprocess or Anthropic API fallback.

    Session persistence: session UUID is stored per-slug in the per-slug review DB
    so it survives notebook restarts. The subprocess cwd is set to project_root
    (NOT the slug dir) so Claude Code binds the session to the actual project.

    Prompt injection defense: system_prompt is wrapped in <context>...</context>
    XML tags and injected via --append-system-prompt, not the user message.

    Flag-error fallback: if the claude CLI does not support --append-system-prompt
    (detected by "unknown option"/"unrecognized" in stderr), the context is embedded
    as the first user turn instead. This path does NOT retry with --append-system-prompt
    to avoid an infinite loop.
    """

    def __init__(
        self,
        *,
        slug: str,
        project_root: Path,
        review_db_dir: Path,
        system_prompt: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.slug = slug
        self.project_root = Path(project_root)
        self.review_db_dir = Path(review_db_dir)
        self.system_prompt = system_prompt
        self._session_id: str | None = session_id
        # When True, --append-system-prompt is not supported by the installed claude CLI
        self._append_prompt_unsupported: bool = False
        # Set by start_new_session() so the notebook can surface a banner
        # ("Resume updated — new chat session started.") and clear it after.
        self._session_restarted_banner: bool = False

        if session_id is None:
            self._load_session_id()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def start_new_session(self) -> None:
        """End the current chat session and start fresh.

        Called by Finalize (slice 7) so the next chat turn sees the
        post-Finalize work.yml / cover-letter state instead of resuming
        a session that was reasoning about the pre-Finalize content.
        Sets the banner flag so the notebook can render
        "Resume updated — new chat session started."
        """
        self._session_id = None
        self._session_restarted_banner = True

    def consume_restart_banner(self) -> bool:
        """Return + clear the session-restart banner flag.

        Used by the notebook's sidebar cell to display the banner exactly
        once after a Finalize.
        """
        flag = self._session_restarted_banner
        self._session_restarted_banner = False
        return flag

    @staticmethod
    def is_available() -> tuple[bool, str]:
        """Check if claude CLI is on PATH."""
        path = shutil.which("claude")
        if path:
            return True, f"claude CLI found at {path}"
        return (
            False,
            "claude CLI not found on PATH. Install from https://claude.ai/download.",
        )

    @staticmethod
    def has_api_fallback() -> bool:
        """Check if ANTHROPIC_API_KEY is set for API fallback."""
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    def send(self, user_msg: str) -> Generator[str, None, None]:
        """Yield text chunks from Claude's response.

        Writes user message to the review DB before invoking Claude.
        Writes the full assistant response after the generator completes.

        Raises RuntimeError if neither claude CLI nor API fallback is available.
        """
        available, _ = self.is_available()
        if available:
            yield from self._invoke_subprocess(user_msg)
        elif self.has_api_fallback():
            yield from self._invoke_sdk(user_msg)
        else:
            raise RuntimeError(
                "claude CLI not found and ANTHROPIC_API_KEY is not set. "
                "Cannot send message. Install claude CLI or set ANTHROPIC_API_KEY."
            )

    # ------------------------------------------------------------------
    # Subprocess path
    # ------------------------------------------------------------------

    def _build_system_prompt_arg(self) -> str:
        """Wrap system_prompt in <context> XML tags."""
        if not self.system_prompt:
            return "<context>\n</context>"
        return f"<context>\n{self.system_prompt}\n</context>"

    def _build_cmd(self, message: str, *, embed_context_in_message: bool = False) -> list[str]:
        """Build the claude CLI command argv."""
        claude_path = shutil.which("claude")
        if not claude_path:
            raise RuntimeError("claude CLI not on PATH")

        # When context is embedded as first user turn, prepend it to the message
        if embed_context_in_message and self.system_prompt:
            full_message = (
                f"<context>\n{self.system_prompt}\n</context>\n\n{message}"
            )
        else:
            full_message = message

        cmd: list[str] = [
            claude_path,
            "-p",
            full_message,
            "--output-format",
            "stream-json",
            "--verbose",
        ]

        if not embed_context_in_message:
            cmd += ["--append-system-prompt", self._build_system_prompt_arg()]

        if self._session_id:
            cmd += ["--resume", self._session_id]

        return cmd

    @staticmethod
    def _read_stream(proc: subprocess.Popen, output_queue: queue.Queue) -> None:
        """Background thread: drain proc.stdout line-by-line into the queue."""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                stripped = line.strip()
                if stripped:
                    output_queue.put(stripped)
        finally:
            output_queue.put(None)  # sentinel — stream ended

    def _invoke_subprocess(self, user_msg: str) -> Generator[str, None, None]:
        """Invoke claude CLI and stream text deltas.

        Handles:
        - Session UUID capture from type=system,subtype=init event
        - Stale-resume retry (non-zero exit + "session not found" in stderr)
        - --append-system-prompt flag-error fallback to embedded first user turn
        - Persistence: writes user+assistant rows to review DB
        """
        # Persist user message before invoking
        self._persist_message(role="user", text=user_msg)

        embed_ctx = self._append_prompt_unsupported
        cmd = self._build_cmd(user_msg, embed_context_in_message=embed_ctx)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(self.project_root),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Failed to start claude CLI: {exc}") from exc

        output_queue: queue.Queue = queue.Queue()
        reader = threading.Thread(
            target=self._read_stream, args=(proc, output_queue), daemon=True
        )
        reader.start()

        full_response = ""
        timed_out = False

        while True:
            try:
                line = output_queue.get(timeout=60)
            except queue.Empty:
                proc.terminate()
                timed_out = True
                break

            if line is None:
                break  # stream ended normally

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip non-JSON lines

            event_type = event.get("type", "")

            if event_type == "system" and event.get("subtype") == "init":
                sid = event.get("session_id") or event.get("sessionId")
                if sid and not self._session_id:
                    self._session_id = sid
                    self._save_session_id()

            elif event_type == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            full_response += text
                            yield text

            elif event_type == "result":
                break  # conversation turn complete

        proc.wait()
        stderr_output = proc.stderr.read() if proc.stderr else ""

        # Handle --append-system-prompt flag not supported by installed claude CLI
        if proc.returncode != 0 and not embed_ctx:
            stderr_lower = stderr_output.lower()
            if "unknown option" in stderr_lower or "unrecognized" in stderr_lower:
                # Flag unsupported: switch to context-in-message mode, do NOT retry same cmd
                self._append_prompt_unsupported = True
                self._session_id = None  # start fresh session without the bad flag
                yield from self._invoke_subprocess(user_msg)
                return

        # Handle stale --resume (session not found)
        if proc.returncode != 0 and self._session_id and not timed_out:
            stderr_lower = stderr_output.lower()
            if "session not found" in stderr_lower or "not found" in stderr_lower:
                self._session_id = None
                self._save_session_id()
                yield from self._invoke_subprocess(user_msg)
                return

        if timed_out:
            raise RuntimeError("claude CLI timed out after 60 seconds with no output.")

        # Persist assistant response
        if full_response:
            self._persist_message(role="assistant", text=full_response)

    # ------------------------------------------------------------------
    # SDK fallback path
    # ------------------------------------------------------------------

    def _invoke_sdk(self, user_msg: str) -> Generator[str, None, None]:
        """Best-effort Anthropic API fallback when claude CLI is unavailable.

        Session continuity is not supported via the stateless API path.
        Persists user + assistant messages to review DB.
        """
        try:
            import anthropic  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "anthropic Python package is not available. "
                "Run: uv pip install anthropic"
            ) from exc

        self._persist_message(role="user", text=user_msg)

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        client = anthropic.Anthropic(api_key=api_key)

        system = self._build_system_prompt_arg()
        full_response = ""

        with client.messages.stream(
            model="claude-opus-4-5",
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            for text in stream.text_stream:
                full_response += text
                yield text

        if full_response:
            self._persist_message(role="assistant", text=full_response)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _get_review_conn(self):
        """Open the per-slug review DB connection."""
        return open_review_db(self.slug, self.review_db_dir)

    def _load_session_id(self) -> None:
        """Load stored session UUID from review DB, if any."""
        try:
            conn = self._get_review_conn()
            row = conn.execute(
                "SELECT session_uuid FROM chat_sessions WHERE slug=? "
                "ORDER BY rowid DESC LIMIT 1",
                (self.slug,),
            ).fetchone()
            conn.close()
            if row:
                self._session_id = row["session_uuid"]
        except Exception:  # noqa: BLE001
            self._session_id = None

    def _save_session_id(self) -> None:
        """Persist current session UUID to review DB (idempotent)."""
        if not self._session_id:
            return
        try:
            conn = self._get_review_conn()
            insert_chat_session(conn, self.slug, self._session_id)
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _persist_message(self, *, role: str, text: str) -> None:
        """Append a chat_messages row to the review DB."""
        try:
            conn = self._get_review_conn()
            insert_chat_message(conn, slug=self.slug, role=role, text=text, created_at=_now_iso())
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def load_history(self) -> list[dict]:
        """Load all chat messages for this slug from the review DB.

        Returns list of dicts with keys: role, text, created_at.
        """
        try:
            conn = self._get_review_conn()
            rows = conn.execute(
                "SELECT role, text, created_at FROM chat_messages WHERE slug=? ORDER BY created_at",
                (self.slug,),
            ).fetchall()
            conn.close()
            return [{"role": r["role"], "content": r["text"], "created_at": r["created_at"]}
                    for r in rows]
        except Exception:  # noqa: BLE001
            return []


__all__ = ["ClaudeChatBackend"]
