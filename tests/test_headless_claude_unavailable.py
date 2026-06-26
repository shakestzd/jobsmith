"""Regression: headless.run_phase surfaces a structured claude_unavailable
signal when the `claude` CLI is missing, instead of an unhandled crash
(feat-dac00175, slice 6).

Kept in its own file (not test_headless.py) so the slice-6 fallback assertions
have a clearly-owned home and do not collide with in-flight edits elsewhere.
"""

from __future__ import annotations

from pathlib import Path

from jobsmith import headless
from jobsmith.headless import CLAUDE_UNAVAILABLE, run_phase

PLUGIN_DIR = Path("/fake/plugin")
SYSTEM_PROMPT = Path("/fake/system.md")


def test_run_phase_missing_claude_yields_structured_error(monkeypatch):
    """Popen raising FileNotFoundError ⇒ a single type='error' event whose
    error code is 'claude_unavailable' — NOT a propagated exception."""

    def _missing_binary(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory: 'claude'")

    monkeypatch.setattr(headless.subprocess, "Popen", _missing_binary)

    # Must not raise — the generator yields a structured signal and returns.
    events = list(
        run_phase("gather", "sid", "prompt", PLUGIN_DIR, SYSTEM_PROMPT)
    )

    assert len(events) == 1
    event = events[0]
    assert event.type == "error"
    assert event.error == CLAUDE_UNAVAILABLE
    assert event.raw.get("code") == CLAUDE_UNAVAILABLE
    # A human-readable hint rides along for surfaces that render raw.message.
    assert "claude" in (event.raw.get("message") or "").lower()


def test_run_phase_present_claude_unaffected(monkeypatch):
    """When Popen succeeds, the FileNotFoundError guard is transparent: a normal
    JSONL stream parses exactly as before (no claude_unavailable event)."""
    from io import StringIO
    from unittest.mock import MagicMock

    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
    proc = MagicMock()
    stdout = MagicMock()
    stdout.__iter__ = MagicMock(return_value=iter([line]))
    stdout.close = MagicMock()
    proc.stdout = stdout
    proc.stderr = StringIO("")
    proc.returncode = 0
    proc.wait.return_value = 0
    monkeypatch.setattr(headless.subprocess, "Popen", MagicMock(return_value=proc))

    events = list(run_phase("gather", "sid", "prompt", PLUGIN_DIR, SYSTEM_PROMPT))

    assert [e.type for e in events] == ["text"]
    assert all(e.error != CLAUDE_UNAVAILABLE for e in events)
