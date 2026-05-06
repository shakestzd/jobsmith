"""Tests for GET/PUT /api/master/benchmark endpoints.

DB-as-source-of-truth (bug-96d070f7): PUT writes to ``master_content``
(section='benchmark') and never touches the file. GET reads DB first,
falls back to disk when the DB has no row. Disk is regenerated only via
``jobsmith master export``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.master import router
from jobsmith.db import open_pipeline_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_db_for(repo_root: Path) -> Path:
    """Create the pipeline DB at the canonical location and return its path."""
    db_dir = repo_root / "private"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "jobsmith.db"
    open_pipeline_db(db_path).close()
    return db_path


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Create a minimal jobsmith repo root with benchmark.md and a DB."""
    # Write a .apply-config.yaml so _require_config_path() finds it
    (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
    # Create the content directory with a seed benchmark.md
    content_dir = tmp_path / "assets" / "content"
    content_dir.mkdir(parents=True)
    (content_dir / "benchmark.md").write_text(
        "# Benchmark\n\nInitial content.\n", encoding="utf-8"
    )
    _seed_db_for(tmp_path)
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

    def test_put_writes_to_db_not_disk(self, client: TestClient, repo_root: Path) -> None:
        """PUT persists to ``master_content`` and leaves benchmark.md untouched (S5).

        Regression test for bug-96d070f7: prior to the fix the PUT handler
        called ``save_benchmark()`` which wrote the file directly.
        """
        bench_path = repo_root / "assets" / "content" / "benchmark.md"
        original_disk = bench_path.read_text(encoding="utf-8")
        new_text = "# Updated\n\nChanged content.\n"
        resp = client.put("/api/master/benchmark", json={"text": new_text})
        assert resp.status_code == 200, resp.text
        # File on disk MUST be unchanged.
        assert bench_path.read_text(encoding="utf-8") == original_disk
        # DB row MUST contain the new text.
        db = open_pipeline_db(repo_root / "private" / "jobsmith.db")
        try:
            row = db.execute(
                "SELECT content_blob FROM master_content WHERE section = ?",
                ("benchmark",),
            ).fetchone()
        finally:
            db.close()
        assert row is not None, "DB row missing for section='benchmark'"
        assert row["content_blob"] == new_text

    def test_get_returns_db_after_put(self, client: TestClient) -> None:
        """After PUT, GET returns the new text — proves DB read is wired."""
        new_text = "# Mid-test\n\nFresh.\n"
        client.put("/api/master/benchmark", json={"text": new_text})
        resp = client.get("/api/master/benchmark")
        assert resp.json()["text"] == new_text

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

    def test_put_persists_when_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PUT succeeds with DB-only persistence even when benchmark.md is absent."""
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        # Do NOT create assets/content/benchmark.md
        _seed_db_for(tmp_path)
        monkeypatch.chdir(tmp_path)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        c = TestClient(app)
        resp = c.put("/api/master/benchmark", json={"text": "# New\n"})
        assert resp.status_code == 200, resp.text
        # File on disk should NOT have been created.
        assert not (tmp_path / "assets" / "content" / "benchmark.md").exists()
        # DB row should reflect the new text.
        db = open_pipeline_db(tmp_path / "private" / "jobsmith.db")
        try:
            row = db.execute(
                "SELECT content_blob FROM master_content WHERE section = ?",
                ("benchmark",),
            ).fetchone()
        finally:
            db.close()
        assert row is not None
        assert row["content_blob"] == "# New\n"
