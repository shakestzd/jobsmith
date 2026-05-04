"""Tests for bearer-token auth and localhost-binding defaults.

Coverage:
- Missing token → 401
- Wrong token → 401
- Correct token via env var → 200
- Correct token via file fallback → 200
- Health endpoint exempt (200 without token)
- OPTIONS preflight exempt (200 without token)
- Default bind host is 127.0.0.1
- --bind-public flips to 0.0.0.0
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.main import create_app


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """Reset the cached expected token between tests."""
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def token() -> str:
    return "test-secret-token-abc123"


@pytest.fixture()
def app_with_token_env(token: str):
    """App with JOBSMITH_API_TOKEN set in env."""
    with patch.dict(os.environ, {TOKEN_ENV_VAR: token}):
        yield create_app()


@pytest.fixture()
def client_with_token_env(app_with_token_env: FastAPI, token: str):
    """TestClient + the correct bearer token."""
    yield TestClient(app_with_token_env, raise_server_exceptions=True), token


@pytest.fixture()
def app_with_token_file(tmp_path: Path, token: str):
    """App with token stored in a file (env var absent)."""
    token_file = tmp_path / "jobsmith.token"
    token_file.write_text(token)
    token_file.chmod(0o600)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(TOKEN_ENV_VAR, None)
        with patch("jobsmith.api.auth.PRIVATE_TOKEN_PATH", token_file):
            yield create_app()


@pytest.fixture()
def client_with_token_file(app_with_token_file: FastAPI, token: str):
    yield TestClient(app_with_token_file, raise_server_exceptions=True), token


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _auth_header(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# Token validation tests (env var path)
# ---------------------------------------------------------------------------


class TestTokenEnvVar:
    def test_missing_token_returns_401(self, client_with_token_env):
        client, _ = client_with_token_env
        resp = client.get("/api/master")
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, client_with_token_env):
        client, _ = client_with_token_env
        resp = client.get("/api/master", headers={"Authorization": "Bearer wrongtoken"})
        assert resp.status_code == 401

    def test_correct_token_returns_non_401(self, client_with_token_env):
        client, token = client_with_token_env
        resp = client.get("/api/master", headers=_auth_header(token))
        # The master endpoint may 404 if no config found — that's OK; we only
        # care that auth itself passes (not 401).
        assert resp.status_code != 401

    def test_malformed_bearer_returns_401(self, client_with_token_env):
        client, token = client_with_token_env
        resp = client.get("/api/master", headers={"Authorization": token})
        assert resp.status_code == 401

    def test_bearer_wrong_scheme_returns_401(self, client_with_token_env):
        client, token = client_with_token_env
        resp = client.get("/api/master", headers={"Authorization": f"Token {token}"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Token validation tests (file fallback path)
# ---------------------------------------------------------------------------


class TestTokenFileFallback:
    def test_correct_token_from_file_returns_non_401(self, client_with_token_file):
        client, token = client_with_token_file
        resp = client.get("/api/master", headers=_auth_header(token))
        assert resp.status_code != 401

    def test_wrong_token_against_file_returns_401(self, client_with_token_file):
        client, _ = client_with_token_file
        resp = client.get("/api/master", headers={"Authorization": "Bearer wrongtoken"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Exempt endpoints
# ---------------------------------------------------------------------------


class TestExemptEndpoints:
    def test_health_no_token_returns_200(self, client_with_token_env):
        client, _ = client_with_token_env
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_wrong_token_still_200(self, client_with_token_env):
        client, _ = client_with_token_env
        resp = client.get("/health", headers={"Authorization": "Bearer wrongtoken"})
        assert resp.status_code == 200

    def test_options_preflight_no_token_not_401(self, client_with_token_env):
        """CORS preflight OPTIONS requests must not require auth."""
        client, _ = client_with_token_env
        resp = client.options(
            "/api/master",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Must not be 401 — CORS preflight should succeed unauthenticated.
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Bind host defaults
# ---------------------------------------------------------------------------


class TestBindHostDefaults:
    def test_cli_default_host_is_localhost(self):
        """The api serve command default host must be 127.0.0.1, not 0.0.0.0."""


        # Inspect the default value of `host` parameter in `api serve`.
        # We look at the registered typer command instead of running it.
        from jobsmith.api.server import DEFAULT_HOST

        assert DEFAULT_HOST == "127.0.0.1"

    def test_bind_public_flag_resolves_to_0000(self):
        """--bind-public must set host to 0.0.0.0."""
        from jobsmith.api.server import resolve_host

        assert resolve_host(bind_public=True) == "0.0.0.0"  # noqa: S104

    def test_bind_public_false_stays_localhost(self):
        from jobsmith.api.server import resolve_host

        assert resolve_host(bind_public=False) == "127.0.0.1"
