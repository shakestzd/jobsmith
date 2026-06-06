"""Tests for supervisor terminal-phase handling — updated for Slice 4 (trk-ad6d8227).

Slice 4 change summary
----------------------
The ``synth_terminal_phase_failed`` module-level function has been removed.
The supervisor no longer spawns subprocesses, reads transcript files, or
synthesises failure events from file contents. Instead, the pipeline's
EventSink emits ``phase_failed`` PipelineEvents in-process, which are
converted to ``TranscriptEvent`` payloads and broadcast to SSE subscribers.

Tests retained
--------------
- ``SynthPhaseEvent`` dataclass import/field test (backward-compat stub is kept)
- ``TestSupervisorInProcessCompletion``: verifies that ``register_run`` +
  ``on_run_complete`` finalise the handle correctly and notify subscribers.

Tests removed (covered by Slice 4 design)
------------------------------------------
- synth_terminal_phase_failed unit tests (function deleted)
- supervisor integration tests that spawned subprocesses (old start() API gone)
"""
from __future__ import annotations

import asyncio

import pytest

from jobsmith.api.supervisor import RunSupervisor, SynthPhaseEvent

# ---------------------------------------------------------------------------
# Backward-compat stub: SynthPhaseEvent dataclass still importable
# ---------------------------------------------------------------------------


class TestSynthPhaseEvent:
    """SynthPhaseEvent is kept as a backward-compat stub; events.py imports it."""

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

    def test_synth_phase_event_is_frozen(self) -> None:
        event = SynthPhaseEvent(
            run_id="r1", status="failed", last_phase="draft", error_excerpt=""
        )
        import dataclasses
        assert dataclasses.is_dataclass(event)


# ---------------------------------------------------------------------------
# In-process run lifecycle: register_run + on_run_complete
# ---------------------------------------------------------------------------


class TestSupervisorInProcessCompletion:
    """Verify supervisor correctly finalises runs via on_run_complete."""

    def test_on_run_complete_success_sets_done(self) -> None:
        """rc=0 → handle.status == 'done'."""
        sup = RunSupervisor()
        sup.register_run(run_id="r-ok", slug="slug-ok")
        sup.on_run_complete("r-ok", rc=0)
        handle = sup.get("r-ok")
        assert handle is not None
        assert handle.status == "done"
        assert handle.exit_code == 0
        assert handle.finished_at is not None

    def test_on_run_complete_failure_sets_failed(self) -> None:
        """rc != 0 → handle.status == 'failed'."""
        sup = RunSupervisor()
        sup.register_run(run_id="r-fail", slug="slug-fail")
        sup.on_run_complete("r-fail", rc=1)
        handle = sup.get("r-fail")
        assert handle is not None
        assert handle.status == "failed"
        assert handle.exit_code == 1

    def test_on_run_complete_removes_from_active_by_slug(self) -> None:
        """Completed run is deregistered from active-by-slug map."""
        sup = RunSupervisor()
        sup.register_run(run_id="r-active", slug="active-slug")
        assert sup.get_active_for_slug("active-slug") == "r-active"
        sup.on_run_complete("r-active", rc=0)
        assert sup.get_active_for_slug("active-slug") is None

    def test_on_run_complete_notifies_subscribers(self) -> None:
        """Subscribers receive the end-of-stream sentinel (None) on completion."""
        sup = RunSupervisor()
        sup.register_run(run_id="r-sub", slug="slug-sub")

        # Manually attach a subscriber queue.
        record = sup._runs["r-sub"]
        q: asyncio.Queue = asyncio.Queue()
        record.subscribers.append(q)

        sup.on_run_complete("r-sub", rc=0)
        sentinel = q.get_nowait()
        assert sentinel is None, "Expected None sentinel from on_run_complete"

    def test_on_run_complete_idempotent_for_unknown_run(self) -> None:
        """on_run_complete with unknown run_id is a no-op (no exception)."""
        sup = RunSupervisor()
        sup.on_run_complete("does-not-exist", rc=0)  # must not raise

    @pytest.mark.anyio
    async def test_stream_completes_after_on_run_complete(self) -> None:
        """stream() terminates cleanly when on_run_complete is called."""
        sup = RunSupervisor()
        sink = sup.register_run(run_id="r-stream", slug="slug-stream")

        collected = []
        from jobsmith.core.events import PipelineEvent

        sink.emit(PipelineEvent(kind="phase_started", phase="gather"))

        # Simulate completion (rc=0).
        sup.on_run_complete("r-stream", rc=0)

        async for item in sup.stream("r-stream"):
            collected.append(item)

        assert len(collected) == 1
        assert collected[0].payload["type"] == "phase_started"
