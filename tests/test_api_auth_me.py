"""Integration test for GET /api/auth/me (feat-ddd98f7d)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.main import create_app


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


@pytest.fixture()
def repo_with_user(tmp_path: Path) -> tuple[Path, str, str]:
    """Scaffold a minimal jobsmith repo with config + user identity."""
    cfg = {
        "user": {
            "name": "Alice Tester",
            "email": "alice@test.example",
        },
        "output": {
            "jobsmith_db": "private/jobsmith.db",
        },
    }
    (tmp_path / ".apply-config.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "private").mkdir()
    return tmp_path, "alice@test.example", "Alice Tester"


@pytest.fixture()
def client(repo_with_user, monkeypatch: pytest.MonkeyPatch):
    repo_root, _, _ = repo_with_user
    token = "test-token-xyz"
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    monkeypatch.setenv("JOBSMITH_REPO_ROOT", str(repo_root))
    monkeypatch.chdir(repo_root)
    app = create_app()
    with TestClient(app) as c:
        yield c, token


def test_me_returns_401_without_token(client):
    c, _ = client
    resp = c.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_returns_user_with_valid_token(client, repo_with_user):
    c, token = client
    _, email, name = repo_with_user
    resp = c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == email
    assert body["name"] == name
    assert body["user_id"]
    assert body["created_at"]
