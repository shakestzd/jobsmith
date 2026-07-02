"""Shared OpenAI-compatible HTTP client (feat-7f4d1643, slice 2).

A small, dependency-light client for any server that implements the OpenAI
``POST {base_url}/chat/completions`` contract. The SAME client covers MLX
(``mlx_lm.server`` on :8080/v1), Ollama (:11434/v1), LM Studio and llama.cpp —
they differ only by ``base_url`` and ``model``, never by wire format.

This module is intentionally chat-agnostic and persistence-free so it can be
reused by both the chat backend (``OpenAICompatibleProvider``) and the scorer
(slice 3). It exposes two entry points:

* :meth:`OpenAICompatClient.stream_chat` — a ``Generator[str, None, None]`` of
  assistant text deltas, for streaming chat UIs.
* :meth:`OpenAICompatClient.complete` — the full assistant content string, with
  optional ``response_format`` for structured-JSON calls (the scorer path).

The streaming wire format is OpenAI Server-Sent Events: each chunk arrives as a
``data: {json}`` line whose ``choices[0].delta.content`` carries the next text
fragment; the stream terminates on the ``data: [DONE]`` sentinel. The pure
parser :func:`iter_sse_content_deltas` is exported separately so it can be unit
tested against canned lines without any network.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterable

import httpx

# SSE sentinel emitted by OpenAI / MLX / Ollama to mark end-of-stream.
_DONE_SENTINEL = "[DONE]"
_DATA_PREFIX = "data:"


def _chat_completions_url(base_url: str) -> str:
    """Return the ``/chat/completions`` endpoint for a ``base_url``.

    ``base_url`` already includes the ``/v1`` suffix (see ``LLM_PRESETS``), e.g.
    ``http://127.0.0.1:8080/v1`` → ``http://127.0.0.1:8080/v1/chat/completions``.
    """
    return base_url.rstrip("/") + "/chat/completions"


def iter_sse_content_deltas(lines: Iterable[str]) -> Generator[str, None, None]:
    """Parse OpenAI-compatible SSE ``data:`` lines into assistant text deltas.

    Yields the ``choices[0].delta.content`` of each chunk in order, skipping
    role-only / empty chunks, blank keep-alive lines and ``event:`` lines, and
    terminating cleanly on the ``data: [DONE]`` sentinel. Malformed JSON chunks
    are skipped rather than raising. This is the single parse path shared by
    every openai_compatible server (MLX, Ollama, LM Studio, llama.cpp).
    """
    for raw in lines:
        if raw is None:
            continue
        line = raw.strip()
        if not line or not line.startswith(_DATA_PREFIX):
            continue
        payload = line[len(_DATA_PREFIX):].strip()
        if payload == _DONE_SENTINEL:
            return
        try:
            chunk = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content


class OpenAICompatClient:
    """Minimal client for an OpenAI-compatible ``/chat/completions`` endpoint.

    Parameters
    ----------
    base_url:
        Server base URL including the ``/v1`` suffix (e.g. an ``LLM_PRESETS``
        value). The endpoint is derived as ``{base_url}/chat/completions``.
    model:
        Model identifier the server expects in the request body.
    api_key:
        Optional bearer token. Sent as ``Authorization: Bearer <key>`` when set;
        local MLX/Ollama servers ignore it, so it is harmless when absent.
    timeout_s:
        Per-request timeout in seconds (maps to ``config.llm.timeout_s``).
    max_tokens:
        Maximum number of tokens the server may generate per request. Sending an
        explicit cap prevents local engines (mlx_lm, Ollama) from waiting until
        context is exhausted before returning, which would otherwise cause a
        300-second timeout on every structured call. Default 4096.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 300.0,
        max_tokens: int = 4096,
    ) -> None:
        if not base_url:
            raise ValueError("OpenAICompatClient requires a non-empty base_url.")
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Request construction
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(
        self,
        messages: list[dict],
        *,
        stream: bool,
        response_format: dict | None = None,
        temperature: float | None = None,
        extra: dict | None = None,
    ) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "max_tokens": self.max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if temperature is not None:
            payload["temperature"] = temperature
        if extra:
            payload.update(extra)
        return payload

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stream_chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        response_format: dict | None = None,
        extra: dict | None = None,
    ) -> Generator[str, None, None]:
        """Stream assistant text deltas for ``messages``.

        POSTs with ``stream=True`` and yields each ``delta.content`` fragment as
        it arrives, terminating on ``data: [DONE]``.
        """
        payload = self._payload(
            messages,
            stream=True,
            response_format=response_format,
            temperature=temperature,
            extra=extra,
        )
        with httpx.Client(timeout=self.timeout_s) as client, client.stream(
            "POST",
            _chat_completions_url(self.base_url),
            json=payload,
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            yield from iter_sse_content_deltas(resp.iter_lines())

    def complete(
        self,
        messages: list[dict],
        *,
        response_format: dict | None = None,
        temperature: float | None = None,
        extra: dict | None = None,
    ) -> str:
        """Return the full assistant content string for ``messages``.

        POSTs with ``stream=False`` and returns ``choices[0].message.content``.
        Pass ``response_format={"type": "json_object"}`` for structured-JSON
        calls (the scorer's path). Returns ``""`` if the server returns no
        choices.
        """
        payload = self._payload(
            messages,
            stream=False,
            response_format=response_format,
            temperature=temperature,
            extra=extra,
        )
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(
                _chat_completions_url(self.base_url),
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return message.get("content") or ""


__all__ = ["OpenAICompatClient", "iter_sse_content_deltas"]
