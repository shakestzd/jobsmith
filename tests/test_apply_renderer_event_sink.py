"""Tests for ApplyRenderer satisfying the EventSink protocol — Slice 3a."""
from io import StringIO

from rich.console import Console

from jobsmith.core.events import PipelineEvent
from jobsmith.core.protocols import EventSink
from jobsmith.render import ApplyRenderer


def _make_renderer() -> ApplyRenderer:
    """Create an ApplyRenderer with a no-op console for test isolation."""
    con = Console(file=StringIO(), highlight=False, markup=True)
    return ApplyRenderer(yes=True, console=con)


def test_apply_renderer_satisfies_event_sink_protocol():
    """ApplyRenderer must be a runtime-checkable EventSink."""
    rdr = ApplyRenderer.__new__(ApplyRenderer)  # bypass __init__
    assert isinstance(rdr, EventSink)


def test_apply_renderer_emit_phase_started_does_not_raise():
    """emit(phase_started) routes to internal start-phase behavior without crashing."""
    rdr = _make_renderer()
    event = PipelineEvent(kind="phase_started", phase="gather", payload={})
    rdr.emit(event)  # should not raise


def test_apply_renderer_emit_phase_complete_does_not_raise():
    rdr = _make_renderer()
    event = PipelineEvent(kind="phase_complete", phase="gather", payload={})
    rdr.emit(event)


def test_apply_renderer_emit_phase_failed_does_not_raise():
    rdr = _make_renderer()
    event = PipelineEvent(kind="phase_failed", phase="draft", payload={"rc": 1})
    rdr.emit(event)


def test_apply_renderer_emit_slug_changed_does_not_raise():
    rdr = _make_renderer()
    event = PipelineEvent(
        kind="slug_changed",
        phase="gather",
        payload={"old_slug": "old-slug", "new_slug": "new-slug"},
    )
    rdr.emit(event)


def test_apply_renderer_emit_guard_failed_does_not_raise():
    rdr = _make_renderer()
    event = PipelineEvent(kind="guard_failed", phase="gather", payload={"rc": 2})
    rdr.emit(event)


def test_apply_renderer_emit_cancelled_does_not_raise():
    rdr = _make_renderer()
    event = PipelineEvent(kind="cancelled", phase="render", payload={})
    rdr.emit(event)


def test_apply_renderer_emit_unknown_kind_no_crash():
    """A future event kind should not crash the renderer; no-op is acceptable."""
    rdr = _make_renderer()
    event = PipelineEvent(kind="future_unknown_kind", phase="gather", payload={})
    rdr.emit(event)
