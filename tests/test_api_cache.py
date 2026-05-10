"""API tests for the LLM cache surfaces (feat-ff4ccde2)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.main import create_app
from jobsmith.db import open_pipeline_db
from jobsmith.llm.sqlite_cache import put_cached_phase


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    cfg = {
        "user": {"name": "Cache Tester", "email": "cache@test.example"},
        "output": {"jobsmith_db": "private/jobsmith.db"},
    }
    (tmp_path / ".apply-config.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "private").mkdir()
    token = "static-token-123"
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    monkeypatch.setenv("JOBSMITH_REPO_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path, token


@pytest.fixture()
def client(repo):
    app = create_app()
    with TestClient(app) as c:
        yield c


def _bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def test_doctor_llm_cache_empty(client, repo):
    _, token = repo
    resp = client.get("/api/doctor/llm-cache", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"total_entries": 0, "total_hits": 0}


def test_doctor_llm_cache_after_put(client, repo):
    repo_root, token = repo
    db_path = repo_root / "private" / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    try:
        put_cached_phase(conn, {"a": {"v": 1}}, "jdh", "metag", "claude")
    finally:
        conn.close()
    resp = client.get("/api/doctor/llm-cache", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json() == {"total_entries": 1, "total_hits": 0}


def test_invalidate_cache_drops_rows(client, repo):
    repo_root, token = repo
    db_path = repo_root / "private" / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    try:
        put_cached_phase(conn, {"a": {}, "b": {}}, "jdh", "metag", "m")
    finally:
        conn.close()
    resp = client.post("/api/cache/invalidate", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}
    # Subsequent stats endpoint should be empty.
    stats = client.get("/api/doctor/llm-cache", headers=_bearer(token))
    assert stats.json() == {"total_entries": 0, "total_hits": 0}


def test_invalidate_requires_auth(client):
    resp = client.post("/api/cache/invalidate")
    assert resp.status_code == 401
