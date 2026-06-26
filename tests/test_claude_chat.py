"""Tests for ClaudeChatBackend (slice 5).

Tests follow the TDD plan: write failing tests first, then implement.

Test strategy:
- Subprocess invocation args
- Session UUID capture from init event
- Streaming assistant text
- Generator terminates on result event
- Stale-resume retry logic
- SDK fallback when binary absent
- System prompt XML wrapping
- Subprocess cwd is project root
- Unknown flag fallback to first user turn
- Chat message persistence
"""
from __future__ import annotations

import json
import unittest.mock as mock
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pytest

from jobsmith.api.claude_chat import ClaudeChatBackend
from jobsmith.db import open_review_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _make_backend(
    tmp_path: Path,
    *,
    slug: str = "test-slug",
    system_prompt: str | None = "You are helpful.",
    session_id: str | None = None,
) -> ClaudeChatBackend:
    """Construct a ClaudeChatBackend with a tmp review_db_dir."""
    review_dir = tmp_path / ".review"
    review_dir.mkdir(parents=True, exist_ok=True)
    return ClaudeChatBackend(
        slug=slug,
        project_root=tmp_path,
        review_db_dir=review_dir,
        system_prompt=system_prompt,
        session_id=session_id,
    )


def _jsonl(*events: dict) -> str:
    """Encode a sequence of dicts as newline-delimited JSON."""
    return "\n".join(json.dumps(ev) for ev in events) + "\n"


def _make_stdout(content: str) -> StringIO:
    return StringIO(content)


def _init_event(session_id: str = "abc-123") -> dict:
    return {"type": "system", "subtype": "init", "session_id": session_id}


def _assistant_event(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _result_event() -> dict:
    return {"type": "result", "subtype": "success"}


# ---------------------------------------------------------------------------
# test_subprocess_invocation_args
# ---------------------------------------------------------------------------


def test_subprocess_invocation_args(tmp_path: Path) -> None:
    """Popen argv must include --output-format stream-json --verbose --append-system-prompt."""
    backend = _make_backend(tmp_path, system_prompt="ctx")

    jsonl = _jsonl(_init_event(), _assistant_event("hello"), _result_event())

    proc_mock = mock.MagicMock()
    proc_mock.stdout = StringIO(jsonl)
    proc_mock.stderr = StringIO("")
    proc_mock.returncode = 0
    proc_mock.wait.return_value = 0

    with mock.patch("shutil.which", return_value="/usr/bin/claude"), mock.patch(
        "subprocess.Popen", return_value=proc_mock
    ) as popen_mock:
        list(backend.send("hello"))

    popen_mock.assert_called_once()
    argv = popen_mock.call_args[0][0]
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--verbose" in argv
    assert "--append-system-prompt" in argv


# ---------------------------------------------------------------------------
# test_session_uuid_captured_from_init_event
# ---------------------------------------------------------------------------


def test_session_uuid_captured_from_init_event(tmp_path: Path) -> None:
    """session_id is captured from type=system, subtype=init event."""
    backend = _make_backend(tmp_path)

    jsonl = _jsonl(
        {"type": "system", "subtype": "init", "session_id": "abc-123"},
        _assistant_event("hi"),
        _result_event(),
    )

    proc_mock = mock.MagicMock()
    proc_mock.stdout = StringIO(jsonl)
    proc_mock.stderr = StringIO("")
    proc_mock.returncode = 0
    proc_mock.wait.return_value = 0

    with mock.patch("shutil.which", return_value="/usr/bin/claude"), mock.patch(
        "subprocess.Popen", return_value=proc_mock
    ):
        list(backend.send("hello"))

    assert backend.session_id == "abc-123"


# ---------------------------------------------------------------------------
# test_stream_yields_assistant_text
# ---------------------------------------------------------------------------


def test_stream_yields_assistant_text(tmp_path: Path) -> None:
    """Generator yields text chunks from type=assistant events."""
    backend = _make_backend(tmp_path)

    jsonl = _jsonl(
        _init_event(),
        _assistant_event("Hello "),
        _assistant_event("world!"),
        _result_event(),
    )

    proc_mock = mock.MagicMock()
    proc_mock.stdout = StringIO(jsonl)
    proc_mock.stderr = StringIO("")
    proc_mock.returncode = 0
    proc_mock.wait.return_value = 0

    with mock.patch("shutil.which", return_value="/usr/bin/claude"), mock.patch(
        "subprocess.Popen", return_value=proc_mock
    ):
        chunks = list(backend.send("hello"))

    assert chunks == ["Hello ", "world!"]


# ---------------------------------------------------------------------------
# test_stream_breaks_on_result
# ---------------------------------------------------------------------------


def test_stream_breaks_on_result(tmp_path: Path) -> None:
    """Generator terminates when type=result event is received."""
    backend = _make_backend(tmp_path)

    # After result, there are more assistant events — they should not be yielded
    jsonl = _jsonl(
        _init_event(),
        _assistant_event("before"),
        _result_event(),
        _assistant_event("after"),  # should not appear
    )

    proc_mock = mock.MagicMock()
    proc_mock.stdout = StringIO(jsonl)
    proc_mock.stderr = StringIO("")
    proc_mock.returncode = 0
    proc_mock.wait.return_value = 0

    with mock.patch("shutil.which", return_value="/usr/bin/claude"), mock.patch(
        "subprocess.Popen", return_value=proc_mock
    ):
        chunks = list(backend.send("hello"))

    assert "after" not in chunks
    assert "before" in chunks


# ---------------------------------------------------------------------------
# test_stale_resume_retries_without_resume
# ---------------------------------------------------------------------------


def test_stale_resume_retries_without_resume(tmp_path: Path) -> None:
    """On stale session, retry without --resume; second invocation has no --resume."""
    backend = _make_backend(tmp_path, session_id="stale-uuid-old")

    # First invocation: non-zero exit + "session not found" in stderr
    fail_mock = mock.MagicMock()
    fail_mock.stdout = StringIO(_jsonl(_init_event("stale-uuid-old")))
    fail_mock.stderr = StringIO("Error: session not found: stale-uuid-old")
    fail_mock.returncode = 1
    fail_mock.wait.return_value = 1

    # Second invocation (no --resume): success
    success_jsonl = _jsonl(_init_event("new-uuid"), _assistant_event("hi"), _result_event())
    ok_mock = mock.MagicMock()
    ok_mock.stdout = StringIO(success_jsonl)
    ok_mock.stderr = StringIO("")
    ok_mock.returncode = 0
    ok_mock.wait.return_value = 0

    call_count = 0
    captured_argvs: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        nonlocal call_count
        captured_argvs.append(cmd)
        call_count += 1
        if call_count == 1:
            return fail_mock
        return ok_mock

    with mock.patch("shutil.which", return_value="/usr/bin/claude"), mock.patch(
        "subprocess.Popen", side_effect=fake_popen
    ):
        list(backend.send("hello"))

    assert call_count == 2
    # Second invocation must NOT have --resume
    assert "--resume" not in captured_argvs[1]


# ---------------------------------------------------------------------------
# test_sdk_fallback_when_binary_absent
# ---------------------------------------------------------------------------


def test_sdk_fallback_when_binary_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK path taken when claude binary absent but ANTHROPIC_API_KEY is set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-12345")
    backend = _make_backend(tmp_path)

    chunks_yielded: list[str] = ["SDK chunk 1", "SDK chunk 2"]

    mock_stream_ctx = mock.MagicMock()
    mock_stream_ctx.__enter__ = mock.MagicMock(return_value=mock_stream_ctx)
    mock_stream_ctx.__exit__ = mock.MagicMock(return_value=False)
    mock_stream_ctx.text_stream = iter(chunks_yielded)

    mock_client = mock.MagicMock()
    mock_client.messages.stream.return_value = mock_stream_ctx
    mock_anthropic_module = mock.MagicMock()
    mock_anthropic_module.Anthropic.return_value = mock_client

    with mock.patch("shutil.which", return_value=None), mock.patch.dict(
        "sys.modules", {"anthropic": mock_anthropic_module}
    ):
        result = list(backend.send("hello"))

    assert result == chunks_yielded


# ---------------------------------------------------------------------------
# test_system_prompt_xml_wrapped
# ---------------------------------------------------------------------------


def test_system_prompt_xml_wrapped(tmp_path: Path) -> None:
    """Context is wrapped in <context>...</context> tags in the argv."""
    backend = _make_backend(tmp_path, system_prompt="my context data")

    jsonl = _jsonl(_init_event(), _assistant_event("ok"), _result_event())
    proc_mock = mock.MagicMock()
    proc_mock.stdout = StringIO(jsonl)
    proc_mock.stderr = StringIO("")
    proc_mock.returncode = 0
    proc_mock.wait.return_value = 0

    with mock.patch("shutil.which", return_value="/usr/bin/claude"), mock.patch(
        "subprocess.Popen", return_value=proc_mock
    ) as popen_mock:
        list(backend.send("hello"))

    argv = popen_mock.call_args[0][0]
    # Find the --append-system-prompt value
    idx = argv.index("--append-system-prompt")
    prompt_value = argv[idx + 1]
    assert "<context>" in prompt_value
    assert "my context data" in prompt_value
    assert "</context>" in prompt_value


# ---------------------------------------------------------------------------
# test_subprocess_cwd_is_project_root
# ---------------------------------------------------------------------------


def test_subprocess_cwd_is_project_root(tmp_path: Path) -> None:
    """Popen is called with cwd=project_root, not slug dir."""
    project_root = tmp_path / "my-project"
    project_root.mkdir()
    review_dir = tmp_path / ".review"
    review_dir.mkdir()

    backend = ClaudeChatBackend(
        slug="test-slug",
        project_root=project_root,
        review_db_dir=review_dir,
        system_prompt="ctx",
    )

    jsonl = _jsonl(_init_event(), _assistant_event("ok"), _result_event())
    proc_mock = mock.MagicMock()
    proc_mock.stdout = StringIO(jsonl)
    proc_mock.stderr = StringIO("")
    proc_mock.returncode = 0
    proc_mock.wait.return_value = 0

    with mock.patch("shutil.which", return_value="/usr/bin/claude"), mock.patch(
        "subprocess.Popen", return_value=proc_mock
    ) as popen_mock:
        list(backend.send("hello"))

    kwargs = popen_mock.call_args[1]
    assert str(kwargs.get("cwd")) == str(project_root)


# ---------------------------------------------------------------------------
# test_unknown_flag_falls_back_to_first_user_turn
# ---------------------------------------------------------------------------


def test_unknown_flag_falls_back_to_first_user_turn(tmp_path: Path) -> None:
    """On 'unknown option' in stderr, fallback: no --append-system-prompt, context as first user turn."""
    backend = _make_backend(tmp_path, system_prompt="important context")

    call_count = 0
    captured_argvs: list[list[str]] = []
    captured_msgs: list[str] = []

    fail_mock = mock.MagicMock()
    fail_mock.stdout = StringIO("")
    fail_mock.stderr = StringIO("Error: unknown option: --append-system-prompt")
    fail_mock.returncode = 1
    fail_mock.wait.return_value = 1

    success_jsonl = _jsonl(_init_event("new-uuid"), _assistant_event("ok"), _result_event())
    ok_mock = mock.MagicMock()
    ok_mock.stdout = StringIO(success_jsonl)
    ok_mock.stderr = StringIO("")
    ok_mock.returncode = 0
    ok_mock.wait.return_value = 0

    def fake_popen(cmd, **kwargs):
        nonlocal call_count
        captured_argvs.append(cmd)
        # Capture the user message (first positional after 'claude -p')
        if "-p" in cmd:
            idx = cmd.index("-p")
            captured_msgs.append(cmd[idx + 1])
        call_count += 1
        if call_count == 1:
            return fail_mock
        return ok_mock

    with mock.patch("shutil.which", return_value="/usr/bin/claude"), mock.patch(
        "subprocess.Popen", side_effect=fake_popen
    ):
        list(backend.send("user question"))

    assert call_count == 2
    # Second invocation must NOT include --append-system-prompt
    assert "--append-system-prompt" not in captured_argvs[1]
    # Second invocation's message should embed context as preamble
    assert len(captured_msgs) >= 2
    assert "important context" in captured_msgs[1]


# ---------------------------------------------------------------------------
# test_chat_messages_persisted
# ---------------------------------------------------------------------------


def test_chat_messages_persisted(tmp_path: Path) -> None:
    """After a send() turn, chat_messages table has user + assistant rows for slug."""
    slug = "acme-swe-2026"
    review_dir = tmp_path / ".review"
    review_dir.mkdir(parents=True, exist_ok=True)

    backend = ClaudeChatBackend(
        slug=slug,
        project_root=tmp_path,
        review_db_dir=review_dir,
        system_prompt="ctx",
    )

    jsonl = _jsonl(_init_event("sess-999"), _assistant_event("Nice to meet you."), _result_event())
    proc_mock = mock.MagicMock()
    proc_mock.stdout = StringIO(jsonl)
    proc_mock.stderr = StringIO("")
    proc_mock.returncode = 0
    proc_mock.wait.return_value = 0

    with mock.patch("shutil.which", return_value="/usr/bin/claude"), mock.patch(
        "subprocess.Popen", return_value=proc_mock
    ):
        list(backend.send("Hello Claude!"))

    # Verify persistence in review DB
    conn = open_review_db(slug, review_dir)
    rows = conn.execute(
        "SELECT role, text FROM chat_messages WHERE slug=? ORDER BY created_at",
        (slug,),
    ).fetchall()
    conn.close()

    roles = [r["role"] for r in rows]
    assert "user" in roles
    assert "assistant" in roles
    texts = {r["role"]: r["text"] for r in rows}
    assert "Hello Claude!" in texts["user"]
    assert "Nice to meet you." in texts["assistant"]


# ===========================================================================
# Slice 2 — pluggable backend providers (feat-7f4d1643)
# ===========================================================================

from jobsmith.api.chat import _make_backend as make_backend_factory  # noqa: E402
from jobsmith.api.claude_chat import (  # noqa: E402
    AntigravityCliProvider,
    BaseChatBackend,
    CodexCliProvider,
    OpenAICompatibleProvider,
)
from jobsmith.config import LLMSettings  # noqa: E402
from jobsmith.llm.openai_compat import (  # noqa: E402
    OpenAICompatClient,
    iter_sse_content_deltas,
)


def _common_kwargs(tmp_path: Path) -> dict:
    review_dir = tmp_path / ".review"
    review_dir.mkdir(parents=True, exist_ok=True)
    return {
        "slug": "test-slug",
        "project_root": tmp_path,
        "review_db_dir": review_dir,
        "system_prompt": "ctx",
    }


# ---------------------------------------------------------------------------
# Backend factory resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider, expected_cls",
    [
        ("claude_cli", ClaudeChatBackend),
        ("antigravity_cli", AntigravityCliProvider),
        ("codex_cli", CodexCliProvider),
        ("openai_compatible", OpenAICompatibleProvider),
    ],
)
def test_backend_factory_resolves_provider_class(tmp_path, provider, expected_cls):
    """The factory maps each config.llm.provider value to the correct class."""
    base_url = "http://127.0.0.1:8080/v1" if provider == "openai_compatible" else None
    llm = LLMSettings(provider=provider, model="m", base_url=base_url)
    backend = make_backend_factory(llm=llm, **_common_kwargs(tmp_path))
    assert type(backend) is expected_cls
    assert isinstance(backend, BaseChatBackend)


def test_backend_factory_defaults_to_claude(tmp_path):
    """Default LLMSettings (no llm block) resolves to ClaudeChatBackend."""
    backend = make_backend_factory(llm=LLMSettings(), **_common_kwargs(tmp_path))
    assert type(backend) is ClaudeChatBackend


# ---------------------------------------------------------------------------
# Antigravity CLI (`agy -p ... --dangerously-skip-permissions`)
# ---------------------------------------------------------------------------


def test_antigravity_cmd_construction(tmp_path):
    """argv is `agy -p <prompt> --dangerously-skip-permissions` (no --conversation)."""
    backend = AntigravityCliProvider(**_common_kwargs(tmp_path))
    with mock.patch("shutil.which", return_value="/usr/bin/agy"):
        cmd = backend._build_cmd("hello there")
    assert cmd[0] == "/usr/bin/agy"
    assert cmd[1] == "-p"
    assert "--dangerously-skip-permissions" in cmd
    assert "--conversation" not in cmd
    # context embedded as <context> preamble on the user turn
    assert "<context>" in cmd[2]
    assert "hello there" in cmd[2]


def test_antigravity_cmd_includes_conversation_when_session_present(tmp_path):
    """A stored session id maps onto `--conversation <id>`."""
    backend = AntigravityCliProvider(session_id="conv-123", **_common_kwargs(tmp_path))
    with mock.patch("shutil.which", return_value="/usr/bin/agy"):
        cmd = backend._build_cmd("hi")
    assert "--conversation" in cmd
    assert cmd[cmd.index("--conversation") + 1] == "conv-123"


def test_antigravity_stream_yields_plaintext_lines(tmp_path):
    """Plain-text stdout lines are forwarded verbatim as text deltas."""
    backend = AntigravityCliProvider(**_common_kwargs(tmp_path))

    proc_mock = mock.MagicMock()
    proc_mock.stdout = iter(["Hello\n", "world\n"])
    proc_mock.stderr = StringIO("")
    proc_mock.returncode = 0
    proc_mock.wait.return_value = 0

    with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
        "subprocess.Popen", return_value=proc_mock
    ):
        chunks = list(backend.send("hi"))
    assert "".join(chunks) == "Hello\nworld\n"


# ---------------------------------------------------------------------------
# Codex CLI (`codex exec --json`)
# ---------------------------------------------------------------------------


def test_codex_cmd_construction(tmp_path):
    """argv is `codex exec --json <prompt>` on a fresh session."""
    backend = CodexCliProvider(**_common_kwargs(tmp_path))
    with mock.patch("shutil.which", return_value="/usr/bin/codex"):
        cmd = backend._build_cmd("summarize")
    assert cmd[0] == "/usr/bin/codex"
    assert cmd[1] == "exec"
    assert "--json" in cmd
    assert "resume" not in cmd
    assert any("summarize" in part for part in cmd)


def test_codex_cmd_resumes_with_session_id(tmp_path):
    """A stored session id produces `codex exec resume <id> --json <prompt>`."""
    backend = CodexCliProvider(session_id="thread-abc", **_common_kwargs(tmp_path))
    with mock.patch("shutil.which", return_value="/usr/bin/codex"):
        cmd = backend._build_cmd("again")
    assert cmd[1:4] == ["exec", "resume", "thread-abc"]
    assert "--json" in cmd


def test_codex_stream_parses_agent_message_and_captures_session(tmp_path):
    """JSONL item.completed/agent_message yields text; thread.started captures id."""
    backend = CodexCliProvider(**_common_kwargs(tmp_path))

    events = [
        json.dumps({"type": "thread.started", "thread_id": "thread-xyz"}) + "\n",
        json.dumps({"type": "turn.started"}) + "\n",
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_3", "type": "agent_message", "text": "Final answer."},
            }
        )
        + "\n",
        json.dumps({"type": "turn.completed"}) + "\n",
    ]
    proc_mock = mock.MagicMock()
    proc_mock.stdout = iter(events)
    proc_mock.stderr = StringIO("")
    proc_mock.returncode = 0
    proc_mock.wait.return_value = 0

    with mock.patch("shutil.which", return_value="/usr/bin/codex"), mock.patch(
        "subprocess.Popen", return_value=proc_mock
    ):
        chunks = list(backend.send("question"))

    assert chunks == ["Final answer."]
    assert backend.session_id == "thread-xyz"


# ---------------------------------------------------------------------------
# OpenAI-compatible SSE parsing (shared client — reused by the scorer)
# ---------------------------------------------------------------------------

_CANNED_SSE = [
    'data: {"choices":[{"delta":{"role":"assistant"}}]}',
    'data: {"choices":[{"delta":{"content":"Hello"}}]}',
    "",  # keep-alive blank line
    'data: {"choices":[{"delta":{"content":", "}}]}',
    'data: {"choices":[{"delta":{"content":"world"}}]}',
    "data: [DONE]",
    'data: {"choices":[{"delta":{"content":"AFTER-DONE"}}]}',  # must be ignored
]


def test_iter_sse_content_deltas_orders_and_terminates():
    """Pure parser yields ordered content deltas and stops at [DONE]."""
    deltas = list(iter_sse_content_deltas(_CANNED_SSE))
    assert deltas == ["Hello", ", ", "world"]
    assert "AFTER-DONE" not in deltas


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    def iter_lines(self):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeHttpxClient:
    """Records the streamed/posted URL and replays canned SSE lines."""

    captured_url = None
    captured_post_url = None
    post_json = {"choices": [{"message": {"content": "FULL"}}]}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        _FakeHttpxClient.captured_url = url
        return _FakeStreamResponse(_CANNED_SSE)

    def post(self, url, **kwargs):
        _FakeHttpxClient.captured_post_url = url
        resp = mock.MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = _FakeHttpxClient.post_json
        return resp


@pytest.mark.parametrize(
    "base_url, expected_endpoint",
    [
        ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1/chat/completions"),
        ("http://localhost:11434/v1", "http://localhost:11434/v1/chat/completions"),
    ],
)
def test_openai_compat_same_parse_path_mlx_and_ollama(base_url, expected_endpoint):
    """MLX and Ollama differ only by base_url; the SSE parse path is identical."""
    client = OpenAICompatClient(base_url=base_url, model="local-model")
    with mock.patch("jobsmith.llm.openai_compat.httpx.Client", _FakeHttpxClient):
        deltas = list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert deltas == ["Hello", ", ", "world"]
    assert _FakeHttpxClient.captured_url == expected_endpoint


def test_openai_compat_complete_returns_full_content():
    """Non-streaming complete() returns choices[0].message.content for the scorer."""
    client = OpenAICompatClient(base_url="http://localhost:11434/v1", model="m")
    with mock.patch("jobsmith.llm.openai_compat.httpx.Client", _FakeHttpxClient):
        result = client.complete(
            [{"role": "user", "content": "score this"}],
            response_format={"type": "json_object"},
        )
    assert result == "FULL"
    assert _FakeHttpxClient.captured_post_url == (
        "http://localhost:11434/v1/chat/completions"
    )


def test_openai_compatible_provider_streams_via_client(tmp_path):
    """OpenAICompatibleProvider.send streams deltas through the shared client."""
    backend = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8080/v1",
        model="local-model",
        **_common_kwargs(tmp_path),
    )
    with mock.patch("jobsmith.llm.openai_compat.httpx.Client", _FakeHttpxClient):
        chunks = list(backend.send("hi"))
    assert chunks == ["Hello", ", ", "world"]
