"""Desktop sidecar entry point (feat-b621a4ab, slice 1).

Runs the existing jobsmith FastAPI application as a self-contained PyInstaller
onefile binary, intended to be spawned and supervised by the Tauri shell
(slice-3).  Contract with the Rust parent process:

* On startup the binary selects a free ephemeral loopback port and prints
  exactly one line ``JOBSMITH_LISTENING_PORT=<n>`` to stdout (flushed) so the
  parent can discover where the server is listening.
* The binary shuts down cleanly when its stdin reaches EOF (the parent closes
  the pipe) or when it receives ``SIGTERM``.

This module is purely additive — it does not alter the ``jobsmith up`` CLI or
the standalone FastAPI behaviour.  It only differs from ``api/server.py`` in
how it is launched and supervised.

Environment contract (applied before uvicorn / Playwright import):

* ``JOBSMITH_PUBLIC_BIND`` and ``JOBSMITH_DEV`` are removed so the localhost
  auto-auth shim is emitted and the static UI is served.
* ``JOBSMITH_DESKTOP=1`` is set (slice-4 gates a desktop router on this).
* ``PLAYWRIGHT_BROWSERS_PATH`` defaults to ``<app-data>/ms-playwright`` (only
  when the parent has not already supplied a value).
* ``JOBSMITH_API_TOKEN`` is NOT touched — the parent owns the token.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import re
import signal
import socket
import sys
import threading
from pathlib import Path

import platformdirs

_HOST = "127.0.0.1"
_APP_NAME = "jobsmith"
_PORT_SELECT_RETRIES = 5

# Redact ``token=<value>`` query-param values from uvicorn access-log records.
# The localhost auto-auth shim passes the bearer token via ``?token=`` on SSE
# endpoints (browser EventSource cannot set Authorization headers).  Uvicorn's
# access log records the full request URL, which the Tauri shell tees to a
# persistent file (~/Library/Logs/Jobsmith/sidecar.log) — so the token would be
# written to disk in cleartext.  Redacting at the source keeps it out of stdout
# entirely, before the parent ever sees it (roborev job 1056).
_TOKEN_QS_RE = re.compile(r"(token=)[^&\s'\"]+")


class _RedactTokenLogFilter(logging.Filter):
    """Logging filter that redacts ``token=<value>`` from access-log records.

    Uvicorn's access logger emits the request line via ``record.args``
    (client_addr, method, full_path, http_version, status_code).  We rewrite
    the string args in place before any handler formats them, leaving non-string
    args (e.g. the integer status code) untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                _TOKEN_QS_RE.sub(r"\1REDACTED", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def _redacting_log_config() -> dict:
    """Return uvicorn's default logging config with token redaction added.

    Attaches :class:`_RedactTokenLogFilter` to uvicorn's ``access`` handler so
    the bearer token never reaches the persisted sidecar log.
    """
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_token"] = {
        "()": f"{__name__}._RedactTokenLogFilter"
    }
    access_handler = config.get("handlers", {}).get("access")
    if access_handler is not None:
        access_handler.setdefault("filters", []).append("redact_token")
    return config


def _resolve_data_dir() -> Path:
    """Return the per-user application data directory for jobsmith."""
    return Path(platformdirs.user_data_dir(_APP_NAME, _APP_NAME))


def _prepare_env() -> Path:
    """Normalise process env for the desktop sidecar; return the data dir.

    Must run BEFORE uvicorn or Playwright are imported so the Playwright
    browser path is in effect for the first import.
    """
    # Force loopback auto-auth + static UI on: clear the flags that would
    # suppress the token shim or the static mount.
    os.environ.pop("JOBSMITH_PUBLIC_BIND", None)
    os.environ.pop("JOBSMITH_DEV", None)

    # Mark this process as the desktop sidecar (slice-4 gates a router on it).
    os.environ["JOBSMITH_DESKTOP"] = "1"

    data_dir = _resolve_data_dir()
    # setdefault: never clobber a path the parent (Tauri) already exported.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(data_dir / "ms-playwright"))
    return data_dir


def _select_port(retries: int = _PORT_SELECT_RETRIES) -> int:
    """Return a free ephemeral loopback port.

    Binds a socket to ``127.0.0.1:0``, reads the kernel-assigned port, and
    closes the socket.  Retries a few times to ride out transient bind races.
    """
    last_exc: OSError | None = None
    for _ in range(max(1, retries)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((_HOST, 0))
                return int(sock.getsockname()[1])
        except OSError as exc:  # pragma: no cover - exercised only under races
            last_exc = exc
    raise RuntimeError("could not select a free ephemeral port") from last_exc


def _emit_port(port: int, stream=sys.stdout) -> None:
    """Print the port-discovery sentinel line and flush so the parent sees it."""
    stream.write(f"JOBSMITH_LISTENING_PORT={port}\n")
    stream.flush()


def _watch_stdin_eof() -> None:
    """Block on stdin; exit the process when the parent closes the pipe."""
    with contextlib.suppress(Exception):
        sys.stdin.read()
    os._exit(0)


def _install_stdin_eof_shutdown() -> None:
    """Start a daemon thread that exits the process on stdin EOF."""
    thread = threading.Thread(
        target=_watch_stdin_eof, name="jobsmith-sidecar-stdin-eof", daemon=True
    )
    thread.start()


def _install_sigterm_handler() -> None:
    """Exit cleanly on SIGTERM (the parent's graceful-stop signal)."""

    def _handle(_signum, _frame):  # noqa: ANN001
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle)


def main() -> None:
    """Sidecar entry point: prepare env, announce port, then run uvicorn."""
    _prepare_env()

    port = _select_port()
    _emit_port(port)

    _install_stdin_eof_shutdown()
    _install_sigterm_handler()

    # Imported here (not at module top) so the env contract above — notably
    # PLAYWRIGHT_BROWSERS_PATH — is in effect before any heavy import.
    import uvicorn

    uvicorn.run(
        "jobsmith.api.main:create_app",
        factory=True,
        host=_HOST,
        port=port,
        reload=False,
        log_config=_redacting_log_config(),
    )


if __name__ == "__main__":
    main()
