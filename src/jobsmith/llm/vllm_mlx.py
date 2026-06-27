"""vllm-mlx local engine lifecycle manager (feat-0d2f3df4, slice 4).

The code-orchestrated LOCAL apply path needs a reliable local engine serving
gemma-4. This module owns that engine *process*: start it cleanly, signal the
cold load, never race under concurrent applies, and degrade with a clear install
hint instead of crashing.

Authoritative source for the serve contract is ``docs/spikes/byo-model-apply.md``
(NOT the upstream README). Two hard-won facts from the spike:

* Tool calling on gemma-4 requires ``--enable-auto-tool-choice
  --tool-call-parser gemma4 --reasoning-parser gemma4``.
* ``--continuous-batching`` (the upstream README default) crashes gemma-4's MLLM
  attention path (``shared_kv`` TypeError → 0 tokens). We never pass it.

Concurrency model: a single in-process :data:`_START_LOCK` serialises the start
critical section (concurrent applies live in one API process), and a PID-lock
file under the app data dir makes a second ``start()`` a no-op rather than a port
collision — across processes too. The lock file also records the chosen port so
:func:`health` and :func:`stop` can find the engine without guessing.

This module launches no process and opens no socket at import time; everything is
lazy so importing it (e.g. from desktop detection) is free.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx
import platformdirs

# --- constants --------------------------------------------------------------

_APP_NAME = "jobsmith"
_HOST = "127.0.0.1"

# Console script (hyphen) and importable module (underscore). The spike drives
# the console script `vllm-mlx serve`; the module form is a launch fallback.
_RUNTIME_BINARY = "vllm-mlx"
_RUNTIME_MODULE = "vllm_mlx"

# Default served model (the spike's gemma-4 E4B QAT build).
_DEFAULT_MODEL = "mlx-community/gemma-4-E4B-it-qat-4bit"

# Guided remediation surfaced when the runtime is absent (single source of truth
# for the engine module; desktop/deps.py mirrors this string for its status API).
INSTALL_HINT = "uv pip install vllm-mlx"

# PID-lock + chosen-port record, plus a place to drain the engine's stdio so a
# full pipe can never wedge (and orphan) the child. Both live under the data dir.
_LOCK_FILENAME = "vllm-mlx.lock.json"
_LOG_FILENAME = "vllm-mlx.log"

# Free-port picker retries (ride out transient bind races).
_PORT_SELECT_RETRIES = 5

# /v1/models health probe + graceful-stop bounds. The probe is short: a cold
# engine refuses/!=200 (→ loading), a ready one answers fast.
_HEALTH_TIMEOUT_S = 1.0
_STOP_TIMEOUT_S = 5.0

# Health classifications.
STATE_READY = "ready"  # process up AND /v1/models == 200
STATE_LOADING = "loading"  # process up, /v1/models not yet 200 (~20-30s cold)
STATE_CRASHED = "crashed"  # lock present but the process is gone
STATE_STOPPED = "stopped"  # no engine recorded

# Serialises the start critical section within this process; the PID-lock file
# extends idempotency across processes.
_START_LOCK = threading.Lock()

# In-process pid -> Popen registry so stop()/health() can reach the live child
# object (clean terminate + reap). A cross-process caller falls back to signals.
_PROCS: dict[int, subprocess.Popen] = {}


class VllmMlxNotInstalledError(RuntimeError):
    """Raised by :func:`start` when the vllm-mlx runtime is not installed.

    Carries the guided :data:`INSTALL_HINT` so callers can surface an actionable
    message instead of an opaque ``FileNotFoundError`` from ``Popen``.
    """

    def __init__(self, hint: str = INSTALL_HINT) -> None:
        super().__init__(f"vllm-mlx is not installed. Install it with: {hint}")
        self.hint = hint


@dataclass(frozen=True)
class EngineHandle:
    """Identity of a managed engine: its PID, loopback port, and served model."""

    pid: int
    port: int
    model: str

    @property
    def base_url(self) -> str:
        """OpenAI/Anthropic-compatible base URL (includes the ``/v1`` suffix)."""
        return f"http://{_HOST}:{self.port}/v1"


# --- installation detection -------------------------------------------------


def _module_available(name: str) -> bool:
    """Return True when ``name`` is importable, without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _serve_command_prefix() -> list[str] | None:
    """Return the argv prefix that launches the engine, or None when absent.

    Prefers the ``vllm-mlx`` console script (as the spike used); falls back to
    ``<python> -m vllm_mlx`` when only the importable module is present.
    """
    path = shutil.which(_RUNTIME_BINARY)
    if path:
        return [path]
    if _module_available(_RUNTIME_MODULE):
        return [sys.executable, "-m", _RUNTIME_MODULE]
    return None


def is_installed() -> bool:
    """Return True when the vllm-mlx runtime can be launched."""
    return _serve_command_prefix() is not None


# --- ports ------------------------------------------------------------------


def _select_free_port(retries: int = _PORT_SELECT_RETRIES) -> int:
    """Return a free ephemeral loopback port (bind :0, read, release)."""
    last_exc: OSError | None = None
    for _ in range(max(1, retries)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((_HOST, 0))
                return int(sock.getsockname()[1])
        except OSError as exc:  # pragma: no cover - only under bind races
            last_exc = exc
    raise RuntimeError("could not select a free ephemeral port") from last_exc


def _port_in_use(port: int) -> bool:
    """Return True when something is already listening on ``127.0.0.1:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((_HOST, port)) == 0


def _resolve_port(preferred: int | None) -> int:
    """Pick the port to bind: a free preferred one, else a free-port fallback."""
    if preferred is not None and not _port_in_use(preferred):
        return preferred
    return _select_free_port()


# --- lock file --------------------------------------------------------------


def _resolve_data_dir() -> Path:
    """Return the per-user application data directory for jobsmith."""
    return Path(platformdirs.user_data_dir(_APP_NAME, _APP_NAME))


def _lock_path(data_dir: Path) -> Path:
    return data_dir / _LOCK_FILENAME


def _read_handle(data_dir: Path) -> EngineHandle | None:
    """Read the recorded engine handle, or None when no/invalid lock exists."""
    path = _lock_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return EngineHandle(
            pid=int(raw["pid"]), port=int(raw["port"]), model=str(raw["model"])
        )
    except (KeyError, TypeError, ValueError):
        return None


def _write_handle(data_dir: Path, handle: EngineHandle) -> None:
    """Atomically persist the engine handle to the lock file."""
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {"pid": handle.pid, "port": handle.port, "model": handle.model}
    tmp = _lock_path(data_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, _lock_path(data_dir))


def _clear_lock(data_dir: Path) -> None:
    with contextlib.suppress(OSError):
        _lock_path(data_dir).unlink()


# --- process liveness + termination -----------------------------------------


def _pid_alive(pid: int) -> bool:
    """Return True when ``pid`` is a live process (best-effort, never raises)."""
    proc = _PROCS.get(pid)
    if proc is not None:
        return proc.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but owned by another user
        return True
    except OSError:
        return False
    return True


def _terminate(handle: EngineHandle, proc: subprocess.Popen | None) -> None:
    """Terminate the engine and reap it so no orphan/zombie is left behind."""
    if proc is not None:
        _terminate_proc(proc)
        return
    _terminate_by_pid(handle.pid)


def _terminate_proc(proc: subprocess.Popen) -> None:
    """SIGTERM the tracked child, escalate to SIGKILL, then reap it."""
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=_STOP_TIMEOUT_S)
        return
    except Exception:  # noqa: BLE001 - timeout or platform error → escalate
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=_STOP_TIMEOUT_S)


def _terminate_by_pid(pid: int) -> None:
    """Cross-process stop: SIGTERM, then SIGKILL if it lingers."""
    if not _signal_pid(pid, signal.SIGTERM):
        return
    if _wait_pid_gone(pid, _STOP_TIMEOUT_S):
        return
    _signal_pid(pid, signal.SIGKILL)


def _signal_pid(pid: int, sig: int) -> bool:
    """Send ``sig`` to ``pid``; return False when the process is already gone."""
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, OSError):
        return False
    return True


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` exits or ``timeout`` elapses; return True if it exited."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


# --- launch -----------------------------------------------------------------


def _build_serve_argv(prefix: list[str], model: str, port: int) -> list[str]:
    """Build the full serve argv per the spike (no --continuous-batching)."""
    return [
        *prefix,
        "serve",
        model,
        "--host",
        _HOST,
        "--port",
        str(port),
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "gemma4",
        "--reasoning-parser",
        "gemma4",
    ]


def _spawn(argv: list[str], data_dir: Path) -> subprocess.Popen:
    """Launch the engine detached, draining stdio to a log file (no pipe wedge)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log = open(data_dir / _LOG_FILENAME, "ab")  # noqa: SIM115 - child owns it
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- public API -------------------------------------------------------------


def start(
    model: str = _DEFAULT_MODEL,
    *,
    port: int | None = None,
    data_dir: Path | None = None,
) -> EngineHandle:
    """Start the vllm-mlx engine, or return the live one (idempotent).

    Idempotent under concurrency: if an engine recorded in the lock file is still
    alive, this is a no-op and returns its handle (never a second launch / port
    collision). A stale lock (dead PID) is discarded. Picks a free port (a busy
    ``port`` falls back to an ephemeral one). Raises
    :class:`VllmMlxNotInstalledError` — carrying :data:`INSTALL_HINT` — when the
    runtime is absent, rather than crashing with a Popen ``FileNotFoundError``.
    """
    data_dir = data_dir or _resolve_data_dir()
    with _START_LOCK:
        existing = _read_handle(data_dir)
        if existing is not None and _pid_alive(existing.pid):
            return existing
        if existing is not None:
            _clear_lock(data_dir)  # stale

        prefix = _serve_command_prefix()
        if prefix is None:
            raise VllmMlxNotInstalledError()

        chosen_port = _resolve_port(port)
        argv = _build_serve_argv(prefix, model, chosen_port)
        proc = _spawn(argv, data_dir)
        handle = EngineHandle(pid=proc.pid, port=chosen_port, model=model)
        _PROCS[proc.pid] = proc
        _write_handle(data_dir, handle)
        return handle


def stop(*, data_dir: Path | None = None) -> bool:
    """Stop the recorded engine and clear the lock; return False if none ran.

    Terminates and reaps the child so no orphan process survives, then removes
    the lock file. Safe to call when nothing is running.
    """
    data_dir = data_dir or _resolve_data_dir()
    with _START_LOCK:
        handle = _read_handle(data_dir)
        if handle is None:
            return False
        proc = _PROCS.pop(handle.pid, None)
        _terminate(handle, proc)
        _clear_lock(data_dir)
        return True


def _models_ready(port: int, timeout: float = _HEALTH_TIMEOUT_S) -> bool:
    """Return True iff ``GET /v1/models`` answers 200 (any failure → False)."""
    url = f"http://{_HOST}:{port}/v1/models"
    try:
        resp = httpx.get(url, timeout=timeout)
    except (httpx.HTTPError, OSError):
        return False
    return resp.status_code == 200


def health(*, data_dir: Path | None = None) -> dict:
    """Classify the recorded engine as stopped / crashed / loading / ready.

    * **stopped** — no engine recorded.
    * **crashed** — lock present but the process is gone.
    * **loading** — process up, ``/v1/models`` not yet 200 (~20-30s cold load).
    * **ready**   — process up and ``/v1/models`` answers 200.

    Returns ``{"state", "pid", "port", "base_url", "model"}``; never raises.
    """
    data_dir = data_dir or _resolve_data_dir()
    handle = _read_handle(data_dir)
    if handle is None:
        return {
            "state": STATE_STOPPED,
            "pid": None,
            "port": None,
            "base_url": None,
            "model": None,
        }
    if not _pid_alive(handle.pid):
        state = STATE_CRASHED
    elif _models_ready(handle.port):
        state = STATE_READY
    else:
        state = STATE_LOADING
    return {
        "state": state,
        "pid": handle.pid,
        "port": handle.port,
        "base_url": handle.base_url,
        "model": handle.model,
    }


__all__ = [
    "INSTALL_HINT",
    "STATE_CRASHED",
    "STATE_LOADING",
    "STATE_READY",
    "STATE_STOPPED",
    "EngineHandle",
    "VllmMlxNotInstalledError",
    "health",
    "is_installed",
    "start",
    "stop",
]
