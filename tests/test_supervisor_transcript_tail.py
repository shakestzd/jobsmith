"""Tests for supervisor event buffering and DB state log — updated for Slice 4 (trk-ad6d8227).

Slice 4 change summary
----------------------
The supervisor no longer spawns subprocesses or tails transcript.jsonl files.
The old ``TestTranscriptTail`` tests that called ``supervisor.start(argv=...)``
have been removed.

The DB-level tests for ``apply_state_log`` (append/read round-trips) are
retained because the pipeline still writes to that table for audit purposes.

The ``test_render_dual_writes_transcript_and_state_log`` test has been
updated: after Slice 4 the disk transcript.jsonl is no longer written, but
the DB rows are still present.

Retained tests
--------------
- DB round-trip: append_state_log + read_state_log
- DB cursor advancement: after_id semantics
- DB run_id discriminator: cross-run isolation
- DB slug rekey: run_id survives rekey_slug
- Render: open_transcript writes to DB (not disk) after Slice 4
- Supervisor: EventSink emits TranscriptEvents to subscribers
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsmith.api.supervisor import (
    RunSupervisor,
    TranscriptEvent,
)


# ---------------------------------------------------------------------------
# Supervisor EventSink → TranscriptEvent path
# ---------------------------------------------------------------------------


class TestSupervisorEventSinkBuffering:
    """The in-process EventSink buffers TranscriptEvents for SSE subscribers."""

    def test_sink_emit_creates_transcript_event(self) -> None:
        """EventSink.emit converts PipelineEvent to TranscriptEvent in buffer."""
        from jobsmith.core.events import PipelineEvent

        sup = RunSupervisor(max_buffered_lines=100)
        sink = sup.register_run(run_id="r-buf-1", slug="buf-slug")
        sink.emit(PipelineEvent(kind="phase_started", phase="gather"))

        record = sup._runs["r-buf-1"]
        assert len(record.buffer) == 1
        item = list(record.buffer)[0]
        assert isinstance(item, TranscriptEvent)
        assert item.run_id == "r-buf-1"
        assert item.payload["type"] == "phase_started"
        assert item.payload["phase"] == "gather"

    def test_sink_emit_broadcasts_to_subscriber_queues(self) -> None:
        """Sink emit pushes items to every registered subscriber queue."""
        import asyncio
        from jobsmith.core.events import PipelineEvent

        sup = RunSupervisor(max_buffered_lines=100)
        sink = sup.register_run(run_id="r-bcast", slug="bcast-slug")
        record = sup._runs["r-bcast"]

        q1: asyncio.Queue = asyncio.Queue()
        q2: asyncio.Queue = asyncio.Queue()
        record.subscribers.extend([q1, q2])

        sink.emit(PipelineEvent(kind="phase_complete", phase="gather"))

        item1 = q1.get_nowait()
        item2 = q2.get_nowait()
        assert isinstance(item1, TranscriptEvent)
        assert item1.payload["type"] == "phase_complete"
        assert item2.payload["type"] == "phase_complete"

    def test_sink_swallows_exceptions(self) -> None:
        """EventSink.emit must not raise even if internal _append fails."""
        from jobsmith.core.events import PipelineEvent
        from unittest.mock import patch

        sup = RunSupervisor(max_buffered_lines=100)
        sink = sup.register_run(run_id="r-exc", slug="exc-slug")

        with patch.object(sup, "_append", side_effect=RuntimeError("boom")):
            sink.emit(PipelineEvent(kind="phase_started", phase="gather"))
        # No exception raised — pipeline must not be aborted by a sink failure.

    @pytest.mark.anyio
    async def test_stream_yields_buffered_events(self) -> None:
        """stream() yields TranscriptEvents from buffer after on_run_complete."""
        from jobsmith.core.events import PipelineEvent

        sup = RunSupervisor(max_buffered_lines=100)
        sink = sup.register_run(run_id="r-stream-2", slug="stream-slug")
        sink.emit(PipelineEvent(kind="phase_started", phase="gather"))
        sink.emit(PipelineEvent(kind="phase_complete", phase="gather"))
        sup.on_run_complete("r-stream-2", rc=0)

        items = []
        async for item in sup.stream("r-stream-2"):
            items.append(item)

        assert len(items) == 2
        assert items[0].payload["type"] == "phase_started"
        assert items[1].payload["type"] == "phase_complete"


# ---------------------------------------------------------------------------
# trk-60217f9f Pass 4 — DB-backed state log (tests retained from before Slice 4)
# ---------------------------------------------------------------------------


def test_append_state_log_round_trip(tmp_path):
    """append_state_log + read_state_log emit the same payloads in id order."""
    from jobsmith.db import append_state_log, open_pipeline_db, read_state_log

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    try:
        id1 = append_state_log(conn, slug="acme", payload='{"event":"a"}')
        id2 = append_state_log(conn, slug="acme", payload='{"event":"b"}')
        id3 = append_state_log(conn, slug="other", payload='{"event":"x"}')
        rows = read_state_log(conn, slug="acme", after_id=0)
    finally:
        conn.close()

    assert id1 < id2 < id3, "row ids must be monotonic"
    assert [(r[0], r[2]) for r in rows] == [
        (id1, '{"event":"a"}'),
        (id2, '{"event":"b"}'),
    ], "filter by slug, exclude rows for other slugs"


def test_read_state_log_after_id_skips_already_seen(tmp_path):
    """after_id cursor advances correctly: only newer rows return."""
    from jobsmith.db import append_state_log, open_pipeline_db, read_state_log

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    try:
        id1 = append_state_log(conn, slug="acme", payload='{"n":1}')
        id2 = append_state_log(conn, slug="acme", payload='{"n":2}')
        first = read_state_log(conn, slug="acme", after_id=0)
        second = read_state_log(conn, slug="acme", after_id=id1)
        third = read_state_log(conn, slug="acme", after_id=id2)
    finally:
        conn.close()

    assert [r[0] for r in first] == [id1, id2]
    assert [r[0] for r in second] == [id2]
    assert third == []


def test_render_writes_to_state_log_not_disk(tmp_path):
    """After Slice 4: open_transcript writes boundary marker to apply_state_log;
    no disk transcript.jsonl is created.
    """
    import io
    from jobsmith.db import open_pipeline_db, read_state_log
    from jobsmith.render import ApplyRenderer
    from rich.console import Console

    db_path = tmp_path / "private" / "jobsmith.db"
    db_path.parent.mkdir(parents=True)
    open_pipeline_db(db_path).close()

    transcript_path = tmp_path / "applications" / "acme" / ".apply-state" / "transcript.jsonl"
    rdr = ApplyRenderer(
        yes=True,
        console=Console(file=io.StringIO(), force_terminal=False, no_color=True, width=120),
    )

    rdr.open_transcript(transcript_path, "gather", slug="acme", db_path=db_path)
    rdr._write_transcript({"type": "tool_use", "name": "Read"})
    rdr._write_transcript({"type": "tool_result", "ok": True})
    rdr.close_transcript()

    # Disk file must NOT exist after Slice 4.
    assert not transcript_path.exists(), (
        "transcript.jsonl must not be written to disk after Slice 4"
    )

    # apply_state_log must have boundary marker + 2 records = 3 rows.
    conn = open_pipeline_db(db_path)
    try:
        rows = read_state_log(conn, slug="acme", after_id=0)
    finally:
        conn.close()
    payloads = [json.loads(r[2]) for r in rows]
    assert len(payloads) == 3, f"expected 3 DB rows, got {len(payloads)}"
    assert payloads[0] == {"_phase_boundary": "gather", "ts": payloads[0]["ts"]}
    assert payloads[1]["type"] == "tool_use"
    assert payloads[2]["type"] == "tool_result"


# ---------------------------------------------------------------------------
# trk-60217f9f roborev job 954 — run_id discriminator on apply_state_log
# ---------------------------------------------------------------------------


def test_append_state_log_persists_run_id_column(tmp_path):
    """Migration 006 added run_id; append_state_log must populate it."""
    from jobsmith.db import append_state_log, open_pipeline_db

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    try:
        append_state_log(conn, slug="acme", payload='{"a":1}', run_id="run-A")
        row = conn.execute(
            "SELECT slug, payload, run_id FROM apply_state_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["slug"] == "acme"
    assert row["run_id"] == "run-A"


def test_read_state_log_filters_by_run_id_isolating_concurrent_runs(tmp_path):
    """The supervisor's tailer filters by run_id so cross-run pollution is bounded."""
    from jobsmith.db import append_state_log, open_pipeline_db, read_state_log

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    try:
        append_state_log(conn, slug="acme", payload='{"r":"A1"}', run_id="run-A")
        append_state_log(conn, slug="acme", payload='{"r":"B1"}', run_id="run-B")
        append_state_log(conn, slug="acme", payload='{"r":"A2"}', run_id="run-A")

        rows_a = read_state_log(conn, run_id="run-A", after_id=0)
        rows_b = read_state_log(conn, run_id="run-B", after_id=0)
    finally:
        conn.close()

    payloads_a = [r[2] for r in rows_a]
    payloads_b = [r[2] for r in rows_b]
    assert payloads_a == ['{"r":"A1"}', '{"r":"A2"}']
    assert payloads_b == ['{"r":"B1"}']


def test_read_state_log_run_id_survives_rekey_slug(tmp_path):
    """rekey_slug mutates slug column but leaves run_id untouched."""
    from jobsmith.db import (
        append_state_log,
        open_pipeline_db,
        read_state_log,
        rekey_slug,
    )

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    try:
        append_state_log(
            conn, slug="url-derived", payload='{"phase":"start"}', run_id="run-1"
        )
        rekey_slug(conn, from_slug="url-derived", to_slug="canonical-slug")
        append_state_log(
            conn, slug="canonical-slug", payload='{"phase":"end"}', run_id="run-1"
        )

        rows = read_state_log(conn, run_id="run-1", after_id=0)
    finally:
        conn.close()

    payloads = [r[2] for r in rows]
    assert payloads == ['{"phase":"start"}', '{"phase":"end"}'], (
        "tailer must see both rows even though the slug was renamed mid-run"
    )
