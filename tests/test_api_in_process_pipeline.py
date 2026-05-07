"""Tests for Slice 4 — API drives apply pipeline in-process (no subprocess)."""
import inspect
from jobsmith.api.supervisor import RunSupervisor
from jobsmith.core.protocols import EventSink


def test_supervisor_register_run_returns_event_sink(tmp_path):
    """register_run replaces start; returns an EventSink."""
    sup = RunSupervisor()
    sink = sup.register_run(run_id="r-001", slug="test-slug")
    assert isinstance(sink, EventSink), f"register_run must return an EventSink, got {type(sink)}"


def test_supervisor_no_longer_has_start_method_with_subprocess_args():
    """RunSupervisor.start should be removed OR no longer accept argv/subprocess args."""
    sup = RunSupervisor()
    # Either start is gone OR its signature no longer takes argv/transcript_path
    if hasattr(sup, "start"):
        sig = inspect.signature(sup.start)
        params = sig.parameters
        # Old subprocess kwargs gone
        assert "argv" not in params, "argv kwarg lingers — subprocess path not removed"


def test_supervisor_run_record_no_subprocess_handle():
    """_RunRecord no longer holds a subprocess Popen handle."""
    sup = RunSupervisor()
    sup.register_run(run_id="r-002", slug="test")
    # Whatever internal record structure is used, no Popen
    runs = getattr(sup, "_runs", None) or getattr(sup, "runs", None)
    if runs:
        for record in runs.values():
            for attr in ("proc", "process", "popen", "subprocess"):
                assert getattr(record, attr, None) is None, (
                    f"_RunRecord still has {attr} field — subprocess path not removed"
                )


def test_synth_terminal_phase_failed_removed():
    """synth_terminal_phase_failed should no longer exist — sink emits PHASE_FAILED directly."""
    from jobsmith.api import supervisor as sup_mod
    assert not hasattr(sup_mod, "synth_terminal_phase_failed"), (
        "synth_terminal_phase_failed lingers — should be deleted in Slice 4"
    )
    # Class-level method too
    assert not hasattr(RunSupervisor, "synth_terminal_phase_failed")


def test_supervisor_register_run_registers_active_by_slug():
    """register_run makes the slug active so get_active_for_slug sees it."""
    sup = RunSupervisor()
    sup.register_run(run_id="r-003", slug="active-slug")
    assert sup.get_active_for_slug("active-slug") == "r-003"


def test_supervisor_register_run_stores_run_in_registry():
    """register_run populates _runs so get() returns a RunHandle."""
    sup = RunSupervisor()
    sup.register_run(run_id="r-004", slug="registry-slug")
    handle = sup.get("r-004")
    assert handle is not None
    assert handle.run_id == "r-004"
    assert handle.slug == "registry-slug"
    assert handle.status == "running"


def test_supervisor_sink_emit_appends_to_buffer():
    """The EventSink returned by register_run broadcasts to the buffer."""
    from jobsmith.core.events import PipelineEvent

    sup = RunSupervisor()
    sink = sup.register_run(run_id="r-005", slug="buffer-slug")
    event = PipelineEvent(kind="phase_started", phase="gather")
    sink.emit(event)

    record = sup._runs.get("r-005")
    assert record is not None
    assert len(record.buffer) == 1


def test_launch_run_no_longer_builds_argv(tmp_path):
    """_launch_run must not construct a sys.executable argv list."""
    import jobsmith.api.applications as apps_mod

    # Inspect the source code of _launch_run to ensure no sys.executable usage
    src = inspect.getsource(apps_mod._launch_run)
    assert "sys.executable" not in src, (
        "_launch_run still references sys.executable — subprocess argv not removed"
    )
    assert "argv" not in src or "argv" not in src.split("def _launch_run")[1], (
        "_launch_run still builds an argv list — subprocess path not removed"
    )


def test_resolve_db_path_removed():
    """_resolve_db_path should be removed from applications.py (it was subprocess-IPC glue)."""
    import jobsmith.api.applications as apps_mod
    assert not hasattr(apps_mod, "_resolve_db_path"), (
        "_resolve_db_path lingers in applications.py — should be deleted in Slice 4"
    )


def test_resolve_transcript_path_removed():
    """_resolve_transcript_path should be removed (transcript.jsonl IPC is gone)."""
    import jobsmith.api.applications as apps_mod
    assert not hasattr(apps_mod, "_resolve_transcript_path"), (
        "_resolve_transcript_path lingers in applications.py — should be deleted in Slice 4"
    )


def test_supervisor_kill_cancels_task_not_process():
    """kill() on the supervisor references asyncio task cancellation, not os.kill."""
    src = inspect.getsource(RunSupervisor.kill)
    # After Slice 4, kill() should not reference SIGTERM or os.killpg
    assert "SIGTERM" not in src, "kill() still references SIGTERM — process kill path lingers"
    assert "os.killpg" not in src, "kill() still uses os.killpg — subprocess not removed"


def test_render_write_transcript_no_disk_write():
    """render._write_transcript no longer opens a file handle for disk write.

    After Slice 4 the disk transcript.jsonl write is removed.
    Only _append_state_log (DB) remains.
    """
    from jobsmith.render import ApplyRenderer
    import io
    from rich.console import Console

    # Construct renderer to confirm import works; then inspect the method source.
    ApplyRenderer(
        yes=True,
        console=Console(file=io.StringIO(), force_terminal=False, no_color=True, width=80),
    )
    src = inspect.getsource(ApplyRenderer._write_transcript)
    # The disk write used self._transcript_fh.write(...) — this must be gone.
    assert "self._transcript_fh.write" not in src, (
        "render._write_transcript still writes to disk file — transcript.jsonl not removed"
    )
