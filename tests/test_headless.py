"""Tests for jobsmith.headless — claude -p subprocess wrapper + JSONL stream parser."""

from __future__ import annotations

import json
import uuid
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

from jobsmith.headless import (
    JOBSMITH_NAMESPACE,
    deterministic_session_id,
    run_phase,
    session_exists,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLUGIN_DIR = Path("/fake/plugin")
SYSTEM_PROMPT = Path("/fake/system.md")
SESSION_ID = "test-session-id"
PHASE = "gather"
PROMPT = "Do the thing."


def _jsonl(*payloads: dict) -> str:
    """Return newline-separated JSON lines."""
    return "\n".join(json.dumps(p) for p in payloads) + "\n"


def _mock_popen_with_lines(monkeypatch, lines: list[str], returncode: int = 0, stderr: str = "") -> MagicMock:
    """Patch subprocess.Popen so that proc.stdout yields *lines* one per iteration.

    Parameters
    ----------
    monkeypatch:
        pytest monkeypatch fixture.
    lines:
        List of raw strings that simulate stdout lines from ``claude -p``.
    returncode:
        The exit code the mocked process returns.
    stderr:
        Content of stderr.

    Returns
    -------
    MagicMock
        The mock Popen class (useful for asserting ``call_args``).
    """
    mock_proc = MagicMock()
    # Wrap the list in a MagicMock that also supports iteration and close().
    stdout_mock = MagicMock()
    stdout_mock.__iter__ = MagicMock(return_value=iter(lines))
    stdout_mock.close = MagicMock()
    mock_proc.stdout = stdout_mock
    mock_proc.stderr = StringIO(stderr)
    mock_proc.returncode = returncode
    mock_proc.wait.return_value = returncode

    mock_popen_cls = MagicMock(return_value=mock_proc)
    monkeypatch.setattr("jobsmith.headless.subprocess.Popen", mock_popen_cls)
    return mock_popen_cls


# ---------------------------------------------------------------------------
# 1. Command construction — happy path (no resume)
# ---------------------------------------------------------------------------


def test_command_construction_no_resume(monkeypatch):
    mock_popen_cls = _mock_popen_with_lines(monkeypatch, [])

    list(
        run_phase(
            PHASE,
            SESSION_ID,
            PROMPT,
            PLUGIN_DIR,
            SYSTEM_PROMPT,
            resume=False,
            max_turns=30,
        )
    )

    args = mock_popen_cls.call_args[0][0]

    assert args[0] == "claude"
    assert "-p" in args
    assert "--bare" not in args  # dropped: allow keychain/OAuth for Claude Max users
    assert "--plugin-dir" in args
    assert str(PLUGIN_DIR) in args
    assert "--system-prompt-file" in args
    assert str(SYSTEM_PROMPT) in args
    assert "--session-id" in args
    assert SESSION_ID in args
    assert "--output-format" in args
    assert "stream-json" in args
    assert "--verbose" in args
    assert "--allowedTools" in args
    allowed = args[args.index("--allowedTools") + 1]
    # Base tool set preserved …
    assert "Agent Bash WebFetch Read Edit Write" in allowed
    # … plus the claude-in-chrome render fallback for the JD parser (feat-2c81da71)
    assert "mcp__claude-in-chrome__navigate" in allowed
    assert "mcp__claude-in-chrome__get_page_text" in allowed
    assert "--max-turns" in args
    assert "30" in args
    # No --resume flag when resume=False (mutually exclusive)
    assert "--resume" not in args
    # Prompt is the last argument
    assert args[-1] == PROMPT


def test_command_includes_embedded_agents_json(monkeypatch, tmp_path: Path):
    """Embedded plugin agents are passed explicitly to avoid stale cwd agents."""
    plugin_dir = tmp_path / "plugin"
    agents_dir = plugin_dir / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "apply-company-research.md").write_text(
        """---
name: apply-company-research
description: Company research specialist.
tools: Read, Bash
model: sonnet
color: green
---

You are the company research specialist.
""",
        encoding="utf-8",
    )
    mock_popen_cls = _mock_popen_with_lines(monkeypatch, [])

    list(
        run_phase(
            PHASE,
            SESSION_ID,
            PROMPT,
            plugin_dir,
            SYSTEM_PROMPT,
            resume=False,
            max_turns=30,
        )
    )

    args = mock_popen_cls.call_args[0][0]
    assert "--agents" in args
    agents = json.loads(args[args.index("--agents") + 1])
    assert "apply-company-research" in agents
    assert agents["apply-company-research"]["description"] == "Company research specialist."
    assert "company research specialist" in agents["apply-company-research"]["prompt"]


# ---------------------------------------------------------------------------
# 2. Command construction — with resume
# ---------------------------------------------------------------------------


def test_command_construction_with_resume(monkeypatch):
    mock_popen_cls = _mock_popen_with_lines(monkeypatch, [])

    list(
        run_phase(
            PHASE,
            SESSION_ID,
            PROMPT,
            PLUGIN_DIR,
            SYSTEM_PROMPT,
            resume=True,
        )
    )

    args = mock_popen_cls.call_args[0][0]
    # --resume and --session-id are mutually exclusive.
    # When resume=True, only --resume should be present.
    assert "--resume" in args
    assert "--session-id" not in args
    resume_idx = args.index("--resume")
    assert args[resume_idx + 1] == SESSION_ID


# ---------------------------------------------------------------------------
# 3. JSONL parsing — text event
# ---------------------------------------------------------------------------


def test_parse_text_event(monkeypatch):
    payload = {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "Hello, world!"}]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])

    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    assert len(events) == 1
    assert events[0].type == "text"
    assert events[0].text == "Hello, world!"


# ---------------------------------------------------------------------------
# 4. JSONL parsing — tool_use event
# ---------------------------------------------------------------------------


def test_parse_tool_use_event(monkeypatch):
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "ls -la"},
                }
            ]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])

    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    assert len(events) == 1
    ev = events[0]
    assert ev.type == "tool_use"
    assert ev.tool_name == "Bash"
    assert ev.tool_input == {"command": "ls -la"}


# ---------------------------------------------------------------------------
# 5. JSONL parsing — tool_result event
# ---------------------------------------------------------------------------


def test_parse_tool_result_event(monkeypatch):
    payload = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": [{"type": "text", "text": "file1.txt\nfile2.txt"}],
                }
            ]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])

    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    assert len(events) == 1
    ev = events[0]
    assert ev.type == "tool_result"
    assert ev.tool_name == "toolu_123"
    assert "file1.txt" in ev.tool_result


# ---------------------------------------------------------------------------
# 6. Phase complete marker
# ---------------------------------------------------------------------------


def test_phase_complete_marker(monkeypatch):
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Work done. <<PHASE_COMPLETE: gather>>>"}
            ]
        },
    }
    # Add a second line that should NOT be yielded after phase_complete.
    extra = {"type": "assistant", "message": {"content": [{"type": "text", "text": "extra"}]}}
    _mock_popen_with_lines(
        monkeypatch,
        [json.dumps(payload) + "\n", json.dumps(extra) + "\n"],
    )

    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    types = [e.type for e in events]
    assert "text" in types
    assert "phase_complete" in types
    # phase_complete is the last event
    assert events[-1].type == "phase_complete"
    assert events[-1].name == "gather"
    # extra line was not processed
    text_events = [e for e in events if e.type == "text"]
    assert all("extra" not in (e.text or "") for e in text_events)


def test_phase_failed_marker_with_reason(monkeypatch):
    """`<<PHASE_FAILED: draft: prose-qa-max-iterations>>>` yields a phase_failed event
    carrying the reason on Event.error."""
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": "Halting. <<PHASE_FAILED: draft: prose-qa-max-iterations>>>",
                }
            ]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])
    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    failed = [e for e in events if e.type == "phase_failed"]
    assert len(failed) == 1
    assert failed[0].name == "draft"
    assert failed[0].error == "prose-qa-max-iterations"
    # Subsequent events not emitted (terminal marker)
    assert events[-1].type == "phase_failed"


def test_phase_failed_marker_without_reason(monkeypatch):
    """`<<PHASE_FAILED: draft>>>` (no reason) yields a phase_failed event with error=None."""
    payload = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "<<PHASE_FAILED: draft>>>"}]},
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])
    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    failed = [e for e in events if e.type == "phase_failed"]
    assert len(failed) == 1
    assert failed[0].name == "draft"
    assert failed[0].error is None


def test_phase_complete_marker_two_brackets(monkeypatch):
    """Marker with exactly 2 closing brackets (`<<PHASE_COMPLETE: gather>>`) must match."""
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Work done. <<PHASE_COMPLETE: gather>>"}
            ]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])

    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    complete = [e for e in events if e.type == "phase_complete"]
    assert len(complete) == 1
    assert complete[0].name == "gather"


def test_phase_complete_marker_three_brackets(monkeypatch):
    """Marker with exactly 3 closing brackets (regression check)."""
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Work done. <<PHASE_COMPLETE: gather>>>"}
            ]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])

    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    complete = [e for e in events if e.type == "phase_complete"]
    assert len(complete) == 1
    assert complete[0].name == "gather"


def test_phase_failed_marker_two_brackets(monkeypatch):
    """`<<PHASE_FAILED: draft : reason>>` (2 brackets) must match."""
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": "Halting. <<PHASE_FAILED: draft : test-reason>>",
                }
            ]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])
    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    failed = [e for e in events if e.type == "phase_failed"]
    assert len(failed) == 1
    assert failed[0].name == "draft"
    assert failed[0].error == "test-reason"


def test_phase_failed_gather_contracts_not_frozen(monkeypatch):
    """`<<PHASE_FAILED: gather: Contracts not frozen; freeze specialist-contracts.yaml before running apply.>>`
    yields a phase_failed event with phase=gather and reason captured."""
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": "<<PHASE_FAILED: gather: Contracts not frozen; freeze specialist-contracts.yaml before running apply.>>",
                }
            ]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])
    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    failed = [e for e in events if e.type == "phase_failed"]
    assert len(failed) == 1
    assert failed[0].name == "gather"
    assert "Contracts not frozen" in failed[0].error
    assert "freeze specialist-contracts.yaml" in failed[0].error


def test_phase_failed_with_angle_brackets_in_description(monkeypatch):
    """Regression: description containing `>` (e.g., `<id>`) must be captured fully.

    The marker contains literal angle-bracket placeholders like `<id>` and `<trk-id>`,
    which previously caused the regex to terminate early at the first `>`.
    Now with `.+?` instead of `[^>]+?`, the full description is preserved."""
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": "<<PHASE_FAILED: gather: htmlgraph PreToolUse hook blocks all Write/Bash calls until an htmlgraph work item is active. Resolve by either (a) running `htmlgraph feature start <id>` (or `htmlgraph feature create \"Apply: Duke AI Engineer\" --track <trk-id>`) before re-invoking /apply.>>",
                }
            ]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])
    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    failed = [e for e in events if e.type == "phase_failed"]
    assert len(failed) == 1
    assert failed[0].name == "gather"
    # Verify that the description captures the full text including <id> and <trk-id>
    assert failed[0].error is not None
    assert "<id>" in failed[0].error
    assert "<trk-id>" in failed[0].error
    assert "htmlgraph PreToolUse hook" in failed[0].error
    assert "feature start" in failed[0].error


def test_phase_complete_marker_one_bracket_no_match(monkeypatch):
    """Marker with only 1 closing bracket (`<<PHASE_COMPLETE: gather>`) must NOT match."""
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Work done. <<PHASE_COMPLETE: gather>"}
            ]
        },
    }
    _mock_popen_with_lines(monkeypatch, [json.dumps(payload) + "\n"])

    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    # Should be parsed as regular text, not phase_complete
    complete = [e for e in events if e.type == "phase_complete"]
    assert len(complete) == 0
    text = [e for e in events if e.type == "text"]
    assert len(text) == 1
    assert "<<PHASE_COMPLETE: gather>" in text[0].text


def test_subprocess_reaped_before_stderr_read(monkeypatch):
    """Regression: caller breaking early on phase_complete must NOT cause a hang
    because the finally block reads stderr before reaping the subprocess.

    Verifies that proc.wait (or terminate/kill) is invoked before proc.stderr.read
    in the cleanup path.
    """
    from unittest.mock import MagicMock

    call_log: list[str] = []

    mock_proc = MagicMock()
    stdout_mock = MagicMock()
    payload = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "<<PHASE_COMPLETE: gather>>>"}]},
    }
    stdout_mock.__iter__ = MagicMock(return_value=iter([json.dumps(payload) + "\n"]))
    stdout_mock.close = MagicMock(side_effect=lambda: call_log.append("stdout.close"))
    mock_proc.stdout = stdout_mock

    # Simulate "still running" so the finally branch must wait/terminate.
    mock_proc.poll = MagicMock(side_effect=lambda: call_log.append("poll") or None)
    mock_proc.wait = MagicMock(side_effect=lambda timeout=None: call_log.append(f"wait(timeout={timeout})") or 0)
    mock_proc.terminate = MagicMock(side_effect=lambda: call_log.append("terminate"))

    stderr_mock = MagicMock()
    stderr_mock.read = MagicMock(side_effect=lambda: call_log.append("stderr.read") or "")
    mock_proc.stderr = stderr_mock
    mock_proc.returncode = 0

    monkeypatch.setattr("jobsmith.headless.subprocess.Popen", MagicMock(return_value=mock_proc))

    list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    # Critical ordering: poll/wait/terminate must precede stderr.read.
    assert "stderr.read" in call_log
    stderr_idx = call_log.index("stderr.read")
    reap_calls = [c for c in call_log[:stderr_idx] if c.startswith(("wait(", "terminate"))]
    assert reap_calls, f"subprocess was not reaped before stderr.read; log={call_log}"


# ---------------------------------------------------------------------------
# 7. Malformed JSON — error event then continue
# ---------------------------------------------------------------------------


def test_malformed_json_continues(monkeypatch):
    valid_payload = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "valid"}]},
    }
    _mock_popen_with_lines(
        monkeypatch,
        ["not json\n", json.dumps(valid_payload) + "\n"],
    )

    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    assert events[0].type == "error"
    assert events[1].type == "text"
    assert events[1].text == "valid"


# ---------------------------------------------------------------------------
# 8. Non-zero exit with no events — error event
# ---------------------------------------------------------------------------


def test_nonzero_exit_yields_error(monkeypatch):
    _mock_popen_with_lines(monkeypatch, [], returncode=1, stderr="something went wrong")

    events = list(run_phase(PHASE, SESSION_ID, PROMPT, PLUGIN_DIR, SYSTEM_PROMPT))

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].raw.get("returncode") == 1


# ---------------------------------------------------------------------------
# 9. deterministic_session_id stability
# ---------------------------------------------------------------------------


def test_deterministic_session_id_stable():
    id1 = deterministic_session_id("acme-corp")
    id2 = deterministic_session_id("acme-corp")
    assert id1 == id2

    id3 = deterministic_session_id("other-corp")
    assert id1 != id3

    # Validate it's a proper UUID string.
    parsed = uuid.UUID(id1)
    assert str(parsed) == id1


def test_deterministic_session_id_uses_namespace():
    expected = str(uuid.uuid5(JOBSMITH_NAMESPACE, "test-slug"))
    assert deterministic_session_id("test-slug") == expected


# ---------------------------------------------------------------------------
# 10. session_exists
# ---------------------------------------------------------------------------


def test_session_exists_true(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    cwd = Path("/Users/alice/projects/jobsmith")
    encoded = "-Users-alice-projects-jobsmith"
    session_dir = tmp_path / ".claude" / "projects" / encoded
    session_dir.mkdir(parents=True)
    (session_dir / f"{SESSION_ID}.jsonl").write_text("{}\n")

    assert session_exists(SESSION_ID, cwd=cwd) is True


def test_session_exists_false(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    cwd = Path("/Users/alice/projects/jobsmith")
    assert session_exists("nonexistent-session", cwd=cwd) is False


def test_session_exists_default_cwd(monkeypatch, tmp_path):
    """session_exists falls back to Path.cwd() when cwd is omitted."""
    monkeypatch.setenv("HOME", str(tmp_path))

    import os
    real_cwd = Path(os.getcwd())
    encoded = str(real_cwd).replace("/", "-")
    session_dir = tmp_path / ".claude" / "projects" / encoded
    session_dir.mkdir(parents=True)
    (session_dir / f"{SESSION_ID}.jsonl").write_text("{}\n")

    assert session_exists(SESSION_ID) is True
