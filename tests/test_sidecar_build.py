"""Integration test: build the sidecar binary and exercise its contract.

Heavy + slow (a full PyInstaller onefile build). Opt-in only:

    JOBSMITH_SIDECAR_BUILD=1 uv run pytest tests/test_sidecar_build.py

Skipped by default so the fast suite stays green without PyInstaller / a build.
Also skipped when PyInstaller is not importable.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-sidecar.sh"
WEB_DIST = REPO_ROOT / "src" / "jobsmith" / "web_dist"

_BUILD_ENABLED = os.environ.get("JOBSMITH_SIDECAR_BUILD") == "1"
_PYINSTALLER = importlib.util.find_spec("PyInstaller") is not None

pytestmark = [
    pytest.mark.skipif(
        not _BUILD_ENABLED,
        reason="Set JOBSMITH_SIDECAR_BUILD=1 to run the PyInstaller sidecar build",
    ),
    pytest.mark.skipif(
        not _PYINSTALLER, reason="PyInstaller not installed (uv pip install pyinstaller)"
    ),
]

_MINIMAL_INDEX = (
    "<!doctype html><html><head><title>jobsmith</title></head>"
    "<body><div id='root'></div></body></html>"
)


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def staged_web_dist():
    """Stage a minimal web_dist/index.html (containing </head>) for the build."""
    created = not WEB_DIST.exists()
    WEB_DIST.mkdir(parents=True, exist_ok=True)
    index = WEB_DIST / "index.html"
    had_index = index.exists()
    backup = index.read_text() if had_index else None
    index.write_text(_MINIMAL_INDEX)
    try:
        yield index
    finally:
        if created:
            shutil.rmtree(WEB_DIST, ignore_errors=True)
        elif had_index and backup is not None:
            index.write_text(backup)


@pytest.fixture(scope="module")
def sidecar_binary(staged_web_dist):
    """Build the onefile sidecar and return the path to the produced binary."""
    triple = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    triple = next(
        line.split("host: ", 1)[1].strip()
        for line in triple.splitlines()
        if line.startswith("host: ")
    )

    # Ensure PyInstaller and the jobsmith src tree are reachable for the build.
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        pytest.fail(
            f"build-sidecar.sh failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    binary = REPO_ROOT / "src-tauri" / "binaries" / f"jobsmith-sidecar-{triple}"
    assert binary.exists(), f"sidecar binary not staged at {binary}"
    return binary


def test_sidecar_serves_and_shuts_down(sidecar_binary):
    # Launch with stdin held open and a PATH that contains NO python — the
    # onefile binary must be fully self-contained.
    env = {
        **os.environ,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "JOBSMITH_API_TOKEN": "sidecar-it-token-0001",
    }
    proc = subprocess.Popen(
        [str(sidecar_binary)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        # Parse the JOBSMITH_LISTENING_PORT= sentinel from stdout.
        port = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            if line.startswith("JOBSMITH_LISTENING_PORT="):
                port = int(line.strip().split("=", 1)[1])
                break
        assert port is not None, "sidecar did not emit JOBSMITH_LISTENING_PORT="
        assert _wait_for_port(port, timeout=30.0), "sidecar port never accepted connections"

        base = f"http://127.0.0.1:{port}"
        health = httpx.get(f"{base}/health", timeout=10.0)
        assert health.status_code == 200
        assert health.json().get("status") == "ok"

        index = httpx.get(f"{base}/", timeout=10.0)
        assert index.status_code == 200
        # Localhost auto-auth shim must be injected (JOBSMITH_PUBLIC_BIND popped).
        assert "window.__JOBSMITH__" in index.text
    finally:
        # Close stdin (EOF) and assert clean shutdown within 3s.
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
            pytest.fail("sidecar did not exit within 3s of stdin EOF")
