"""Tests for the desktop-gated Playwright Chromium router (feat-0c74180d).

Critical regression (Goal 4): the /api/desktop/* routes exist ONLY when the
process is the desktop sidecar (JOBSMITH_DESKTOP=1). A normal server returns
404 — this is the fully-automatable acceptance criterion.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.main import create_app

TOKEN = "test-desktop-token-xyz"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _client(monkeypatch, *, desktop: bool) -> TestClient:
    """Build a TestClient. Env is set via monkeypatch so the bearer token and
    the JOBSMITH_DESKTOP gate stay in effect through every request (and revert
    automatically at test teardown)."""
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    if desktop:
        monkeypatch.setenv("JOBSMITH_DESKTOP", "1")
    else:
        monkeypatch.delenv("JOBSMITH_DESKTOP", raising=False)
    _get_expected_token.cache_clear()
    app = create_app()
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Goal-4 regression: gating on JOBSMITH_DESKTOP
# ---------------------------------------------------------------------------


def test_status_404_without_desktop_flag(monkeypatch):
    """No JOBSMITH_DESKTOP → the desktop router is not mounted → 404."""
    client = _client(monkeypatch, desktop=False)
    resp = client.get("/api/desktop/browser/status", headers=_auth())
    assert resp.status_code == 404


def test_status_200_with_desktop_flag(monkeypatch, tmp_path):
    """JOBSMITH_DESKTOP=1 → router mounted → authenticated 200 with schema."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "ms-playwright"))
    client = _client(monkeypatch, desktop=True)
    resp = client.get("/api/desktop/browser/status", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["installed"] is False
    assert body["path"] == str(tmp_path / "ms-playwright")


def test_status_requires_auth_when_mounted(monkeypatch):
    """When mounted, /status still enforces the bearer token."""
    client = _client(monkeypatch, desktop=True)
    resp = client.get("/api/desktop/browser/status")
    assert resp.status_code == 401


def test_install_route_absent_without_desktop_flag(monkeypatch):
    """No desktop flag → POST /install is not a real route.

    With the SPA static catch-all mounted (GET-only) an unmounted POST path
    surfaces as 405; without it, 404. Either way it is NOT a 2xx install ack.
    """
    client = _client(monkeypatch, desktop=False)
    resp = client.post("/api/desktop/browser/install", headers=_auth())
    assert resp.status_code in (404, 405)


def test_install_events_404_without_desktop_flag(monkeypatch):
    client = _client(monkeypatch, desktop=False)
    resp = client.get("/api/desktop/browser/install/events", headers=_auth())
    assert resp.status_code == 404


def test_install_already_installed_is_idempotent(monkeypatch, tmp_path):
    """POST install when Chromium already present → 'already_installed', no run."""
    root = tmp_path / "ms-playwright"
    leaf = root / "chromium-1097" / "chrome-linux"
    leaf.mkdir(parents=True)
    (leaf / "chrome").write_text("x", encoding="utf-8")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(root))
    client = _client(monkeypatch, desktop=True)
    resp = client.post("/api/desktop/browser/install", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_installed"
