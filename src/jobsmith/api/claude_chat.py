"""Pluggable chat backends — Claude CLI plus alternative LLM providers.

This module exposes :class:`BaseChatBackend` (shared session persistence +
streaming contract) and four concrete providers, one per ``config.llm.provider``
value:

* :class:`ClaudeChatBackend` — ``claude_cli`` (DEFAULT, unchanged behavior).
* :class:`AntigravityCliProvider` — ``antigravity_cli`` (``agy -p``).
* :class:`CodexCliProvider` — ``codex_cli`` (``codex exec --json``).
* :class:`OpenAICompatibleProvider` — ``openai_compatible`` (MLX / Ollama / LM
  Studio / llama.cpp via :class:`jobsmith.llm.openai_compat.OpenAICompatClient`).

Every provider implements the SAME ``send(user_msg) -> Generator[str, None, None]``
contract — a stream of assistant text deltas — regardless of whether the
underlying transport parses subprocess JSON lines, plain-text print output, or
HTTP SSE chunks. Session continuity and per-slug chat history are persisted in
the per-slug review DB by the shared base class.

Backward-compatibility guarantee: ``claude_cli`` (the default) reproduces the
prior ClaudeChatBackend behavior exactly — system prompt wrapped in
``<context>`` tags via ``--append-system-prompt``, stream-json parsing, stale
``--resume`` retry, the flag-error first-user-turn fallback, and the Anthropic
SDK fallback.
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


# ===========================================================================
# Base backend — shared persistence, session handling, streaming contract
# ===========================================================================


class BaseChatBackend:
    """Shared chat-backend machinery: persistence, sessions, the send contract.

    Subclasses implement provider-specific streaming. There are two extension
    shapes:

    * Override :meth:`_stream` and inherit the default :meth:`send`
      (persist user → stream deltas → persist assistant). Used by the
      Antigravity, Codex and OpenAI-compatible providers.
    * Override :meth:`send` entirely for bespoke flows (Claude does this to
      preserve its retry / fallback / persistence semantics exactly).

    Session persistence: the provider session/conversation id is stored per-slug
    in the per-slug review DB (``chat_sessions``) so it survives restarts. The
    subprocess cwd (for CLI providers) is ``project_root``, NOT the slug dir.
    """

    def __init__(
        self,
        *,
        slug: str,
        project_root: Path,
        review_db_dir: Path,
        system_prompt: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: int = 300,
    ) -> None:
        self.slug = slug
        self.project_root = Path(project_root)
        self.review_db_dir = Path(review_db_dir)
        self.system_prompt = system_prompt
        self._session_id: str | None = session_id
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_s = timeout_s
        # Set by start_new_session() so the UI can surface a one-shot banner.
        self._session_restarted_banner: bool = False

        if session_id is None:
            self._load_session_id()

    # ------------------------------------------------------------------
    # Public API / session lifecycle
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def start_new_session(self) -> None:
        """End the current session and start fresh (sets the one-shot banner)."""
        self._session_id = None
        self._session_restarted_banner = True

    def consume_restart_banner(self) -> bool:
        """Return + clear the session-restart banner flag."""
        flag = self._session_restarted_banner
        self._session_restarted_banner = False
        return flag

    @staticmethod
    def is_available() -> tuple[bool, str]:
        """Whether this backend can run. Overridden by every provider."""
        raise NotImplementedError

    def send(self, user_msg: str) -> Generator[str, None, None]:
        """Default turn: persist user, stream deltas, persist assistant.

        Providers with stateless or single-shot transports (Antigravity, Codex,
        OpenAI-compatible) inherit this. Claude overrides it.
        """
        self._persist_message(role="user", text=user_msg)
        full_response = ""
        for delta in self._stream(user_msg):
            full_response += delta
            yield delta
        if full_response:
            self._persist_message(role="assistant", text=full_response)

    def _stream(self, user_msg: str) -> Generator[str, None, None]:
        """Provider-specific text-delta stream. Overridden by subclasses."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Prompt helpers (shared)
    # ------------------------------------------------------------------

    def _build_system_prompt_arg(self) -> str:
        """Wrap system_prompt in <context> XML tags."""
        if not self.system_prompt:
            return "<context>\n</context>"
        return f"<context>\n{self.system_prompt}\n</context>"

    def _embed_context(self, message: str) -> str:
        """Prepend the <context> block to a message.

        Used by CLI providers that have no dedicated system-prompt flag (agy,
        codex) — the context rides as a preamble of the user turn.
        """
        if not self.system_prompt:
            return message
        return f"<context>\n{self.system_prompt}\n</context>\n\n{message}"

    # ------------------------------------------------------------------
    # Subprocess streaming helper (shared by CLI providers)
    # ------------------------------------------------------------------

    def _stream_subprocess_lines(self, cmd: list[str]) -> Generator[str, None, None]:
        """Spawn *cmd* at project_root and yield stdout lines as they arrive.

        Raises RuntimeError on spawn failure or a non-zero exit (with stderr
        attached) so the route surfaces an error event.
        """
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
            raise RuntimeError(f"Failed to start {cmd[0]}: {exc}") from exc

        if proc.stdout is not None:
            yield from proc.stdout

        proc.wait()
        if proc.returncode:
            stderr_output = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(
                f"{cmd[0]} exited with code {proc.returncode}: {stderr_output.strip()}"
            )

    # ------------------------------------------------------------------
    # Persistence helpers (shared)
    # ------------------------------------------------------------------

    def _get_review_conn(self):
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
        """Load all chat messages for this slug from the review DB."""
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


# ===========================================================================
# claude_cli — default provider (behavior unchanged)
# ===========================================================================


class ClaudeChatBackend(BaseChatBackend):
    """Headless Claude chat backend using subprocess or Anthropic API fallback.

    Session persistence: session UUID is stored per-slug in the per-slug review
    DB so it survives notebook restarts. The subprocess cwd is set to
    project_root (NOT the slug dir) so Claude Code binds the session to the
    actual project.

    Prompt injection defense: system_prompt is wrapped in <context>...</context>
    XML tags and injected via --append-system-prompt, not the user message.

    Flag-error fallback: if the claude CLI does not support
    --append-system-prompt (detected by "unknown option"/"unrecognized" in
    stderr), the context is embedded as the first user turn instead. This path
    does NOT retry with --append-system-prompt to avoid an infinite loop.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # When True, --append-system-prompt is not supported by the installed CLI.
        self._append_prompt_unsupported: bool = False

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

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

        Writes user message to the review DB before invoking Claude. Writes the
        full assistant response after the generator completes. Raises
        RuntimeError if neither claude CLI nor API fallback is available.
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


# ===========================================================================
# antigravity_cli — Google Antigravity CLI (`agy -p`)
# ===========================================================================


class AntigravityCliProvider(BaseChatBackend):
    """Chat backend wrapping the Antigravity CLI in non-interactive print mode.

    Invocation: ``agy -p "<prompt>" --dangerously-skip-permissions``. Print mode
    emits the assistant response as plain text on stdout (no JSON/SSE envelope),
    so each stdout line is forwarded verbatim as a text delta.

    Session continuity: ``agy --conversation <id>`` resumes an existing
    conversation. The id is mapped onto the review DB ``session_uuid`` (the same
    column Claude uses) and is only passed when one is already stored — agy
    rejects ``--conversation`` for an id that does not yet exist. Print mode does
    not expose a machine-readable new-conversation id, so first-turn id capture
    is a documented follow-up; resume works once an id is seeded.

    Context: print mode has no dedicated system-prompt flag, so the slug context
    is embedded as a ``<context>`` preamble on the user turn.
    """

    BINARY = "agy"

    @staticmethod
    def is_available() -> tuple[bool, str]:
        path = shutil.which(AntigravityCliProvider.BINARY)
        if path:
            return True, f"agy CLI found at {path}"
        return False, "Antigravity CLI (agy) not found on PATH."

    def _build_cmd(self, message: str) -> list[str]:
        path = shutil.which(self.BINARY) or self.BINARY
        cmd: list[str] = [
            path,
            "-p",
            self._embed_context(message),
            "--dangerously-skip-permissions",
        ]
        if self._session_id:
            cmd += ["--conversation", self._session_id]
        return cmd

    def _stream(self, user_msg: str) -> Generator[str, None, None]:
        for line in self._stream_subprocess_lines(self._build_cmd(user_msg)):
            if line:
                yield line


# ===========================================================================
# codex_cli — OpenAI Codex CLI (`codex exec --json`)
# ===========================================================================


class CodexCliProvider(BaseChatBackend):
    """Chat backend wrapping the OpenAI Codex CLI in non-interactive exec mode.

    Invocation: ``codex exec --json "<prompt>"``. With ``--json`` Codex emits a
    JSONL event stream; the assistant text lives in ``item.completed`` events
    whose item ``type`` is ``agent_message`` (field ``text``). The new session
    id arrives on the ``thread.started`` event and is persisted so subsequent
    turns resume via ``codex exec resume <SESSION_ID> --json "<prompt>"``.

    Context: ``codex exec`` has no system-prompt flag, so the slug context is
    embedded as a ``<context>`` preamble on the prompt.
    """

    BINARY = "codex"

    @staticmethod
    def is_available() -> tuple[bool, str]:
        path = shutil.which(CodexCliProvider.BINARY)
        if path:
            return True, f"codex CLI found at {path}"
        return False, "OpenAI Codex CLI (codex) not found on PATH."

    def _build_cmd(self, message: str) -> list[str]:
        path = shutil.which(self.BINARY) or self.BINARY
        prompt = self._embed_context(message)
        if self._session_id:
            return [path, "exec", "resume", self._session_id, "--json", prompt]
        return [path, "exec", "--json", prompt]

    def _capture_session(self, event: dict) -> None:
        thread_id = event.get("thread_id") or (event.get("thread") or {}).get("id")
        if thread_id and not self._session_id:
            self._session_id = thread_id
            self._save_session_id()

    def _stream(self, user_msg: str) -> Generator[str, None, None]:
        for line in self._stream_subprocess_lines(self._build_cmd(user_msg)):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "thread.started":
                self._capture_session(event)
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    text = item.get("text") or ""
                    if text:
                        yield text


# ===========================================================================
# openai_compatible — MLX / Ollama / LM Studio / llama.cpp
# ===========================================================================


class OpenAICompatibleProvider(BaseChatBackend):
    """Chat backend over any OpenAI-compatible ``/chat/completions`` server.

    Backed by the shared :class:`jobsmith.llm.openai_compat.OpenAICompatClient`,
    which is reused by the scorer. MLX (``mlx_lm.server`` :8080/v1), Ollama
    (:11434/v1), LM Studio and llama.cpp all funnel through the SAME SSE parse
    path — only ``base_url`` and ``model`` differ.

    These servers are stateless (no server-side conversation id), so the slug
    context is sent as a ``system`` message each turn and ``session_id`` stays
    ``None``. Per-turn history is still persisted by the base ``send``.
    """

    @staticmethod
    def is_available() -> tuple[bool, str]:
        # Reachability is validated at request time; construction only requires
        # a configured base_url (enforced by LLMSettings for this provider).
        return True, "openai_compatible backend configured."

    def _messages(self, user_msg: str) -> list[dict]:
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        # These servers are stateless, so the FULL conversation must be resent
        # each turn. The base ``send`` persists this user turn before ``_stream``
        # runs, so ``load_history`` already ends with the current message — reuse
        # it instead of re-appending (which would duplicate the turn).
        history = self.load_history()
        messages.extend({"role": h["role"], "content": h["content"]} for h in history)
        if not history or history[-1].get("role") != "user" or history[-1].get("content") != user_msg:
            # Persistence is best-effort; ensure the current turn is present.
            messages.append({"role": "user", "content": user_msg})
        return messages

    def _stream(self, user_msg: str) -> Generator[str, None, None]:
        from jobsmith.llm.openai_compat import OpenAICompatClient

        if not self.base_url:
            raise RuntimeError(
                "openai_compatible provider requires config.llm.base_url "
                "(e.g. LLM_PRESETS['mlx']['base_url'] or LLM_PRESETS['ollama']['base_url'])."
            )
        client = OpenAICompatClient(
            base_url=self.base_url,
            model=self.model or "default",
            api_key=self.api_key,
            timeout_s=float(self.timeout_s),
        )
        yield from client.stream_chat(self._messages(user_msg))


__all__ = [
    "BaseChatBackend",
    "ClaudeChatBackend",
    "AntigravityCliProvider",
    "CodexCliProvider",
    "OpenAICompatibleProvider",
]
