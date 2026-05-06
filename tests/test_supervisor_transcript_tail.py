"""Tests for supervisor transcript-tail (bug-0e13706c, trk-eb70f385).

The renderer in jobsmith.render writes structured agent events
(tool_call, tool_result, text, phase boundary markers) directly to
``transcript.jsonl`` without echoing them to stdout. The supervisor's
``_tail_transcript`` task watches the file and forwards each new JSON
line as a structured ``TranscriptEvent`` over the SSE stream so the UI
can render typed events instead of parsing terminal log lines.

Coverage
--------
- test_tail_emits_each_jsonl_line_as_transcript_event
- test_tail_handles_partial_writes  (renderer flushes mid-line)
- test_tail_drops_non_json_lines  (defensive)
- test_tail_drops_non_dict_payloads  (defensive)
- test_tail_handles_missing_transcript_file_then_creation
- test_tail_stops_when_run_finishes_and_file_drained
- test_tail_no_op_when_transcript_path_none
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jobsmith.api.supervisor import (
    LogLine,
    RunSupervisor,
    TranscriptEvent,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _append_jsonl(path: Path, payload: dict) -> None:
    """Append one JSON object as a single newline-terminated line."""
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")
        fh.flush()


async def _drain_stream_until_finished(supervisor: RunSupervisor, run_id: str) -> list:
    out = []
    async for item in supervisor.stream(run_id):
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTranscriptTail:
    @pytest.mark.anyio
    async def test_tail_emits_each_jsonl_line_as_transcript_event(
        self, tmp_path: Path
    ) -> None:
        """Each newline-terminated JSON line in transcript.jsonl yields a TranscriptEvent."""
        transcript = tmp_path / "transcript.jsonl"
        _append_jsonl(transcript, {"_phase_boundary": "gather", "ts": "2026-05-06T10:00:00Z"})
        _append_jsonl(transcript, {"type": "tool_call", "tool_name": "Read", "tool_use_id": "u1"})
        _append_jsonl(transcript, {"type": "tool_result", "tool_use_id": "u1", "summary": "ok"})

        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="tail-test-1",
            argv=["python", "-c", "import time; time.sleep(0.4)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        items = await _drain_stream_until_finished(supervisor, run_id)
        transcripts = [i for i in items if isinstance(i, TranscriptEvent)]
        # Three structured events tailed.
        assert len(transcripts) == 3, f"expected 3, got {len(transcripts)}: {transcripts}"
        assert transcripts[0].payload.get("_phase_boundary") == "gather"
        assert transcripts[1].payload.get("type") == "tool_call"
        assert transcripts[1].payload.get("tool_name") == "Read"
        assert transcripts[2].payload.get("type") == "tool_result"
        assert transcripts[2].payload.get("tool_use_id") == "u1"
        # Run id propagated.
        assert all(t.run_id == run_id for t in transcripts)

    @pytest.mark.anyio
    async def test_tail_handles_partial_writes(self, tmp_path: Path) -> None:
        """A partial line (no trailing newline yet) is held until the newline arrives.

        Simulates the renderer flushing mid-line (rare but possible if buffered
        I/O lands between the JSON serialization and the newline byte).
        """
        transcript = tmp_path / "transcript.jsonl"
        # Write half a JSON object with no newline.
        transcript.write_text('{"type": "tool_call", "tool_name":', encoding="utf-8")

        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="tail-test-partial",
            argv=["python", "-c", "import time; time.sleep(0.5)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        # Give the tailer a moment to read the partial line.
        await asyncio.sleep(0.2)

        # Now complete the line.
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(' "Bash"}\n')
            fh.flush()

        items = await _drain_stream_until_finished(supervisor, run_id)
        transcripts = [i for i in items if isinstance(i, TranscriptEvent)]
        assert len(transcripts) == 1
        assert transcripts[0].payload == {"type": "tool_call", "tool_name": "Bash"}

    @pytest.mark.anyio
    async def test_tail_drops_non_json_lines(self, tmp_path: Path) -> None:
        """Garbage non-JSON lines are silently dropped — never break the stream."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            "not valid json\n"
            + json.dumps({"type": "text", "text_truncated": "ok"})
            + "\n"
            + "{also broken\n",
            encoding="utf-8",
        )

        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="tail-test-bad-json",
            argv=["python", "-c", "import time; time.sleep(0.4)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        items = await _drain_stream_until_finished(supervisor, run_id)
        transcripts = [i for i in items if isinstance(i, TranscriptEvent)]
        assert len(transcripts) == 1
        assert transcripts[0].payload.get("type") == "text"

    @pytest.mark.anyio
    async def test_tail_drops_non_dict_payloads(self, tmp_path: Path) -> None:
        """JSON-decoded payloads that are NOT dicts (lists, strings) are dropped."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps([1, 2, 3]) + "\n"
            + json.dumps("a string") + "\n"
            + json.dumps({"type": "tool_call", "tool_name": "Read"}) + "\n",
            encoding="utf-8",
        )

        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="tail-test-non-dict",
            argv=["python", "-c", "import time; time.sleep(0.4)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        items = await _drain_stream_until_finished(supervisor, run_id)
        transcripts = [i for i in items if isinstance(i, TranscriptEvent)]
        assert len(transcripts) == 1
        assert transcripts[0].payload.get("tool_name") == "Read"

    @pytest.mark.anyio
    async def test_tail_handles_missing_transcript_file_then_creation(
        self, tmp_path: Path
    ) -> None:
        """The transcript file may not exist when start() runs — it appears mid-run."""
        transcript = tmp_path / "transcript.jsonl"
        # Do NOT pre-create.
        assert not transcript.exists()

        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="tail-test-late-file",
            argv=["python", "-c", "import time; time.sleep(0.5)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        # Tailer is polling. After ~150ms create the file with one event.
        await asyncio.sleep(0.15)
        _append_jsonl(transcript, {"type": "text", "text_truncated": "late"})

        items = await _drain_stream_until_finished(supervisor, run_id)
        transcripts = [i for i in items if isinstance(i, TranscriptEvent)]
        assert len(transcripts) == 1
        assert transcripts[0].payload.get("type") == "text"

    @pytest.mark.anyio
    async def test_tail_stops_when_run_finishes_and_file_drained(
        self, tmp_path: Path
    ) -> None:
        """Tailer terminates cleanly: no spinning when the subprocess is done and
        the file has nothing more to read."""
        transcript = tmp_path / "transcript.jsonl"
        _append_jsonl(transcript, {"type": "tool_call", "tool_name": "Read"})

        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="tail-test-stop",
            argv=["python", "-c", "import sys; sys.exit(0)"],
            cwd=tmp_path,
            transcript_path=transcript,
        )

        # The stream should complete in well under a second.
        items = await asyncio.wait_for(
            _drain_stream_until_finished(supervisor, run_id), timeout=2.0
        )
        # Verify we got the one event AND log lines coexist.
        transcripts = [i for i in items if isinstance(i, TranscriptEvent)]
        logs = [i for i in items if isinstance(i, LogLine)]
        assert len(transcripts) == 1
        # A successful 0-exit produces no stdout in this script — that's fine.
        assert isinstance(logs, list)  # just exercises union typing

    @pytest.mark.anyio
    async def test_tail_no_op_when_transcript_path_none(self, tmp_path: Path) -> None:
        """When transcript_path is None, no tailer is started and the stream
        contains only LogLines (no TranscriptEvents)."""
        supervisor = RunSupervisor(max_buffered_lines=100)
        run_id = await supervisor.start(
            slug="tail-test-no-path",
            argv=["python", "-c", "print('hello')"],
            cwd=tmp_path,
            transcript_path=None,
        )

        items = await _drain_stream_until_finished(supervisor, run_id)
        transcripts = [i for i in items if isinstance(i, TranscriptEvent)]
        assert transcripts == []
