"""Uvicorn entry-point for the jobsmith HTTP API.

Called by the ``jobsmith api serve`` Typer subcommand.
Also used by ``jobsmith up`` (top-level convenience command, feat-2423bbec).
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import threading
import time
import webbrowser

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
_WAIT_TIMEOUT = 30.0  # seconds to wait for port to accept connections
_POLL_INTERVAL = 0.1  # seconds between socket-connect attempts

# Host literals that mean "loopback only".  A bind to anything else (0.0.0.0,
# an empty string, "::", or a specific LAN IP) is reachable by other machines
# and is treated as a public bind for auto-auth purposes.
_LOOPBACK_HOSTS = frozenset(["127.0.0.1", "::1", "localhost"])

_log = logging.getLogger(__name__)


def _is_loopback_host(host: str) -> bool:
    """Return True if *host* binds the server to the loopback interface only."""
    return host.lower() in _LOOPBACK_HOSTS


def _apply_bind_mode_env(host: str) -> None:
    """Publish the effective bind mode via env so the SPA handler can read it.

    Sets ``JOBSMITH_PUBLIC_BIND=1`` for any non-loopback bind and clears it
    otherwise.  ``api/staticui.py`` consults this flag to decide whether
    localhost auto-auth may inject a bearer token — the bind mode, not the
    spoofable request Host header, is the source of truth.
    """
    from jobsmith.api.staticui import PUBLIC_BIND_ENV_VAR

    if _is_loopback_host(host):
        os.environ.pop(PUBLIC_BIND_ENV_VAR, None)
    else:
        os.environ[PUBLIC_BIND_ENV_VAR] = "1"


def resolve_host(*, bind_public: bool) -> str:
    """Return the bind host based on the ``--bind-public`` flag.

    Parameters
    ----------
    bind_public:
        When True, bind to 0.0.0.0 (all interfaces).
        When False (default), bind to 127.0.0.1 (loopback only).
    """
    return "0.0.0.0" if bind_public else DEFAULT_HOST  # noqa: S104


def _wait_for_port(host: str, port: int, *, timeout: float = _WAIT_TIMEOUT) -> None:
    """Block (in a daemon thread) until *host*:*port* accepts TCP connections.

    Polls every ``_POLL_INTERVAL`` seconds.  Gives up silently after *timeout*
    seconds so it never hangs the process.
    """
    # When binding to 0.0.0.0, connect via loopback for the readiness probe.
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else host  # noqa: S104
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((connect_host, port), timeout=0.5):
                return  # port is accepting connections
        except OSError:
            time.sleep(_POLL_INTERVAL)


def _open_browser_after_listen(url: str, host: str, port: int) -> None:
    """Wait for the server to be ready, then open *url* in the default browser.

    Intended to be called in a daemon thread started *before* uvicorn.run so
    that the browser opens shortly after the server starts accepting connections.
    Any exception from webbrowser.open is caught and logged — a browser failure
    must never crash the server process.
    """
    _wait_for_port(host, port, timeout=_WAIT_TIMEOUT)
    try:
        webbrowser.open(url)
    except Exception:
        _log.warning("Could not open browser automatically. Open manually: %s", url)


def up_serve(host: str, port: int, *, open_browser: bool, dev: bool) -> None:
    """Start the uvicorn server for ``jobsmith up``.

    Differences from :func:`serve`:
    - Starts a daemon thread *before* blocking on uvicorn.run that waits for
      the port to accept connections and then opens the browser (A1 fix).
    - Translates EADDRINUSE into a friendly message + sys.exit(1).
    - Sets ``JOBSMITH_DEV=1`` when *dev* is True so ``create_app`` skips the
      static mount (two-process Vite hot-reload workflow).
    - ``open_browser=False`` suppresses the browser entirely (--no-open).
    """
    # Wire the dev flag via env var so the factory picks it up without a
    # signature change on create_app().
    if dev:
        os.environ["JOBSMITH_DEV"] = "1"
    else:
        os.environ.pop("JOBSMITH_DEV", None)

    # Gate localhost auto-auth on the real bind mode (not the request header).
    _apply_bind_mode_env(host)

    # The URL we will open (always loopback-friendly for display, even on 0.0.0.0).
    display_host = "127.0.0.1" if host in ("0.0.0.0", "") else host  # noqa: S104
    url = f"http://{display_host}:{port}"

    if open_browser:
        # Daemon thread: waits for port to be ready, then opens the browser.
        # Started BEFORE uvicorn.run (which blocks) — this is the A1 fix.
        t = threading.Thread(
            target=_open_browser_after_listen,
            args=(url, host, port),
            name="jobsmith-browser-opener",
            daemon=True,
        )
        t.start()

    try:
        uvicorn.run(
            "jobsmith.api.main:create_app",
            factory=True,
            host=host,
            port=port,
            reload=False,
        )
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            import sys

            print(  # noqa: T201
                f"[jobsmith] Port {port} is already in use.\n"
                f"Try a different port: jobsmith up --port <PORT>",
            )
            sys.exit(1)
        raise


def serve(host: str, port: int, reload: bool) -> None:
    """Start the uvicorn server using the create_app factory.

    Lower-level entry used by ``jobsmith api serve``.  Does not open a browser
    and does not handle port-in-use specially.
    """
    # Gate localhost auto-auth on the real bind mode (not the request header).
    _apply_bind_mode_env(host)

    uvicorn.run(
        "jobsmith.api.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
    )
