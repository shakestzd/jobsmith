"""Regression tests for ultrareview bug_029.

The S6 supervisor (feat-438090af) yields ``LogLine | SynthPhaseEvent`` from
``stream()``, but the SSE consumer in ``api/events.py`` originally accessed
``log_line.stream/.line/.timestamp`` unconditionally, which crashes on
``SynthPhaseEvent``.  These tests verify the discriminator added in the fix
correctly emits a ``phase`` SSE event for the synth payload and continues
to forward log lines.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobsmith.api.supervisor import LogLine, SynthPhaseEvent


@pytest.mark.anyio
async def test_drain_loop_forwards_synth_event_as_phase_sse():
    """SynthPhaseEvent on the queue → SSE 'phase' event with structured payload."""
    from fastapi import Request

    from jobsmith.api.events import _stream

    log_queue: asyncio.Queue = asyncio.Queue()
    synth = SynthPhaseEvent(
        run_id="run-x",
        status="failed",
        last_phase="draft",
        error_excerpt="Traceback (most recent call last)...",
    )
    log_queue.put_nowait(("log", "run-x", synth))

    request = MagicMock(spec=Request)
    request.is_disconnected = AsyncMock(return_value=False)

    supervisor = MagicMock()
    supervisor.get_active_for_slug.return_value = None

    received: list = []

    async def collect_first_phase():
        with patch("jobsmith.api.events.asyncio.Queue", return_value=log_queue):
            agen = _stream(
                request=request,
                slug="acme-swe",
                db_path=None,
                supervisor=supervisor,
                verbosity="terse",
                since_run_rowid=0,
                since_specialist_rowid=0,
                poll_interval_s=0.01,
                heartbeat_interval_s=10.0,
                idle_timeout_s=10.0,
            )
            async for evt in agen:
                received.append(evt)
                if getattr(evt, "event", None) == "phase":
                    request.is_disconnected = AsyncMock(return_value=True)
                if len(received) > 5:
                    break

    await asyncio.wait_for(collect_first_phase(), timeout=2.0)

    phase_events = [e for e in received if getattr(e, "event", None) == "phase"]
    assert len(phase_events) == 1, (
        f"Expected one phase SSE event from synth payload; got {phase_events}"
    )
    payload = json.loads(phase_events[0].data)
    assert payload["status"] == "failed"
    # Both phase and last_phase must be present so the frontend phase
    # tracker (which reads data.phase) gets a value, while diagnostic
    # consumers can still rely on data.last_phase.  Regression for
    # roborev branch-review MEDIUM (feat-90e70f1f).
    assert payload["phase"] == "draft"
    assert payload["last_phase"] == "draft"
    assert payload["run_id"] == "run-x"
    assert "Traceback" in payload["error_excerpt"]


@pytest.mark.anyio
async def test_drain_loop_still_forwards_log_lines():
    """LogLine on the queue still emits a 'log' SSE event (regression guard)."""
    from fastapi import Request

    from jobsmith.api.events import _stream

    log_queue: asyncio.Queue = asyncio.Queue()
    log_queue.put_nowait((
        "log",
        "run-x",
        LogLine(stream="stdout", line="hello world", timestamp="2026-05-05T12:00:00Z"),
    ))

    request = MagicMock(spec=Request)
    request.is_disconnected = AsyncMock(return_value=False)

    supervisor = MagicMock()
    supervisor.get_active_for_slug.return_value = None

    received: list = []

    async def collect_first_log():
        with patch("jobsmith.api.events.asyncio.Queue", return_value=log_queue):
            agen = _stream(
                request=request,
                slug="acme-swe",
                db_path=None,
                supervisor=supervisor,
                verbosity="verbose",
                since_run_rowid=0,
                since_specialist_rowid=0,
                poll_interval_s=0.01,
                heartbeat_interval_s=10.0,
                idle_timeout_s=10.0,
            )
            async for evt in agen:
                received.append(evt)
                if getattr(evt, "event", None) == "log":
                    request.is_disconnected = AsyncMock(return_value=True)
                if len(received) > 5:
                    break

    await asyncio.wait_for(collect_first_log(), timeout=2.0)

    log_events = [e for e in received if getattr(e, "event", None) == "log"]
    assert len(log_events) == 1
    payload = json.loads(log_events[0].data)
    assert payload["line"] == "hello world"
    assert payload["stream"] == "stdout"


def test_resolve_transcript_path_uses_apply_state(tmp_path):
    """_launch_run threads transcript.jsonl path through supervisor.start
    (regression for bug_029 second half)."""
    from jobsmith.api.applications import _resolve_transcript_path

    config = tmp_path / ".apply-config.yaml"
    config.write_text(
        "output:\n"
        "  applications_dir: applications\n"
        "  jobsmith_db: private/jobsmith.db\n",
        encoding="utf-8",
    )
    (tmp_path / "applications").mkdir()
    (tmp_path / "private").mkdir()

    transcript = _resolve_transcript_path("acme-swe", tmp_path)
    assert transcript is not None
    assert transcript.name == "transcript.jsonl"
    assert transcript.parent.name == ".apply-state"
    assert transcript.parent.parent.name == "acme-swe"


def test_resolve_transcript_path_returns_none_without_config(tmp_path):
    """When no config is found, _resolve_transcript_path degrades gracefully."""
    from jobsmith.api.applications import _resolve_transcript_path

    transcript = _resolve_transcript_path("acme-swe", tmp_path)
    assert transcript is None
