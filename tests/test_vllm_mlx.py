"""Tests for the vllm-mlx engine lifecycle manager (feat-0d2f3df4, slice 4).

The authoritative source for serve flags is ``docs/spikes/byo-model-apply.md``
(NOT the upstream README): the spike proved ``--continuous-batching`` crashes
gemma-4's MLLM attention path, and that tool calling needs the gemma4 parser
flags. These tests pin that contract.

Everything here is hermetic: ``subprocess.Popen`` and ``httpx`` are stubbed, the
PID-lock + chosen port live under a ``tmp_path`` data dir, and no real engine is
launched and no network is touched. One opt-in live round-trip is gated behind
``JOBSMITH_VLLM_MLX_LIVE`` so it never runs in CI.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from jobsmith.desktop import deps
from jobsmith.llm import vllm_mlx

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal stand-in for the ``subprocess.Popen`` the engine launches."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self._returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15

    def kill(self):
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):  # noqa: ANN001
        self.waited = True
        if self._returncode is None:
            self._returncode = 0
        return self._returncode


class _FakeResp:
    """Stand-in for an ``httpx.Response`` (only ``status_code`` is read)."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.fixture
def popen_spy(monkeypatch):
    """Replace ``Popen`` with a spy that records argv and returns a fake proc."""
    calls: list[list[str]] = []

    def _fake_popen(argv, *_a, **_k):
        calls.append(list(argv))
        return _FakeProc(pid=4242 + len(calls))

    monkeypatch.setattr(vllm_mlx.subprocess, "Popen", _fake_popen)
    return calls


@pytest.fixture(autouse=True)
def _reset_engine_state():
    """Clear the in-process proc registry between tests (no cross-test leak)."""
    vllm_mlx._PROCS.clear()
    yield
    vllm_mlx._PROCS.clear()


# ---------------------------------------------------------------------------
# done_when #2 (port): free-port picker + fallback
# ---------------------------------------------------------------------------


def test_select_free_port_returns_a_usable_port():
    """The picker binds 127.0.0.1:0 and returns a real ephemeral port."""
    port = vllm_mlx._select_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


def test_start_honors_a_free_preferred_port(monkeypatch, tmp_path, popen_spy):
    """A preferred port that is free is used verbatim (no fallback)."""
    monkeypatch.setattr(vllm_mlx, "is_installed", lambda: True)
    monkeypatch.setattr(vllm_mlx, "_serve_command_prefix", lambda: ["vllm-mlx"])
    monkeypatch.setattr(vllm_mlx, "_port_in_use", lambda _p: False)

    handle = vllm_mlx.start("m", port=12345, data_dir=tmp_path)
    assert handle.port == 12345
    assert "--port" in popen_spy[0]
    assert popen_spy[0][popen_spy[0].index("--port") + 1] == "12345"


def test_start_falls_back_to_free_port_when_preferred_busy(
    monkeypatch, tmp_path, popen_spy
):
    """A busy preferred port falls back to the free-port picker."""
    monkeypatch.setattr(vllm_mlx, "is_installed", lambda: True)
    monkeypatch.setattr(vllm_mlx, "_serve_command_prefix", lambda: ["vllm-mlx"])
    monkeypatch.setattr(vllm_mlx, "_port_in_use", lambda p: p == 12345)
    monkeypatch.setattr(vllm_mlx, "_select_free_port", lambda: 54321)

    handle = vllm_mlx.start("m", port=12345, data_dir=tmp_path)
    assert handle.port == 54321


# ---------------------------------------------------------------------------
# done_when #1: serve argv (gemma4 flags, NO --continuous-batching),
# PID-lock idempotency, not-installed degrade, stop() no orphan
# ---------------------------------------------------------------------------


def test_start_builds_gemma4_argv_without_continuous_batching(
    monkeypatch, tmp_path, popen_spy
):
    """The launch argv carries the gemma4 parser flags and the spike's bind,
    and MUST NOT pass --continuous-batching (it 0-tokens gemma-4)."""
    monkeypatch.setattr(vllm_mlx, "is_installed", lambda: True)
    monkeypatch.setattr(vllm_mlx, "_serve_command_prefix", lambda: ["vllm-mlx"])
    monkeypatch.setattr(vllm_mlx, "_select_free_port", lambda: 9001)

    handle = vllm_mlx.start("mlx-community/gemma-4-E4B-it-qat-4bit", data_dir=tmp_path)

    argv = popen_spy[0]
    assert argv == [
        "vllm-mlx",
        "serve",
        "mlx-community/gemma-4-E4B-it-qat-4bit",
        "--host",
        "127.0.0.1",
        "--port",
        "9001",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "gemma4",
        "--reasoning-parser",
        "gemma4",
    ]
    assert "--continuous-batching" not in argv
    assert handle.base_url == "http://127.0.0.1:9001/v1"


def test_second_concurrent_start_is_a_noop_not_a_port_collision(
    monkeypatch, tmp_path, popen_spy
):
    """A second start() while the first engine is alive returns the SAME handle
    and never launches a second process (so it can't collide on a port)."""
    monkeypatch.setattr(vllm_mlx, "is_installed", lambda: True)
    monkeypatch.setattr(vllm_mlx, "_serve_command_prefix", lambda: ["vllm-mlx"])
    monkeypatch.setattr(vllm_mlx, "_select_free_port", lambda: 9100)
    monkeypatch.setattr(vllm_mlx, "_pid_alive", lambda _pid: True)

    first = vllm_mlx.start("m", data_dir=tmp_path)
    second = vllm_mlx.start("m", data_dir=tmp_path)

    assert len(popen_spy) == 1  # only ONE process ever launched
    assert (second.pid, second.port) == (first.pid, first.port)


def test_start_replaces_a_stale_lock(monkeypatch, tmp_path, popen_spy):
    """A lock left by a dead PID is discarded and a fresh engine launches."""
    (tmp_path / vllm_mlx._LOCK_FILENAME).write_text(
        json.dumps({"pid": 999999, "port": 9000, "model": "old"}), encoding="utf-8"
    )
    monkeypatch.setattr(vllm_mlx, "is_installed", lambda: True)
    monkeypatch.setattr(vllm_mlx, "_serve_command_prefix", lambda: ["vllm-mlx"])
    monkeypatch.setattr(vllm_mlx, "_select_free_port", lambda: 9200)
    monkeypatch.setattr(vllm_mlx, "_pid_alive", lambda _pid: False)

    handle = vllm_mlx.start("m", data_dir=tmp_path)
    assert len(popen_spy) == 1
    assert handle.port == 9200


def test_start_raises_clear_install_hint_when_not_installed(
    monkeypatch, tmp_path, popen_spy
):
    """When the runtime is absent, start() raises a typed error carrying the
    guided install hint instead of an opaque Popen FileNotFoundError."""
    monkeypatch.setattr(vllm_mlx, "_serve_command_prefix", lambda: None)

    with pytest.raises(vllm_mlx.VllmMlxNotInstalledError) as exc:
        vllm_mlx.start("m", data_dir=tmp_path)

    assert vllm_mlx.INSTALL_HINT in str(exc.value)
    assert exc.value.hint == vllm_mlx.INSTALL_HINT
    assert popen_spy == []  # never attempted a launch


def test_stop_terminates_and_leaves_no_orphan(monkeypatch, tmp_path, popen_spy):
    """stop() terminates the child, reaps it (no orphan), and clears the lock."""
    monkeypatch.setattr(vllm_mlx, "is_installed", lambda: True)
    monkeypatch.setattr(vllm_mlx, "_serve_command_prefix", lambda: ["vllm-mlx"])
    monkeypatch.setattr(vllm_mlx, "_select_free_port", lambda: 9300)

    handle = vllm_mlx.start("m", data_dir=tmp_path)
    proc = vllm_mlx._PROCS[handle.pid]

    assert vllm_mlx.stop(data_dir=tmp_path) is True
    assert proc.terminated is True
    assert proc.waited is True
    assert proc.poll() is not None  # reaped — not an orphan
    assert handle.pid not in vllm_mlx._PROCS
    assert not (tmp_path / vllm_mlx._LOCK_FILENAME).exists()


def test_stop_without_a_running_engine_returns_false(tmp_path):
    """stop() on a clean data dir is a harmless no-op returning False."""
    assert vllm_mlx.stop(data_dir=tmp_path) is False


# ---------------------------------------------------------------------------
# done_when #2: health() classifier over STUBBED states
# ---------------------------------------------------------------------------


def test_health_reports_stopped_when_no_lock(tmp_path):
    snap = vllm_mlx.health(data_dir=tmp_path)
    assert snap["state"] == vllm_mlx.STATE_STOPPED
    assert snap["pid"] is None


def test_health_reports_crashed_when_process_gone(monkeypatch, tmp_path):
    """Lock present but the PID is gone ⇒ crashed (no /v1/models probe)."""
    (tmp_path / vllm_mlx._LOCK_FILENAME).write_text(
        json.dumps({"pid": 4242, "port": 9400, "model": "m"}), encoding="utf-8"
    )
    monkeypatch.setattr(vllm_mlx, "_pid_alive", lambda _pid: False)

    def _boom(*_a, **_k):
        raise AssertionError("must not probe /v1/models when the process is gone")

    monkeypatch.setattr(vllm_mlx.httpx, "get", _boom)

    snap = vllm_mlx.health(data_dir=tmp_path)
    assert snap["state"] == vllm_mlx.STATE_CRASHED
    assert snap["pid"] == 4242


def test_health_reports_loading_when_proc_up_models_not_ready(monkeypatch, tmp_path):
    """Process up but /v1/models not yet 200 ⇒ loading (cold weights)."""
    (tmp_path / vllm_mlx._LOCK_FILENAME).write_text(
        json.dumps({"pid": 4242, "port": 9500, "model": "m"}), encoding="utf-8"
    )
    monkeypatch.setattr(vllm_mlx, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(vllm_mlx.httpx, "get", lambda *_a, **_k: _FakeResp(503))

    snap = vllm_mlx.health(data_dir=tmp_path)
    assert snap["state"] == vllm_mlx.STATE_LOADING
    assert snap["base_url"] == "http://127.0.0.1:9500/v1"


def test_health_reports_loading_when_port_refuses(monkeypatch, tmp_path):
    """A connection refusal during cold load reads as loading, never crashes."""
    (tmp_path / vllm_mlx._LOCK_FILENAME).write_text(
        json.dumps({"pid": 4242, "port": 9550, "model": "m"}), encoding="utf-8"
    )
    monkeypatch.setattr(vllm_mlx, "_pid_alive", lambda _pid: True)

    def _refuse(*_a, **_k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(vllm_mlx.httpx, "get", _refuse)

    snap = vllm_mlx.health(data_dir=tmp_path)
    assert snap["state"] == vllm_mlx.STATE_LOADING


def test_health_reports_ready_on_models_200(monkeypatch, tmp_path):
    """Process up and /v1/models 200 ⇒ ready."""
    (tmp_path / vllm_mlx._LOCK_FILENAME).write_text(
        json.dumps({"pid": 4242, "port": 9600, "model": "m"}), encoding="utf-8"
    )
    monkeypatch.setattr(vllm_mlx, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(vllm_mlx.httpx, "get", lambda *_a, **_k: _FakeResp(200))

    snap = vllm_mlx.health(data_dir=tmp_path)
    assert snap["state"] == vllm_mlx.STATE_READY


def test_models_ready_classifies_raw_responses(monkeypatch):
    """The probe maps 200→True and everything else (non-200/error)→False."""
    monkeypatch.setattr(vllm_mlx.httpx, "get", lambda *_a, **_k: _FakeResp(200))
    assert vllm_mlx._models_ready(8081) is True

    monkeypatch.setattr(vllm_mlx.httpx, "get", lambda *_a, **_k: _FakeResp(503))
    assert vllm_mlx._models_ready(8081) is False

    def _refuse(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(vllm_mlx.httpx, "get", _refuse)
    assert vllm_mlx._models_ready(8081) is False


# ---------------------------------------------------------------------------
# done_when #3: deps.py detection + guided install hint (never raises)
# ---------------------------------------------------------------------------


def test_vllm_mlx_status_not_installed_emits_hint(monkeypatch):
    """Absent runtime ⇒ installed:false + the guided uv pip install hint."""
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    monkeypatch.setattr(deps, "_module_installed", lambda _name: False)

    snap = deps.vllm_mlx_status()
    assert snap == {
        "installed": False,
        "path": None,
        "install_hint": "uv pip install vllm-mlx",
    }


def test_vllm_mlx_status_installed_via_console_script(monkeypatch):
    """Console script on PATH ⇒ installed:true, path set, no hint."""
    monkeypatch.setattr(deps.shutil, "which", lambda _name: "/opt/bin/vllm-mlx")
    monkeypatch.setattr(deps, "_module_installed", lambda _name: False)

    snap = deps.vllm_mlx_status()
    assert snap["installed"] is True
    assert snap["path"] == "/opt/bin/vllm-mlx"
    assert snap["install_hint"] is None


def test_vllm_mlx_status_installed_via_module_only(monkeypatch):
    """Importable module but no console script ⇒ installed:true, path None."""
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    monkeypatch.setattr(deps, "_module_installed", lambda name: name == "vllm_mlx")

    snap = deps.vllm_mlx_status()
    assert snap["installed"] is True
    assert snap["path"] is None
    assert snap["install_hint"] is None


# ---------------------------------------------------------------------------
# Opt-in live round-trip (NEVER runs by default — needs the model + hardware)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("JOBSMITH_VLLM_MLX_LIVE"),
    reason="opt-in real-engine test; set JOBSMITH_VLLM_MLX_LIVE=1 to run",
)
def test_live_engine_cold_load_to_ready_then_stop(tmp_path):  # pragma: no cover
    """Launch the real engine, watch loading→ready, then stop with no orphan."""
    import time

    handle = vllm_mlx.start(data_dir=tmp_path)
    try:
        deadline = time.monotonic() + 120
        state = vllm_mlx.health(data_dir=tmp_path)["state"]
        while state == vllm_mlx.STATE_LOADING and time.monotonic() < deadline:
            time.sleep(2)
            state = vllm_mlx.health(data_dir=tmp_path)["state"]
        assert state == vllm_mlx.STATE_READY
    finally:
        assert vllm_mlx.stop(data_dir=tmp_path) is True
    assert not vllm_mlx._pid_alive(handle.pid)
