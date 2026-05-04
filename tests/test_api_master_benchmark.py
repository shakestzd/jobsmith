"""Tests for GET/PUT /api/master/benchmark endpoints.

TDD: these tests were written BEFORE the routes existed.  Run them to confirm
they fail, then implement the routes until they all pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.master import router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Create a minimal jobsmith repo root with a benchmark.md."""
    # Write a .apply-config.yaml so _require_config_path() finds it
    (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
    # Create the content directory with a seed benchmark.md
    content_dir = tmp_path / "assets" / "content"
    content_dir.mkdir(parents=True)
    (content_dir / "benchmark.md").write_text(
        "# Benchmark\n\nInitial content.\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def client(tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a TestClient rooted at *repo_root*."""
    monkeypatch.chdir(repo_root)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/master/benchmark
# ---------------------------------------------------------------------------


class TestGetBenchmark:
    def test_get_returns_200_with_text_and_version(self, client: TestClient) -> None:
        """GET /api/master/benchmark returns {text, version} with status 200."""
        resp = client.get("/api/master/benchmark")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "text" in data
        assert "version" in data

    def test_get_returns_initial_content(self, client: TestClient) -> None:
        """GET /api/master/benchmark returns the seeded benchmark text."""
        resp = client.get("/api/master/benchmark")
        assert resp.status_code == 200, resp.text
        assert "Initial content" in resp.json()["text"]

    def test_get_missing_file_returns_empty_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET returns {text: '', version: ''} when benchmark.md is absent."""
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        c = TestClient(app)
        resp = c.get("/api/master/benchmark")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["text"] == ""
        assert data["version"] == ""

    def test_get_404_when_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET returns 404 when no .apply-config.yaml is found."""
        # Use a path with no config file anywhere up the tree
        isolated = tmp_path / "no_config"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        c = TestClient(app)
        resp = c.get("/api/master/benchmark")
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# PUT /api/master/benchmark
# ---------------------------------------------------------------------------


class TestPutBenchmark:
    def test_put_returns_200_with_text_and_version(self, client: TestClient) -> None:
        """PUT /api/master/benchmark returns {text, version} with status 200."""
        resp = client.put(
            "/api/master/benchmark",
            json={"text": "# New Benchmark\n\nUpdated.\n"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "text" in data
        assert "version" in data

    def test_put_writes_content(self, client: TestClient, repo_root: Path) -> None:
        """PUT /api/master/benchmark persists the new text to disk."""
        new_text = "# Updated\n\nChanged content.\n"
        resp = client.put("/api/master/benchmark", json={"text": new_text})
        assert resp.status_code == 200, resp.text
        written = (repo_root / "assets" / "content" / "benchmark.md").read_text(encoding="utf-8")
        assert written == new_text

    def test_put_returns_written_text(self, client: TestClient) -> None:
        """PUT response body text matches what was sent."""
        new_text = "# Hello\n\nWorld.\n"
        resp = client.put("/api/master/benchmark", json={"text": new_text})
        assert resp.status_code == 200, resp.text
        assert resp.json()["text"] == new_text

    def test_version_changes_after_write(self, client: TestClient) -> None:
        """Version token differs before and after a PUT."""
        before = client.get("/api/master/benchmark").json()["version"]
        client.put("/api/master/benchmark", json={"text": "# Different\n\nContent.\n"})
        after = client.get("/api/master/benchmark").json()["version"]
        assert before != after, "version must change after a successful PUT"

    def test_put_missing_text_field_returns_422(self, client: TestClient) -> None:
        """PUT with a body that lacks 'text' returns 422 Unprocessable Entity."""
        resp = client.put("/api/master/benchmark", json={"wrong_key": "oops"})
        assert resp.status_code == 422, resp.text

    def test_put_404_when_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PUT returns 404 when no .apply-config.yaml is found."""
        isolated = tmp_path / "no_config"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        c = TestClient(app)
        resp = c.put("/api/master/benchmark", json={"text": "hello"})
        assert resp.status_code == 404, resp.text

    def test_put_creates_benchmark_if_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PUT creates benchmark.md when it does not yet exist."""
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        # Do NOT create assets/content/benchmark.md
        monkeypatch.chdir(tmp_path)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        c = TestClient(app)
        resp = c.put("/api/master/benchmark", json={"text": "# New\n"})
        assert resp.status_code == 200, resp.text
        created = (tmp_path / "assets" / "content" / "benchmark.md").read_text(encoding="utf-8")
        assert created == "# New\n"
