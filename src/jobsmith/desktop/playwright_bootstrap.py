"""Desktop Playwright Chromium bootstrap (feat-0c74180d, slice 4).

Desktop-only management of the Playwright Chromium browser binary. The browser
is downloaded on first use into the per-user app-data directory — the SAME
``PLAYWRIGHT_BROWSERS_PATH`` the sidecar exports before any Playwright import
(see :mod:`jobsmith.desktop.sidecar_main`) — so the packaged desktop app ships
without a bundled ~150 MB browser and fetches it once.

Resolution of the browsers dir is kept consistent with the sidecar:
``PLAYWRIGHT_BROWSERS_PATH`` when set, otherwise
``<platformdirs user_data_dir>/ms-playwright``. ``install()`` always forces the
install env's ``PLAYWRIGHT_BROWSERS_PATH`` to the resolved path so the download
target and the runtime launch path are guaranteed identical.

This module never imports ``playwright`` at module scope: it shells out to
``python -m playwright install chromium`` so a missing browser/driver can never
break import of the API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import platformdirs

logger = logging.getLogger(__name__)

_APP_NAME = "jobsmith"
_BROWSERS_ENV = "PLAYWRIGHT_BROWSERS_PATH"

# Playwright lays each browser down as ``<name>-<revision>/`` under the browsers
# root. Chromium ships under one of these prefixes depending on the version
# (the headless-shell split landed in newer releases).
_CHROMIUM_PREFIXES = ("chromium-", "chromium_headless_shell-")

_TERMINAL_MESSAGES = {
    "idle": "Browser download has not started.",
    "done": "Chromium is installed and ready.",
    "error": "The last Chromium download failed.",
}


def browsers_path() -> Path:
    """Return the directory Playwright uses for browser binaries.

    Mirrors ``sidecar_main._prepare_env``: an explicit
    ``PLAYWRIGHT_BROWSERS_PATH`` wins, otherwise default under app-data.
    """
    env = os.environ.get(_BROWSERS_ENV, "").strip()
    if env:
        return Path(env)
    data_dir = Path(platformdirs.user_data_dir(_APP_NAME, _APP_NAME))
    return data_dir / "ms-playwright"


def _has_chromium(root: Path) -> bool:
    """True when a non-empty ``chromium-*`` browser dir exists under *root*."""
    if not root.is_dir():
        return False
    for child in root.iterdir():
        # An aborted download can leave an empty dir; require ≥1 entry so a
        # half-finished install never reports as ready.
        if (
            child.is_dir()
            and child.name.startswith(_CHROMIUM_PREFIXES)
            and any(child.iterdir())
        ):
            return True
    return False


def status() -> dict:
    """Return ``{"installed": bool, "path": str}`` for the resolved dir."""
    root = browsers_path()
    return {"installed": _has_chromium(root), "path": str(root)}


def install_command() -> list[str]:
    """The argv that downloads Chromium into the resolved browsers dir."""
    return [sys.executable, "-m", "playwright", "install", "chromium"]


def _install_env() -> dict[str, str]:
    """Process env for the install subprocess, pinning the browsers path.

    Forcing ``PLAYWRIGHT_BROWSERS_PATH`` here guarantees the download target
    equals the runtime launch path (acceptance criterion 1).
    """
    env = dict(os.environ)
    env[_BROWSERS_ENV] = str(browsers_path())
    return env


def install() -> dict:
    """Synchronously run ``playwright install chromium`` into the app-data dir.

    Returns ``{"ok", "returncode", "log"}``. Used by the network-gated
    integration test and as a simple programmatic entry; the API/SSE path uses
    :class:`_Installer` for single-flight + progress streaming.
    """
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        install_command(),
        env=_install_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    log = (proc.stdout or "") + (proc.stderr or "")
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "log": log}


class _Installer:
    """Single-flight async Chromium installer with progress fan-out.

    A module-level singleton (:func:`get_installer`) so a ``POST install`` and
    one-or-more ``GET install/events`` SSE subscribers share one download.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._history: list[dict] = []
        self._state = "idle"  # idle | running | done | error

    @property
    def state(self) -> str:
        return self._state

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _snapshot(self) -> dict:
        installed = status()["installed"]
        phase = self._state
        if phase not in ("done", "error"):
            phase = "done" if installed else "idle"
        return {
            "phase": phase,
            "message": _TERMINAL_MESSAGES.get(phase, ""),
            "installed": installed,
        }

    async def ensure_started(self) -> str:
        """Idempotently kick off a download; return a coarse status string."""
        if self.is_running():
            return "in_progress"
        if status()["installed"]:
            return "already_installed"
        self._history = []
        self._state = "running"
        self._task = asyncio.create_task(self._run())
        return "started"

    async def _emit(self, event: dict) -> None:
        self._history.append(event)
        for q in list(self._subscribers):
            q.put_nowait(event)

    async def _run(self) -> None:
        await self._emit({"phase": "start", "message": "Starting Chromium download…"})
        try:
            proc = await asyncio.create_subprocess_exec(
                *install_command(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=_install_env(),
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    await self._emit({"phase": "progress", "message": line})
            returncode = await proc.wait()
            if returncode == 0 and status()["installed"]:
                self._state = "done"
                await self._emit({"phase": "done", "message": "Chromium installed."})
            else:
                self._state = "error"
                await self._emit(
                    {
                        "phase": "error",
                        "message": f"playwright install exited with {returncode}",
                    }
                )
        except Exception as exc:  # noqa: BLE001 — never crash the server
            self._state = "error"
            logger.warning("chromium install failed: %s", exc)
            await self._emit({"phase": "error", "message": str(exc)})

    async def subscribe(self) -> AsyncIterator[dict]:
        """Yield progress events until the install terminates (done/error).

        When nothing is running, emit a single terminal snapshot and close so
        a browser ``EventSource`` does not hang.
        """
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers.add(q)
        try:
            # Replay history so a late joiner catches up on prior progress.
            for event in list(self._history):
                yield event
            if not self.is_running():
                if not self._history:
                    yield self._snapshot()
                return
            while True:
                event = await q.get()
                yield event
                if event.get("phase") in ("done", "error"):
                    return
        finally:
            self._subscribers.discard(q)


_installer: _Installer | None = None


def get_installer() -> _Installer:
    """Return the process-wide single-flight installer singleton."""
    global _installer
    if _installer is None:
        _installer = _Installer()
    return _installer


__all__ = [
    "browsers_path",
    "get_installer",
    "install",
    "install_command",
    "status",
]
