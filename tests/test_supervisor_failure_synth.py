"""Tests for supervisor terminal-phase-failure synthesis (feat-438090af).

Coverage
--------
Unit -- synth_terminal_phase_failed():
  - test_synth_returns_none_when_terminal_success_present
  - test_synth_returns_none_when_terminal_failed_present
  - test_synth_returns_dict_when_no_terminal_event
  - test_synth_uses_last_seen_phase_from_transcript
  - test_synth_unknown_phase_when_transcript_empty
  - test_synth_handles_missing_transcript_file
  - test_synth_error_excerpt_from_stderr
  - test_synth_sigkill_returncode_negative
  - test_synth_no_event_for_zero_exit

Unit -- SynthPhaseEvent dataclass:
  - test_synth_phase_event_fields

Integration -- supervisor._wait() injects synth event:
  - test_supervisor_emits_synth_phase_on_nonzero_exit
  - test_supervisor_no_synth_when_exit_zero
  - test_supervisor_synth_visible_in_stream
  - test_supervisor_no_synth_when_transcript_has_terminal_event
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsmith.api.supervisor import RunSupervisor, SynthPhaseEvent, synth_terminal_phase_failed

# ---------------------------------------------------------------------------
# Transcript fixture helpers
# ---------------------------------------------------------------------------


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n")


def _make_phase_event(phase: str, status: str) -> dict:
    return {"type": "phase_event", "phase": phase, "status": status}


def _make_log_line(text: str) -> dict:
    return {"type": "log", "text": text}


# ---------------------------------------------------------------------------
# Unit tests: synth_terminal_phase_failed()
# ---------------------------------------------------------------------------


class TestSynthTerminalPhaseFailed:
    """Pure-function tests for synth_terminal_phase_failed."""

    def test_synth_returns_none_when_terminal_success_present(
        self, tmp_path: Path
    ) -> None:
        """When transcript already has status=success, return None."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(
            transcript,
            [
                _make_phase_event("gather", "running"),
                _make_phase_event("render", "success"),
            ],
        )
        result = synth_terminal_phase_failed(
            transcript_path=transcript,
            returncode=0,
            last_stderr_lines=[],
        )
        assert result is None

    def test_synth_returns_none_when_terminal_failed_present(
        self, tmp_path: Path
    ) -> None:
        """When transcript already has status=failed, return None (pipeline wrote it)."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(
            transcript,
            [
                _make_phase_event("gather", "running"),
                _make_phase_event("gather", "failed"),
            ],
        )
        result = synth_terminal_phase_failed(
            transcript_path=transcript,
            returncode=1,
            last_stderr_lines=["Error: something went wrong"],
        )
        assert result is None

    def test_synth_returns_dict_when_no_terminal_event(
        self, tmp_path: Path
    ) -> None:
        """When transcript has no terminal phase event, return a failure dict."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(
            transcript,
            [
                _make_phase_event("gather", "running"),
                _make_log_line("Doing some work"),
            ],
        )
        result = synth_terminal_phase_failed(
            transcript_path=transcript,
            returncode=1,
            last_stderr_lines=["Fatal error occurred"],
        )
        assert result is not None
        assert result["status"] == "failed"
        assert isinstance(result["last_phase"], str)
        assert isinstance(result["error_excerpt"], str)

    def test_synth_uses_last_seen_phase_from_transcript(
        self, tmp_path: Path
    ) -> None:
        """last_phase reflects the last phase event seen in the transcript."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(
            transcript,
            [
                _make_phase_event("gather", "running"),
                _make_phase_event("draft", "running"),
                _make_log_line("halfway through draft"),
            ],
        )
        result = synth_terminal_phase_failed(
            transcript_path=transcript,
            returncode=2,
            last_stderr_lines=[],
        )
        assert result is not None
        assert result["last_phase"] == "draft"

    def test_synth_unknown_phase_when_no_phase_events_in_transcript(
        self, tmp_path: Path
    ) -> None:
        """When transcript exists but has no phase events, last_phase='unknown'."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(transcript, [_make_log_line("some output")])
        result = synth_terminal_phase_failed(
            transcript_path=transcript,
            returncode=1,
            last_stderr_lines=[],
        )
        assert result is not None
        assert result["last_phase"] == "unknown"

    def test_synth_handles_missing_transcript_file(self, tmp_path: Path) -> None:
        """When transcript path does not exist, still return a failure dict."""
        missing = tmp_path / "nonexistent.jsonl"
        result = synth_terminal_phase_failed(
            transcript_path=missing,
            returncode=1,
            last_stderr_lines=["process died"],
        )
        assert result is not None
        assert result["status"] == "failed"
        assert result["last_phase"] == "unknown"

    def test_synth_error_excerpt_from_stderr(
        self, tmp_path: Path
    ) -> None:
        """error_excerpt includes content from last_stderr_lines."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(transcript, [_make_log_line("partial work")])
        result = synth_terminal_phase_failed(
            transcript_path=transcript,
            returncode=1,
            last_stderr_lines=["Killed", "Signal 9"],
        )
        assert result is not None
        assert "Signal 9" in result["error_excerpt"] or "Killed" in result["error_excerpt"]

    def test_synth_sigkill_returncode_negative(self, tmp_path: Path) -> None:
        """SIGKILL (returncode=-9) still synthesizes a failure event."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(
            transcript,
            [_make_phase_event("gather", "running")],
        )
        result = synth_terminal_phase_failed(
            transcript_path=transcript,
            returncode=-9,
            last_stderr_lines=[],
        )
        assert result is not None
        assert result["status"] == "failed"

    def test_synth_no_event_for_zero_exit(self, tmp_path: Path) -> None:
        """When returncode=0 and no terminal event, do NOT synthesize (exit was clean)."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(
            transcript,
            [_make_phase_event("render", "running")],
        )
        result = synth_terminal_phase_failed(
            transcript_path=transcript,
            returncode=0,
            last_stderr_lines=[],
        )
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests: SynthPhaseEvent dataclass
# ---------------------------------------------------------------------------


class TestSynthPhaseEvent:
    """SynthPhaseEvent must be importable and hold the right fields."""

    def test_synth_phase_event_fields(self) -> None:
        event = SynthPhaseEvent(
            run_id="run-abc",
            status="failed",
            last_phase="gather",
            error_excerpt="Something died",
        )
        assert event.run_id == "run-abc"
        assert event.status == "failed"
        assert event.last_phase == "gather"
        assert event.error_excerpt == "Something died"


# ---------------------------------------------------------------------------
# Integration tests: supervisor._wait() synthesizes and broadcasts
# ---------------------------------------------------------------------------


class TestSupervisorSynthIntegration:
    """Verify supervisor injects synth-phase event into subscriber stream."""

    @pytest.mark.anyio
    async def test_supervisor_emits_synth_phase_on_nonzero_exit(
        self, tmp_path: Path
    ) -> None:
        """When subprocess exits non-zero without terminal phase, stream contains SynthPhaseEvent."""
        transcript = tmp_path / "transcript.jsonl"
        # No terminal phase event -- only a running event
        _write_transcript(transcript, [_make_phase_event("gather", "running")])

        supervisor = RunSupervisor(max_buffered_lines=100)
        # Script: exit with code 1 immediately
        run_id = await supervisor.start(
            slug="test-slug",
            argv=["python", "-c", "import sys; sys.exit(1)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        collected: list = []
        async for item in supervisor.stream(run_id):
            collected.append(item)

        synth_events = [e for e in collected if isinstance(e, SynthPhaseEvent)]
        assert synth_events, (
            f"Expected at least one SynthPhaseEvent in stream, got: {collected}"
        )
        synth = synth_events[0]
        assert synth.status == "failed"
        assert synth.run_id == run_id

    @pytest.mark.anyio
    async def test_supervisor_no_synth_when_exit_zero(
        self, tmp_path: Path
    ) -> None:
        """When subprocess exits 0, no SynthPhaseEvent is emitted."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(transcript, [_make_phase_event("render", "running")])

        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="test-slug",
            argv=["python", "-c", "import sys; sys.exit(0)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        collected: list = []
        async for item in supervisor.stream(run_id):
            collected.append(item)

        synth_events = [e for e in collected if isinstance(e, SynthPhaseEvent)]
        assert not synth_events, (
            f"Expected no SynthPhaseEvent for exit 0, got: {synth_events}"
        )

    @pytest.mark.anyio
    async def test_supervisor_synth_visible_in_stream(
        self, tmp_path: Path
    ) -> None:
        """SynthPhaseEvent appears in stream after log lines."""
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(transcript, [_make_phase_event("draft", "running")])

        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="test-slug",
            argv=["python", "-c", "print('output'); import sys; sys.exit(2)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        log_lines = []
        synth_events = []
        async for item in supervisor.stream(run_id):
            if isinstance(item, SynthPhaseEvent):
                synth_events.append(item)
            else:
                log_lines.append(item)

        assert synth_events, "Expected SynthPhaseEvent in stream"
        assert synth_events[0].last_phase == "draft"
        lines_text = [ll.line for ll in log_lines]
        assert any("output" in t for t in lines_text)

    @pytest.mark.anyio
    async def test_supervisor_no_synth_when_transcript_has_terminal_event(
        self, tmp_path: Path
    ) -> None:
        """When transcript already has a terminal phase event, no SynthPhaseEvent is emitted."""
        transcript = tmp_path / "transcript.jsonl"
        # Transcript already has a 'failed' terminal event (pipeline wrote it)
        _write_transcript(
            transcript,
            [
                _make_phase_event("gather", "running"),
                _make_phase_event("gather", "failed"),
            ],
        )

        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="test-slug",
            argv=["python", "-c", "import sys; sys.exit(1)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        collected: list = []
        async for item in supervisor.stream(run_id):
            collected.append(item)

        synth_events = [e for e in collected if isinstance(e, SynthPhaseEvent)]
        assert not synth_events, (
            "Expected no SynthPhaseEvent when transcript already has terminal event, "
            f"got: {synth_events}"
        )
