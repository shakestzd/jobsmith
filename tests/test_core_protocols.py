"""Tests for the ``jobsmith.core`` boundary types — Slice 1 of trk-ad6d8227.

Covers:
- PipelineEvent dataclass shape (back-compat with prior jobsmith.apply.PipelineEvent)
- EventSink + ConfirmGate Protocol satisfaction (runtime-checkable)
- NullEventSink swallows events without raising
- CallbackEventSink forwards to its wrapped callable
- CallbackEventSink swallows callback exceptions
- AutoYesGate always returns True
- ClickConfirmGate falls back to False on EOF / Abort
- jobsmith.apply.PipelineEvent is the same class as jobsmith.core.PipelineEvent
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jobsmith.core import (
    AutoYesGate,
    CallbackEventSink,
    ClickConfirmGate,
    ConfirmGate,
    EventSink,
    NullEventSink,
    PipelineEvent,
)

# ---------------------------------------------------------------------------
# PipelineEvent
# ---------------------------------------------------------------------------


def test_pipeline_event_default_payload_is_empty_dict() -> None:
    e = PipelineEvent(kind="phase_started", phase="gather")
    assert e.kind == "phase_started"
    assert e.phase == "gather"
    assert e.payload == {}


def test_pipeline_event_carries_payload() -> None:
    e = PipelineEvent(
        kind="slug_changed",
        phase="gather",
        payload={"old_slug": "url-x", "new_slug": "canonical-y"},
    )
    assert e.payload == {"old_slug": "url-x", "new_slug": "canonical-y"}


def test_pipeline_event_apply_compat_alias_is_same_class() -> None:
    """jobsmith.apply.PipelineEvent must be the *same* class as the core
    one so `isinstance` checks across legacy and new code paths agree."""
    from jobsmith.apply import PipelineEvent as ApplyAlias

    assert ApplyAlias is PipelineEvent


# ---------------------------------------------------------------------------
# EventSink protocol — NullEventSink + CallbackEventSink
# ---------------------------------------------------------------------------


def test_null_event_sink_satisfies_protocol_and_swallows() -> None:
    sink = NullEventSink()
    assert isinstance(sink, EventSink)
    sink.emit(PipelineEvent(kind="phase_complete", phase="gather"))


def test_callback_event_sink_forwards_to_callable() -> None:
    received: list[PipelineEvent] = []
    sink = CallbackEventSink(received.append)
    assert isinstance(sink, EventSink)

    e1 = PipelineEvent(kind="phase_started", phase="gather")
    e2 = PipelineEvent(kind="phase_complete", phase="gather")
    sink.emit(e1)
    sink.emit(e2)

    assert received == [e1, e2]


def test_callback_event_sink_swallows_callback_exceptions() -> None:
    """Sinks must never abort the pipeline. A buggy callback should be
    silently absorbed so the next emit still works."""
    n_calls = [0]

    def buggy(_event: PipelineEvent) -> None:
        n_calls[0] += 1
        raise RuntimeError("sink blew up")

    sink = CallbackEventSink(buggy)
    sink.emit(PipelineEvent(kind="phase_complete", phase="gather"))
    sink.emit(PipelineEvent(kind="phase_complete", phase="draft"))
    assert n_calls[0] == 2  # emit was retried after the first exception


# ---------------------------------------------------------------------------
# ConfirmGate protocol — AutoYesGate + ClickConfirmGate
# ---------------------------------------------------------------------------


def test_auto_yes_gate_always_proceeds() -> None:
    gate = AutoYesGate()
    assert isinstance(gate, ConfirmGate)
    assert gate.proceed(phase_name="gather", phase_num=1) is True
    assert gate.proceed(phase_name="render", phase_num=3) is True


def test_click_confirm_gate_returns_false_on_eof() -> None:
    """When stdin is closed (e.g. running under a supervisor with stdin=DEVNULL)
    the gate must not abort the process — it should decline cleanly."""
    gate = ClickConfirmGate()
    assert isinstance(gate, ConfirmGate)
    # Patch click.confirm to raise the same exception it raises on EOF
    # so the test does not depend on stdin state.
    import click

    with patch("jobsmith.core.confirm.click.confirm", side_effect=click.Abort()):
        assert gate.proceed(phase_name="gather", phase_num=1) is False
    with patch("jobsmith.core.confirm.click.confirm", side_effect=EOFError()):
        assert gate.proceed(phase_name="gather", phase_num=1) is False


def test_click_confirm_gate_returns_true_when_user_confirms() -> None:
    gate = ClickConfirmGate()
    with patch("jobsmith.core.confirm.click.confirm", return_value=True):
        assert gate.proceed(phase_name="draft", phase_num=2) is True
    with patch("jobsmith.core.confirm.click.confirm", return_value=False):
        assert gate.proceed(phase_name="draft", phase_num=2) is False


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_jobsmith_core_public_surface() -> None:
    """jobsmith.core exposes the slice-1 boundary types and nothing else yet."""
    import jobsmith.core as core

    expected = {
        "AutoYesGate",
        "CallbackEventSink",
        "ClickConfirmGate",
        "ConfirmGate",
        "EventSink",
        "NullEventSink",
        "PipelineEvent",
    }
    assert expected.issubset(set(core.__all__))


@pytest.mark.parametrize(
    "obj,proto",
    [
        (NullEventSink(), EventSink),
        (CallbackEventSink(lambda _e: None), EventSink),
        (AutoYesGate(), ConfirmGate),
        (ClickConfirmGate(), ConfirmGate),
    ],
)
def test_runtime_checkable_protocol_membership(obj, proto) -> None:
    assert isinstance(obj, proto)
