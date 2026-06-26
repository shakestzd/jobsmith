"""Tests for desktop dependency detection (feat-dac00175, slice 6).

Two layers:
  - unit: ``jobsmith.desktop.deps.claude_status`` with shutil/subprocess
    monkeypatched (no live ``claude`` binary required).
  - regression (Goal 4, fully automatable): GET /api/desktop/deps/status is
    404 on a normal server and 200 only when JOBSMITH_DESKTOP=1.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.main import create_app
from jobsmith.desktop import deps

TOKEN = "test-desktop-deps-token-xyz"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


# ---------------------------------------------------------------------------
# Unit: claude_status()
# ---------------------------------------------------------------------------


def test_claude_status_not_installed(monkeypatch):
    """shutil.which → None ⇒ installed:false, no version/path probe."""
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)

    def _boom(*_a, **_k):  # the version probe must not run when absent
        raise AssertionError("subprocess.run must not be called when claude is absent")

    monkeypatch.setattr(deps.subprocess, "run", _boom)

    snap = deps.claude_status()
    assert snap == {"installed": False, "version": None, "path": None}


def test_claude_status_installed_with_version(monkeypatch):
    """Stub on PATH + parseable --version ⇒ installed:true with version+path."""
    monkeypatch.setattr(deps.shutil, "which", lambda _name: "/usr/local/bin/claude")
    monkeypatch.setattr(
        deps.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="1.2.3 (Claude Code)\n", stderr=""),
    )

    snap = deps.claude_status()
    assert snap == {"installed": True, "version": "1.2.3", "path": "/usr/local/bin/claude"}


def test_claude_status_installed_version_probe_fails(monkeypatch):
    """Stub on PATH but --version raises ⇒ installed:true, version None."""
    monkeypatch.setattr(deps.shutil, "which", lambda _name: "/usr/local/bin/claude")

    def _raise(*_a, **_k):
        raise OSError("exec format error")

    monkeypatch.setattr(deps.subprocess, "run", _raise)

    snap = deps.claude_status()
    assert snap["installed"] is True
    assert snap["path"] == "/usr/local/bin/claude"
    assert snap["version"] is None


def test_claude_status_unparseable_version_falls_back_to_raw(monkeypatch):
    """Non-semver output is returned verbatim rather than dropped."""
    monkeypatch.setattr(deps.shutil, "which", lambda _name: "/usr/local/bin/claude")
    monkeypatch.setattr(
        deps.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="claude (dev build)\n", stderr=""),
    )

    snap = deps.claude_status()
    assert snap["version"] == "claude (dev build)"


# ---------------------------------------------------------------------------
# Regression (Goal 4): JOBSMITH_DESKTOP gating of /api/desktop/deps/status
# ---------------------------------------------------------------------------


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _client(monkeypatch, *, desktop: bool) -> TestClient:
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    if desktop:
        monkeypatch.setenv("JOBSMITH_DESKTOP", "1")
    else:
        monkeypatch.delenv("JOBSMITH_DESKTOP", raising=False)
    _get_expected_token.cache_clear()
    app = create_app()
    return TestClient(app, raise_server_exceptions=True)


def test_deps_status_404_without_desktop_flag(monkeypatch):
    """No JOBSMITH_DESKTOP → the deps router is not mounted → 404."""
    client = _client(monkeypatch, desktop=False)
    resp = client.get("/api/desktop/deps/status", headers=_auth())
    assert resp.status_code == 404


def test_deps_status_200_with_desktop_flag(monkeypatch):
    """JOBSMITH_DESKTOP=1 → router mounted → authenticated 200 with schema."""
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    client = _client(monkeypatch, desktop=True)
    resp = client.get("/api/desktop/deps/status", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"claude_installed": False, "version": None, "path": None}


def test_deps_status_200_reports_installed(monkeypatch):
    """With claude present on PATH the endpoint reports claude_installed:true."""
    monkeypatch.setattr(deps.shutil, "which", lambda _name: "/usr/local/bin/claude")
    monkeypatch.setattr(
        deps.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="2.1.15 (Claude Code)\n", stderr=""),
    )
    client = _client(monkeypatch, desktop=True)
    resp = client.get("/api/desktop/deps/status", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["claude_installed"] is True
    assert body["version"] == "2.1.15"
    assert body["path"] == "/usr/local/bin/claude"


def test_deps_status_requires_auth_when_mounted(monkeypatch):
    """When mounted, /deps/status still enforces the bearer token."""
    client = _client(monkeypatch, desktop=True)
    resp = client.get("/api/desktop/deps/status")
    assert resp.status_code == 401
