"""Per-node LLM backends for the code-orchestrated LOCAL apply path (feat-c920ceeb, slice 2).

Slice 1 (driver.py) defined the injected :class:`StructuredBackend` Protocol —
``complete_structured(messages, schema, *, temperature) -> (dict | None, parse_ok)``.
This module supplies the two real implementations a node can be routed to so the
SAME node body runs on a local OpenAI-compatible engine OR on cloud Claude:

* :class:`OpenAICompatBackend` — wraps the shared
  :class:`jobsmith.llm.openai_compat.OpenAICompatClient`, POSTs a json_schema
  ``response_format``, and parses the returned content with the SHARED robust
  helpers (:mod:`jobsmith.llm.robust_json`). Unparseable content yields
  ``parse_ok=False`` (never an exception); transport errors propagate so the
  driver's bounded reask loop can handle them.

* :class:`AnthropicBackend` — Claude has no OpenAI ``response_format``; structured
  output is obtained via tool use. We declare ONE custom tool whose
  ``input_schema`` is the node schema and FORCE ``tool_choice`` to it (strict
  mode), then map the returned ``tool_use`` block's ``input`` to a dict. A
  text-only reply (no tool_use block) yields ``parse_ok=False``.

:func:`resolve_backend` reads ``config.llm.apply`` (per-node override, then the
default, then the parent ``LLMSettings``) and returns the right backend — the
caller never imports either class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jobsmith.llm.robust_json import coerce_json_object

if TYPE_CHECKING:
    from jobsmith.apply_local.driver import StructuredBackend
    from jobsmith.config import JobsmithConfig, NodeBackendConfig

# Default Claude model when a node's backend config omits ``model``. Overridable
# per node via NodeBackendConfig.model; only a fallback, never hard-coded into a
# request when the operator has specified one.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_S = 300.0
# The single forced tool used to coerce Claude into strict structured output.
_TOOL_NAME = "emit_structured_output"
_TOOL_DESCRIPTION = (
    "Emit the node's result as a single structured object matching the provided "
    "input schema. You MUST call this tool exactly once and return nothing else."
)


# ---------------------------------------------------------------------------
# Schema normalisation — accept either the OpenAI response_format wrapper or a
# bare JSON Schema object, and expose both shapes the two backends need.
# ---------------------------------------------------------------------------


def _inner_schema(schema: dict) -> dict:
    """Return the bare JSON Schema object from any accepted node-schema shape."""
    if not isinstance(schema, dict):
        return {}
    nested = schema.get("json_schema")
    if isinstance(nested, dict) and isinstance(nested.get("schema"), dict):
        return nested["schema"]
    if isinstance(schema.get("schema"), dict):
        return schema["schema"]
    return schema


def _openai_response_format(schema: dict) -> dict:
    """Return an OpenAI-style ``response_format`` dict for ``schema``.

    Passes an already-OpenAI-shaped wrapper through unchanged; otherwise wraps
    the bare schema as ``{"type": "json_schema", "json_schema": {...}}``.
    """
    if isinstance(schema, dict) and schema.get("type") == "json_schema" and "json_schema" in schema:
        return schema
    return {
        "type": "json_schema",
        "json_schema": {"name": "node_output", "schema": _inner_schema(schema)},
    }


def _split_system(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Split chat ``messages`` into (system_text, non_system_turns).

    The Anthropic Messages API has no ``system`` role inside ``messages`` — the
    system prompt is a top-level parameter. System messages are concatenated;
    the remaining user/assistant turns are returned verbatim.
    """
    system_parts: list[str] = []
    turns: list[dict] = []
    for message in messages:
        if message.get("role") == "system":
            content = message.get("content")
            if content:
                system_parts.append(str(content))
        else:
            turns.append(message)
    system = "\n\n".join(system_parts) if system_parts else None
    return system, turns


# ---------------------------------------------------------------------------
# OpenAI-compatible backend (local gemma / MLX / Ollama / LM Studio / llama.cpp)
# ---------------------------------------------------------------------------


class OpenAICompatBackend:
    """Structured backend over an OpenAI-compatible ``/chat/completions`` server.

    Satisfies the driver's :class:`StructuredBackend` Protocol. Transport errors
    are NOT swallowed here — the driver's :meth:`Node._attempt` catches them and
    turns them into a reask, so this backend's only failure mode is returning
    ``parse_ok=False`` for content it cannot coerce into a JSON object.

    Parameters
    ----------
    disable_thinking:
        When True, sends ``{"chat_template_kwargs": {"enable_thinking": false}}``
        in the request body so mlx_lm's constrained JSON decoding applies without
        a Gemma-4 thinking preamble in the response.  Has no effect on servers
        that don't recognise the key.
    max_tokens:
        Maximum generation tokens; forwarded to :class:`OpenAICompatClient`.
        Prevents local engines from timing out waiting for context exhaustion.
    plain_text_mode:
        When True, skip the ``response_format`` (json_schema constrained decoding)
        and request plain-text output.  The raw response is wrapped into
        ``{"markdown": text, "would_fabricate": None}`` (or the would-fabricate
        sentinel is extracted if the response starts with ``WOULD_FABRICATE:``).
        Returns ``(None, False)`` when the response is empty so the driver's
        bounded reask loop retries rather than passing an empty draft through.
        Use this for long-form prose nodes where constrained JSON decoding fights
        free-text generation (e.g. ``prose-write`` with small local models).
    """

    # Sentinel prefix prose-write emits when it cannot verify a claim.
    _FABRICATE_SENTINEL = "WOULD_FABRICATE:"

    def __init__(
        self,
        *,
        base_url: str,
        model: str | None = None,
        api_key: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        disable_thinking: bool = False,
        plain_text_mode: bool = False,
        _client: Any = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.disable_thinking = disable_thinking
        self.plain_text_mode = plain_text_mode
        self._client = _client  # injectable for tests

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from jobsmith.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(
            base_url=self.base_url,
            model=self.model or "default",
            api_key=self.api_key,
            timeout_s=self.timeout_s,
            max_tokens=self.max_tokens,
        )

    def complete_structured(
        self, messages: list[dict], schema: dict, *, temperature: float = 0.0
    ) -> tuple[dict | None, bool]:
        client = self._get_client()
        extra: dict | None = None
        if self.disable_thinking:
            extra = {"chat_template_kwargs": {"enable_thinking": False}}
        if self.plain_text_mode:
            return self._complete_plain(client, messages, temperature=temperature, extra=extra)
        content = client.complete(
            messages,
            response_format=_openai_response_format(schema),
            temperature=temperature,
            extra=extra,
        )
        obj = coerce_json_object(content)
        if obj is None:
            return None, False
        return obj, True

    def _complete_plain(
        self,
        client: Any,
        messages: list[dict],
        *,
        temperature: float,
        extra: dict | None,
    ) -> tuple[dict | None, bool]:
        """Plain-text completion: no response_format, wraps result as markdown envelope.

        Returns ``({"markdown": text, "would_fabricate": None}, True)`` when the
        model produces non-empty prose, ``({"markdown": "", "would_fabricate":
        claim}, True)`` on a WOULD_FABRICATE sentinel, and ``(None, False)`` when
        the response is empty so the driver's reask loop can retry.
        """
        content = client.complete(messages, response_format=None, temperature=temperature, extra=extra)
        text = (content or "").strip()
        if not text:
            return None, False
        if text.startswith(self._FABRICATE_SENTINEL):
            claim = text[len(self._FABRICATE_SENTINEL):].strip().splitlines()[0].strip()
            return {"markdown": "", "would_fabricate": claim or "unspecified fabrication"}, True
        return {"markdown": text, "would_fabricate": None}, True


# ---------------------------------------------------------------------------
# Anthropic backend (cloud Claude, strict tool_use structured output)
# ---------------------------------------------------------------------------


class AnthropicBackend:
    """Structured backend over the Anthropic Messages API via forced tool use.

    Claude exposes no OpenAI ``response_format``; the documented way to obtain a
    schema-conformant object is a custom tool whose ``input_schema`` is the
    target schema, with ``tool_choice`` forced to that tool. The model's
    ``tool_use`` block ``input`` is the structured result. A text-only reply
    (no tool_use block) is reported as ``parse_ok=False``.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        _client: Any = None,
    ) -> None:
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self._client = _client  # injectable for tests

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import anthropic

        return anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout_s)

    def complete_structured(
        self, messages: list[dict], schema: dict, *, temperature: float = 0.0
    ) -> tuple[dict | None, bool]:
        system, turns = _split_system(messages)
        tool = {
            "name": _TOOL_NAME,
            "description": _TOOL_DESCRIPTION,
            "input_schema": _inner_schema(schema),
        }
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": temperature,
            "messages": turns,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        }
        if system is not None:
            kwargs["system"] = system
        response = self._get_client().messages.create(**kwargs)
        return _extract_tool_use(response)


def _extract_tool_use(response: Any) -> tuple[dict | None, bool]:
    """Return ``(tool_input, True)`` from the first tool_use block, else (None, False)."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "tool_use":
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data, True
    return None, False


# ---------------------------------------------------------------------------
# Resolution — config.llm.apply (override -> default -> parent LLMSettings)
# ---------------------------------------------------------------------------


def _resolve_node_config(config: JobsmithConfig, node_name: str) -> NodeBackendConfig:
    """Resolve the effective NodeBackendConfig for ``node_name``.

    Precedence: ``node_backends[node_name]`` > ``node_backend`` default > a
    config synthesised from the parent ``LLMSettings`` provider.
    """
    from jobsmith.config import NodeBackendConfig

    apply = config.llm.apply
    override = apply.node_backends.get(node_name)
    if override is not None:
        return override
    if apply.node_backend is not None:
        return apply.node_backend
    llm = config.llm
    return NodeBackendConfig(
        provider=llm.provider,
        model=llm.model,
        base_url=llm.base_url,
        api_key=llm.api_key,
    )


def resolve_backend(config: JobsmithConfig, node_name: str) -> StructuredBackend:
    """Return the per-node :class:`StructuredBackend` for ``node_name``.

    ``anthropic`` -> :class:`AnthropicBackend`; ``openai_compatible`` ->
    :class:`OpenAICompatBackend`. Other providers (claude_cli / codex_cli /
    antigravity_cli) are cloud orchestrators or chat-only and are NOT valid
    per-node structured backends in the code-local path — they fail fast.

    ``NodeBackendConfig.timeout_s``, ``max_tokens``, and ``disable_thinking``
    are forwarded to the concrete backend when set; ``None`` keeps each
    backend's own default so existing configs without these fields are unaffected.
    """
    node_cfg = _resolve_node_config(config, node_name)
    provider = node_cfg.provider
    if provider == "anthropic":
        kwargs: dict[str, Any] = {"model": node_cfg.model, "api_key": node_cfg.api_key}
        if node_cfg.max_tokens is not None:
            kwargs["max_tokens"] = node_cfg.max_tokens
        if node_cfg.timeout_s is not None:
            kwargs["timeout_s"] = node_cfg.timeout_s
        return AnthropicBackend(**kwargs)
    if provider == "openai_compatible":
        oc_kwargs: dict[str, Any] = {
            "base_url": node_cfg.base_url or "",
            "model": node_cfg.model,
            "api_key": node_cfg.api_key,
            "disable_thinking": node_cfg.disable_thinking,
            "plain_text_mode": node_cfg.plain_text_mode,
        }
        if node_cfg.max_tokens is not None:
            oc_kwargs["max_tokens"] = node_cfg.max_tokens
        if node_cfg.timeout_s is not None:
            oc_kwargs["timeout_s"] = node_cfg.timeout_s
        return OpenAICompatBackend(**oc_kwargs)
    raise ValueError(
        f"provider {provider!r} is not a valid per-node apply backend; "
        "the code-local orchestrator supports 'openai_compatible' (local engines) "
        "and 'anthropic' (cloud Claude). Set apply.node_backend or "
        "apply.node_backends[<node>] accordingly."
    )


__all__ = [
    "OpenAICompatBackend",
    "AnthropicBackend",
    "resolve_backend",
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_MAX_TOKENS",
]
