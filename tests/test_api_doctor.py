"""Tests for GET /api/doctor.

Coverage:
1. GET /api/doctor without auth → 401
2. GET /api/doctor returns list of check results matching schema
3. Each item has name + status + message
4. Empty / all-pass case still returns 200 with the list
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.main import create_app
from jobsmith.doctor import CheckResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """Reset cached token between tests."""
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


TOKEN = "test-doctor-token-xyz"


@pytest.fixture()
def client():
    """TestClient with a known Bearer token set via env."""
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}):
        app = create_app()
        yield TestClient(app, raise_server_exceptions=True)


def _auth(tok: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_get_doctor_no_auth_returns_401(client: TestClient) -> None:
    """Missing token → 401."""
    resp = client.get("/api/doctor")
    assert resp.status_code == 401


def test_get_doctor_wrong_token_returns_401(client: TestClient) -> None:
    """Wrong token → 401."""
    resp = client.get("/api/doctor", headers=_auth("wrong-token"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def _passing_checks(n: int = 3) -> list[CheckResult]:
    return [CheckResult(name=f"check_{i}", ok=True, message=f"ok {i}") for i in range(n)]


def test_get_doctor_returns_200(client: TestClient) -> None:
    """Authenticated request → 200."""
    with patch("jobsmith.api.doctor.run_all_checks", return_value=_passing_checks()):
        resp = client.get("/api/doctor", headers=_auth())
    assert resp.status_code == 200


def test_get_doctor_returns_list(client: TestClient) -> None:
    """Response body is a JSON array."""
    with patch("jobsmith.api.doctor.run_all_checks", return_value=_passing_checks()):
        resp = client.get("/api/doctor", headers=_auth())
    data = resp.json()
    assert isinstance(data, list)


def test_get_doctor_schema_fields(client: TestClient) -> None:
    """Each item has name, status, message."""
    with patch("jobsmith.api.doctor.run_all_checks", return_value=_passing_checks(1)):
        resp = client.get("/api/doctor", headers=_auth())
    item = resp.json()[0]
    assert "name" in item
    assert "status" in item
    assert "message" in item


def test_get_doctor_pass_status(client: TestClient) -> None:
    """ok=True maps to status='pass'."""
    checks = [CheckResult(name="python_version", ok=True, message="3.13")]
    with patch("jobsmith.api.doctor.run_all_checks", return_value=checks):
        resp = client.get("/api/doctor", headers=_auth())
    assert resp.json()[0]["status"] == "pass"


def test_get_doctor_fail_status(client: TestClient) -> None:
    """ok=False maps to status='fail'."""
    checks = [CheckResult(name="claude_binary", ok=False, message="not found")]
    with patch("jobsmith.api.doctor.run_all_checks", return_value=checks):
        resp = client.get("/api/doctor", headers=_auth())
    assert resp.json()[0]["status"] == "fail"


def test_get_doctor_all_pass_returns_200_with_list(client: TestClient) -> None:
    """All-pass case still returns 200 with full list (not empty body)."""
    checks = _passing_checks(7)
    with patch("jobsmith.api.doctor.run_all_checks", return_value=checks):
        resp = client.get("/api/doctor", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 7
    assert all(item["status"] == "pass" for item in data)


def test_get_doctor_empty_checks_returns_200(client: TestClient) -> None:
    """Edge case: zero checks → 200 with empty list."""
    with patch("jobsmith.api.doctor.run_all_checks", return_value=[]):
        resp = client.get("/api/doctor", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_doctor_names_match(client: TestClient) -> None:
    """Check names are passed through unchanged."""
    checks = [
        CheckResult(name="python_version", ok=True, message="ok"),
        CheckResult(name="claude_binary", ok=False, message="missing"),
    ]
    with patch("jobsmith.api.doctor.run_all_checks", return_value=checks):
        resp = client.get("/api/doctor", headers=_auth())
    names = [item["name"] for item in resp.json()]
    assert names == ["python_version", "claude_binary"]
