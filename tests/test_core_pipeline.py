"""Tests for jobsmith.core.pipeline — Slice 3b."""
from jobsmith.core import pipeline as core_pipeline
from jobsmith.core.sinks import CallbackEventSink
from jobsmith import apply as apply_mod


def test_run_phase_iter_importable_from_core():
    assert callable(core_pipeline.run_phase_iter)


def test_apply_re_export_is_same_object():
    """jobsmith.apply.run_phase_iter must be SAME object as core.pipeline.run_phase_iter
    so monkeypatch / isinstance checks across the boundary keep working."""
    assert apply_mod.run_phase_iter is core_pipeline.run_phase_iter


def test_run_phase_iter_accepts_event_sink_param():
    """The new signature accepts events: EventSink instead of (or alongside) rdr."""
    import inspect
    sig = inspect.signature(core_pipeline.run_phase_iter)
    # Must accept either 'events' or 'sink' parameter
    params = list(sig.parameters.keys())
    assert any(p in {"events", "sink"} for p in params), f"params={params}"
