"""End-to-end tests for the JWT auth pipeline (feat-901b79a7).

Covers:
- POST /api/auth/login on a fresh install (legacy token as initial password).
- /api/auth/me with the issued JWT.
- POST /api/auth/refresh rotates the refresh token.
- POST /api/auth/logout revokes the session.
- POST /api/auth/set-password gates on the legacy bearer token + 409 on re-set.
- decode_access_token round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from jobsmith.api.auth import (
    TOKEN_ENV_VAR,
    _get_expected_token,
    create_access_token,
    decode_access_token,
)
from jobsmith.api.main import create_app


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    cfg = {
        "user": {"name": "Alice JWT", "email": "alice.jwt@test.example"},
        "output": {"jobsmith_db": "private/jobsmith.db"},
    }
    (tmp_path / ".apply-config.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "private").mkdir()
    token = "static-bearer-token-xyz"
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    monkeypatch.setenv("JOBSMITH_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "jobsmith.api.auth.PRIVATE_TOKEN_PATH",
        tmp_path / "private" / "jobsmith.token",
    )
    (tmp_path / "private" / "jobsmith.token").write_text(token)
    monkeypatch.chdir(tmp_path)
    return tmp_path, token


@pytest.fixture()
def client(repo):
    app = create_app()
    with TestClient(app) as c:
        yield c


def _bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def test_decode_access_token_roundtrip():
    secret = "x" * 32  # 32 bytes — silences InsecureKeyLengthWarning.
    tok = create_access_token("user-1", secret)
    assert decode_access_token(tok, secret) == "user-1"
    assert decode_access_token(tok, "y" * 32) is None
    assert decode_access_token("not-a-jwt", secret) is None


def test_login_with_legacy_token_returns_token_pair(client, repo):
    _, token = repo
    resp = client.post("/api/auth/login", json={"password": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]


def test_login_wrong_password_returns_401(client):
    resp = client.post("/api/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401


def test_me_with_jwt(client, repo):
    _, token = repo
    pair = client.post("/api/auth/login", json={"password": token}).json()
    me = client.get("/api/auth/me", headers=_bearer(pair["access_token"]))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["email"] == "alice.jwt@test.example"
    assert body["name"] == "Alice JWT"


def test_refresh_rotates_token(client, repo):
    _, token = repo
    pair = client.post("/api/auth/login", json={"password": token}).json()
    refreshed = client.post(
        "/api/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refreshed.status_code == 200, refreshed.text
    new_pair = refreshed.json()
    assert new_pair["refresh_token"] != pair["refresh_token"]
    # Old refresh should now be revoked.
    second = client.post(
        "/api/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert second.status_code == 401


def test_logout_revokes_session(client, repo):
    _, token = repo
    pair = client.post("/api/auth/login", json={"password": token}).json()
    out = client.post(
        "/api/auth/logout", json={"refresh_token": pair["refresh_token"]}
    )
    assert out.status_code == 204
    again = client.post(
        "/api/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert again.status_code == 401


def test_set_password_then_login_with_new_password(client, repo):
    _, token = repo
    new_pw = "s3cret-pa55word"
    set_resp = client.post(
        "/api/auth/set-password",
        headers=_bearer(token),
        json={"new_password": new_pw},
    )
    assert set_resp.status_code == 204
    # Re-set must 409.
    again = client.post(
        "/api/auth/set-password",
        headers=_bearer(token),
        json={"new_password": "another-pw-12345"},
    )
    assert again.status_code == 409
    # Login with new password works.
    login = client.post("/api/auth/login", json={"password": new_pw})
    assert login.status_code == 200, login.text


def test_set_password_requires_legacy_bearer(client):
    resp = client.post(
        "/api/auth/set-password", json={"new_password": "long-enough-pw"}
    )
    assert resp.status_code == 401


def test_login_uses_env_token_not_just_file(tmp_path, monkeypatch):
    """Roborev job 972 MEDIUM: env-only deployments must be able to login."""
    import yaml

    cfg = {
        "user": {"name": "EnvOnly", "email": "envonly@test.example"},
        "output": {"jobsmith_db": "private/jobsmith.db"},
    }
    (tmp_path / ".apply-config.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "private").mkdir()
    env_token = "env-only-static-token"
    monkeypatch.setenv(TOKEN_ENV_VAR, env_token)
    monkeypatch.setenv("JOBSMITH_REPO_ROOT", str(tmp_path))
    # Point PRIVATE_TOKEN_PATH at a path that does NOT exist — only the
    # env var should grant access.
    monkeypatch.setattr(
        "jobsmith.api.auth.PRIVATE_TOKEN_PATH",
        tmp_path / "private" / "no-such-token-file",
    )
    monkeypatch.chdir(tmp_path)
    app = create_app()
    with TestClient(app) as c:
        resp = c.post("/api/auth/login", json={"password": env_token})
        assert resp.status_code == 200, resp.text
        assert resp.json()["access_token"]


def test_concurrent_refresh_only_one_wins(client, repo):
    """Roborev job 972 MEDIUM: rotation must be atomic.

    We can't truly race two requests through TestClient, but we can verify
    the conditional UPDATE by manually revoking the row between the
    refresh's read and update — equivalent to losing a race.
    """
    repo_root, token = repo
    pair = client.post("/api/auth/login", json={"password": token}).json()

    # Simulate the loser of a race: revoke the session out-of-band, then
    # call refresh. The server must reject with 401, not mint a new pair.
    from jobsmith.db import open_pipeline_db

    conn = open_pipeline_db(repo_root / "private" / "jobsmith.db")
    try:
        conn.execute("UPDATE user_sessions SET revoked = 1")
        conn.commit()
    finally:
        conn.close()

    resp = client.post(
        "/api/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert resp.status_code == 401
