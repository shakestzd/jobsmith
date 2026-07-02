"""Tests for jobsmith.apply_local.backends (feat-c920ceeb, slice 2).

TDD: written before the implementation.

done_when coverage:
  1. OpenAICompatBackend.complete_structured posts a json_schema response_format
     and returns (parsed_dict, parse_ok) via the SHARED robust-parse helpers
     (extracted to jobsmith.llm.robust_json, not duplicated); parse_ok=False on
     unparseable content (NOT an exception).
  2. AnthropicBackend.complete_structured calls the anthropic SDK with ONE strict
     tool (input_schema == node schema), forces tool_choice to that tool, and
     returns the tool_use input as the dict; a text-only reply -> parse_ok=False.
  3. resolve_backend returns AnthropicBackend for `anthropic` nodes and
     OpenAICompatBackend for openai_compatible nodes, honouring the
     node_backends override first, then the node_backend default, then the
     parent LLMSettings provider — the caller imports neither class.

No live network: the OpenAI-compatible client and the anthropic client are both
injected fakes.
"""

from __future__ import annotations

import json

import pytest

from jobsmith.apply_local.backends import (
    AnthropicBackend,
    OpenAICompatBackend,
    resolve_backend,
)
from jobsmith.apply_local.driver import StructuredBackend
from jobsmith.config import (
    ApplySettings,
    JobsmithConfig,
    LLMSettings,
    NodeBackendConfig,
)

# ---------------------------------------------------------------------------
# Shared schema fixtures (OpenAI response_format shape wrapping a bare schema)
# ---------------------------------------------------------------------------

INNER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}
NODE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "node_output", "schema": INNER_SCHEMA},
}
_WELL_FORMED = json.dumps({"answer": "yes", "score": 7})


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCompatClient:
    """Stand-in for OpenAICompatClient — records calls, returns canned content."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    def complete(self, messages, *, response_format=None, temperature=None, extra=None) -> str:
        self.calls.append(
            {"messages": messages, "response_format": response_format, "temperature": temperature}
        )
        return self._content


class _FakeBlock:
    """A duck-typed anthropic content block (tool_use or text)."""

    def __init__(self, block_type: str, **attrs) -> None:
        self.type = block_type
        for key, value in attrs.items():
            setattr(self, key, value)


class _FakeResponse:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs) -> _FakeResponse:
        self.calls.append(kwargs)
        return self._response


class _FakeAnthropicClient:
    """Stand-in for anthropic.Anthropic — exposes a recording .messages.create."""

    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


def _tool_use_client(
    payload: dict, *, name: str = "emit_structured_output"
) -> _FakeAnthropicClient:
    block = _FakeBlock("tool_use", name=name, input=payload, id="toolu_1")
    return _FakeAnthropicClient(_FakeResponse([block]))


def _text_only_client(text: str = "I cannot comply.") -> _FakeAnthropicClient:
    return _FakeAnthropicClient(_FakeResponse([_FakeBlock("text", text=text)]))


# ---------------------------------------------------------------------------
# Protocol conformance — both backends satisfy the EXISTING StructuredBackend
# ---------------------------------------------------------------------------


def test_backends_satisfy_structured_backend_protocol() -> None:
    assert isinstance(OpenAICompatBackend(base_url="http://x/v1"), StructuredBackend)
    assert isinstance(AnthropicBackend(model="claude-sonnet-4-5", api_key="sk"), StructuredBackend)


# ---------------------------------------------------------------------------
# done_when #1 — OpenAICompatBackend
# ---------------------------------------------------------------------------


def test_openai_backend_posts_json_schema_and_parses() -> None:
    client = _FakeCompatClient(_WELL_FORMED)
    backend = OpenAICompatBackend(
        base_url="http://127.0.0.1:8080/v1", model="gemma", _client=client
    )

    data, ok = backend.complete_structured(
        [{"role": "user", "content": "hi"}], NODE_SCHEMA, temperature=0.0
    )

    assert ok is True
    assert data == {"answer": "yes", "score": 7}
    # A json_schema response_format was POSTed (already-OpenAI shape passes through).
    assert client.calls[0]["response_format"]["type"] == "json_schema"
    assert client.calls[0]["response_format"]["json_schema"]["schema"] == INNER_SCHEMA
    assert client.calls[0]["temperature"] == 0.0


def test_openai_backend_wraps_bare_schema_into_response_format() -> None:
    client = _FakeCompatClient(_WELL_FORMED)
    backend = OpenAICompatBackend(base_url="http://x/v1", _client=client)

    backend.complete_structured([{"role": "user", "content": "hi"}], INNER_SCHEMA)

    rf = client.calls[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == INNER_SCHEMA


def test_openai_backend_markdown_fence_parses_via_shared_helper() -> None:
    fenced = "```json\n" + _WELL_FORMED + "\n```"
    backend = OpenAICompatBackend(base_url="http://x/v1", _client=_FakeCompatClient(fenced))
    data, ok = backend.complete_structured([{"role": "user", "content": "hi"}], NODE_SCHEMA)
    assert ok is True
    assert data == {"answer": "yes", "score": 7}


@pytest.mark.parametrize(
    "bad",
    [
        "I'm sorry, I cannot comply.",  # prose
        "not json at all {{{",  # broken
        "",  # empty
        '{"answer": "yes", "score":',  # truncated
        "[1, 2, 3]",  # JSON but not an object
    ],
)
def test_openai_backend_unparseable_is_flagged_not_raised(bad: str) -> None:
    backend = OpenAICompatBackend(base_url="http://x/v1", _client=_FakeCompatClient(bad))
    data, ok = backend.complete_structured([{"role": "user", "content": "hi"}], NODE_SCHEMA)
    assert ok is False
    assert data is None


# ---------------------------------------------------------------------------
# done_when #2 — AnthropicBackend (strict tool_use)
# ---------------------------------------------------------------------------


def test_anthropic_backend_forces_tool_and_returns_input() -> None:
    client = _tool_use_client({"answer": "yes", "score": 7})
    backend = AnthropicBackend(model="claude-sonnet-4-5", api_key="sk", _client=client)

    data, ok = backend.complete_structured(
        [{"role": "system", "content": "be terse"}, {"role": "user", "content": "score it"}],
        NODE_SCHEMA,
        temperature=0.0,
    )

    assert ok is True
    assert data == {"answer": "yes", "score": 7}

    call = client.messages.calls[0]
    # Exactly ONE custom tool whose input_schema is the node's bare schema.
    assert len(call["tools"]) == 1
    assert call["tools"][0]["input_schema"] == INNER_SCHEMA
    tool_name = call["tools"][0]["name"]
    # tool_choice is FORCED to that single tool (strict structured-output mode).
    assert call["tool_choice"] == {"type": "tool", "name": tool_name}
    # The system message is hoisted to the top-level `system` param (Anthropic
    # has no system role inside messages); only chat turns remain.
    assert call["system"] == "be terse"
    assert call["messages"] == [{"role": "user", "content": "score it"}]
    assert call["model"] == "claude-sonnet-4-5"
    assert call["temperature"] == 0.0


def test_anthropic_backend_text_only_reply_is_parse_failure() -> None:
    backend = AnthropicBackend(model="m", api_key="sk", _client=_text_only_client())
    data, ok = backend.complete_structured([{"role": "user", "content": "hi"}], NODE_SCHEMA)
    assert ok is False
    assert data is None


def test_anthropic_backend_extracts_inner_from_bare_schema() -> None:
    client = _tool_use_client({"answer": "x"})
    backend = AnthropicBackend(model="m", api_key="sk", _client=client)
    backend.complete_structured([{"role": "user", "content": "hi"}], INNER_SCHEMA)
    assert client.messages.calls[0]["tools"][0]["input_schema"] == INNER_SCHEMA


# ---------------------------------------------------------------------------
# done_when #3 — resolve_backend routing (caller imports neither class)
# ---------------------------------------------------------------------------


def _config(
    *,
    default: NodeBackendConfig | None = None,
    overrides: dict[str, NodeBackendConfig] | None = None,
    parent: LLMSettings | None = None,
) -> JobsmithConfig:
    llm = parent or LLMSettings()
    llm = llm.model_copy(
        update={"apply": ApplySettings(node_backend=default, node_backends=overrides or {})}
    )
    return JobsmithConfig(llm=llm)


def test_resolve_backend_per_node_override_wins() -> None:
    cfg = _config(
        default=NodeBackendConfig(
            provider="openai_compatible", base_url="http://127.0.0.1:8080/v1", model="gemma"
        ),
        overrides={
            "writer": NodeBackendConfig(
                provider="anthropic", api_key="sk", model="claude-sonnet-4-5"
            )
        },
    )
    assert type(resolve_backend(cfg, "writer")).__name__ == "AnthropicBackend"
    # A node with no override falls through to the default backend.
    assert type(resolve_backend(cfg, "parser")).__name__ == "OpenAICompatBackend"


def test_resolve_backend_default_openai_compatible() -> None:
    cfg = _config(
        default=NodeBackendConfig(
            provider="openai_compatible", base_url="http://localhost:11434/v1"
        )
    )
    assert type(resolve_backend(cfg, "any")).__name__ == "OpenAICompatBackend"


def test_resolve_backend_default_anthropic() -> None:
    cfg = _config(default=NodeBackendConfig(provider="anthropic", api_key="sk"))
    assert type(resolve_backend(cfg, "any")).__name__ == "AnthropicBackend"


def test_resolve_backend_falls_back_to_parent_llm_settings() -> None:
    parent = LLMSettings(provider="openai_compatible", base_url="http://127.0.0.1:8080/v1")
    cfg = _config(parent=parent)  # no node_backend, no overrides
    assert type(resolve_backend(cfg, "any")).__name__ == "OpenAICompatBackend"


def test_resolve_backend_parent_anthropic() -> None:
    cfg = _config(parent=LLMSettings(provider="anthropic", api_key="sk"))
    assert type(resolve_backend(cfg, "any")).__name__ == "AnthropicBackend"


def test_resolve_backend_rejects_non_node_provider() -> None:
    # claude_cli is the cloud orchestrator, not a per-node structured backend.
    cfg = _config(parent=LLMSettings(provider="claude_cli"))
    with pytest.raises(ValueError, match="claude_cli"):
        resolve_backend(cfg, "any")


def test_resolved_backend_satisfies_protocol() -> None:
    cfg = _config(default=NodeBackendConfig(provider="anthropic", api_key="sk"))
    assert isinstance(resolve_backend(cfg, "any"), StructuredBackend)


# ---------------------------------------------------------------------------
# Extraction guard — the robust-parse helpers are SHARED, not duplicated
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# plain_text_mode — OpenAICompatBackend skips response_format, wraps prose
# ---------------------------------------------------------------------------


def test_plain_text_mode_skips_response_format() -> None:
    """plain_text_mode must NOT send response_format to the underlying client."""
    client = _FakeCompatClient("# Summary\n\n- Built a pipeline.")
    backend = OpenAICompatBackend(base_url="http://x/v1", plain_text_mode=True, _client=client)

    data, ok = backend.complete_structured([{"role": "user", "content": "write it"}], NODE_SCHEMA)

    assert ok is True
    assert data == {"markdown": "# Summary\n\n- Built a pipeline.", "would_fabricate": None}
    assert client.calls[0]["response_format"] is None, "response_format must be None in plain_text_mode"


def test_plain_text_mode_empty_response_is_parse_failure() -> None:
    """Empty text in plain_text_mode → (None, False) so the driver can reask."""
    client = _FakeCompatClient("")
    backend = OpenAICompatBackend(base_url="http://x/v1", plain_text_mode=True, _client=client)

    data, ok = backend.complete_structured([{"role": "user", "content": "write it"}], NODE_SCHEMA)

    assert ok is False
    assert data is None


def test_plain_text_mode_would_fabricate_sentinel_extracted() -> None:
    """A WOULD_FABRICATE: prefix is parsed and returned in would_fabricate field."""
    client = _FakeCompatClient("WOULD_FABRICATE: $500M cost savings not in context")
    backend = OpenAICompatBackend(base_url="http://x/v1", plain_text_mode=True, _client=client)

    data, ok = backend.complete_structured([{"role": "user", "content": "write it"}], NODE_SCHEMA)

    assert ok is True
    assert data is not None
    assert data["would_fabricate"] == "$500M cost savings not in context"
    assert data["markdown"] == ""


def test_plain_text_mode_forwarded_by_resolve_backend() -> None:
    """resolve_backend must forward plain_text_mode=True to OpenAICompatBackend."""
    from jobsmith.config import ApplySettings

    cfg = JobsmithConfig(
        llm=LLMSettings(
            apply=ApplySettings(
                node_backend=NodeBackendConfig(
                    provider="openai_compatible",
                    base_url="http://x/v1",
                    plain_text_mode=True,
                )
            )
        )
    )
    backend = resolve_backend(cfg, "prose-write")
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.plain_text_mode is True


# ---------------------------------------------------------------------------
# Gemma smart defaults — _is_gemma_model + _effective_openai_compat_flags
# ---------------------------------------------------------------------------


def test_is_gemma_model_matches_various_gemma_ids() -> None:
    from jobsmith.apply_local.backends import _is_gemma_model

    assert _is_gemma_model("mlx-community/gemma-4-E4B-it-qat-4bit") is True
    assert _is_gemma_model("Gemma-3-2B") is True  # uppercase
    assert _is_gemma_model("GEMMA-4") is True  # all-caps
    assert _is_gemma_model(None) is False
    assert _is_gemma_model("") is False
    assert _is_gemma_model("gpt-4o") is False
    assert _is_gemma_model("claude-sonnet-4-5") is False
    assert _is_gemma_model("llama-3.1-8b") is False


def _make_cfg_openai(model: str, **extra) -> JobsmithConfig:
    """Helper: build a JobsmithConfig with openai_compatible + given model."""
    from jobsmith.config import ApplySettings

    return JobsmithConfig(
        llm=LLMSettings(
            apply=ApplySettings(
                node_backend=NodeBackendConfig(
                    provider="openai_compatible",
                    base_url="http://127.0.0.1:8081/v1",
                    model=model,
                    **extra,
                )
            )
        )
    )


def test_gemma_structured_node_gets_disable_thinking_true() -> None:
    """Gemma + structured node → disable_thinking=True applied by default."""
    for node_name in ("jd-parse", "fit-score", "bullet-select"):
        backend = resolve_backend(_make_cfg_openai("mlx-community/gemma-4-E4B-it-qat-4bit"), node_name)
        assert isinstance(backend, OpenAICompatBackend), node_name
        assert backend.disable_thinking is True, f"disable_thinking should be True for {node_name}"
        assert backend.plain_text_mode is False, f"plain_text_mode should be False for {node_name}"


def test_gemma_prose_write_node_gets_plain_text_mode_true() -> None:
    """Gemma + prose-write → plain_text_mode=True, disable_thinking=False by default."""
    backend = resolve_backend(_make_cfg_openai("mlx-community/gemma-4-E4B-it-qat-4bit"), "prose-write")
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.plain_text_mode is True
    assert backend.disable_thinking is False


def test_gemma_defaults_apply_max_tokens() -> None:
    """Gemma defaults: prose-write gets GEMMA_PROSE_MAX_TOKENS; structured gets GEMMA_STRUCTURED_MAX_TOKENS."""
    from jobsmith.apply_local.backends import GEMMA_PROSE_MAX_TOKENS, GEMMA_STRUCTURED_MAX_TOKENS

    prose_backend = resolve_backend(_make_cfg_openai("mlx-community/gemma-4-E4B-it-qat-4bit"), "prose-write")
    structured_backend = resolve_backend(_make_cfg_openai("mlx-community/gemma-4-E4B-it-qat-4bit"), "jd-parse")
    assert isinstance(prose_backend, OpenAICompatBackend)
    assert isinstance(structured_backend, OpenAICompatBackend)
    assert prose_backend.max_tokens == GEMMA_PROSE_MAX_TOKENS
    assert structured_backend.max_tokens == GEMMA_STRUCTURED_MAX_TOKENS


def test_non_gemma_openai_compat_not_affected() -> None:
    """Non-Gemma openai_compatible model: no Gemma defaults applied."""
    for model in ("gpt-4o", "llama-3.1-8b", "mistral-7b"):
        backend = resolve_backend(_make_cfg_openai(model), "jd-parse")
        assert isinstance(backend, OpenAICompatBackend), model
        assert backend.disable_thinking is False, f"disable_thinking should stay False for {model}"
        assert backend.plain_text_mode is False, f"plain_text_mode should stay False for {model}"

    prose_backend = resolve_backend(_make_cfg_openai("gpt-4o"), "prose-write")
    assert isinstance(prose_backend, OpenAICompatBackend)
    assert prose_backend.plain_text_mode is False


def test_explicit_user_false_overrides_gemma_default() -> None:
    """Explicit disable_thinking=False must override the Gemma default of True for structured nodes."""
    backend = resolve_backend(
        _make_cfg_openai("mlx-community/gemma-4-E4B-it-qat-4bit", disable_thinking=False),
        "jd-parse",
    )
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.disable_thinking is False


def test_explicit_user_false_overrides_gemma_plain_text_mode() -> None:
    """Explicit plain_text_mode=False must override the Gemma default of True for prose-write."""
    backend = resolve_backend(
        _make_cfg_openai("mlx-community/gemma-4-E4B-it-qat-4bit", plain_text_mode=False),
        "prose-write",
    )
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.plain_text_mode is False


def test_explicit_user_max_tokens_overrides_gemma_default() -> None:
    """Explicit max_tokens must override the Gemma default."""
    backend = resolve_backend(
        _make_cfg_openai("mlx-community/gemma-4-E4B-it-qat-4bit", max_tokens=1024),
        "prose-write",
    )
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.max_tokens == 1024


def test_robust_parse_helpers_are_shared_single_source() -> None:
    from jobsmith.llm import robust_json
    from jobsmith.sourcing import llm_rescore

    # llm_rescore must consume the EXACT shared callable (no fork).
    assert llm_rescore.coerce_json_object is robust_json.coerce_json_object
    # Sanity: the shared helper behaves as the backends rely on it to.
    assert robust_json.coerce_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert robust_json.coerce_json_object("nope") is None
