"""S3: drop FS fallback from API read endpoints (feat-eb6c99cb).

Verifies that:
1. derive_application_state never reads from the filesystem.
2. JOBSMITH_FS_FALLBACK env var is no longer honored.
3. GET /api/master/{section} returns a structured 404 when the DB row is
   absent, with `error: 'missing_in_db'` and a `suggestion` hint.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.state import derive_application_state
from jobsmith.db import open_pipeline_db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "private" / "jobsmith.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    open_pipeline_db(db).close()
    return db


def _seed_run(db: Path, slug: str, status: str = "running") -> str:
    conn = open_pipeline_db(db)
    try:
        conn.execute(
            "INSERT INTO apply_runs (run_id, slug, phase, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"run-{slug}", slug, "draft", status, "2026-05-05T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    return f"run-{slug}"


class TestDeriveApplicationStateNoFS:
    def test_no_run_returns_phase_0_queued(self, db_path: Path):
        with patch("jobsmith.api.state._get_db_path", return_value=db_path):
            result = derive_application_state("missing-slug")
        assert result == {"slug": "missing-slug", "run_id": None, "phase": 0, "status": "queued"}

    def test_db_only_phase_when_kinds_present(self, db_path: Path):
        run_id = _seed_run(db_path, "acme")
        conn = open_pipeline_db(db_path)
        try:
            conn.execute(
                "INSERT INTO specialist_outputs "
                "(run_id, specialist, kind, output_json, finished_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, "draft-prose", "prose-draft", "{}", "2026-05-05T00:01:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

        with patch("jobsmith.api.state._get_db_path", return_value=db_path):
            result = derive_application_state("acme")
        assert result["phase"] == 2  # prose-draft

    def test_jobsmith_fs_fallback_env_var_is_ignored(self, db_path: Path, tmp_path: Path):
        """Even with JOBSMITH_FS_FALLBACK=1, no FS reads are attempted."""
        run_id = _seed_run(db_path, "acme")
        # Create a phase-3 marker on disk that the OLD code would have picked up.
        state_dir = tmp_path / "acme" / ".apply-state"
        state_dir.mkdir(parents=True)
        (state_dir.parent / "cover-letter-draft.md").write_text("on disk only")

        with patch.dict(os.environ, {"JOBSMITH_FS_FALLBACK": "1"}), patch(
            "jobsmith.api.state._get_db_path", return_value=db_path
        ):
            result = derive_application_state("acme")

        # Phase stays 0 (running, no kinds in DB) — the disk file is ignored.
        assert result["run_id"] == run_id
        assert result["phase"] == 0

    def test_no_fs_fallback_load_function(self):
        """The _fs_fallback_load helper is gone — module no longer references it."""
        from jobsmith.api import state as state_mod

        assert not hasattr(state_mod, "_fs_fallback_load")


class TestMasterReadStructured404:
    def _make_app(self, db_path: Path) -> FastAPI:
        from jobsmith.api.master import router

        app = FastAPI()
        app.include_router(router, prefix="/api")
        return app

    def test_get_master_section_returns_structured_404_when_missing(
        self, db_path: Path, tmp_path: Path
    ):
        # DB exists but has no master_content rows.
        client = TestClient(self._make_app(db_path))
        with patch("jobsmith.api.master._get_db_path_for_master", return_value=db_path):
            resp = client.get("/api/master/work")

        assert resp.status_code == 404
        body = resp.json()
        detail = body.get("detail", {})
        assert detail.get("error") == "missing_in_db"
        assert detail.get("section") == "work"
        assert "jobsmith db load-master" in detail.get("suggestion", "")

    def test_get_master_section_returns_db_row_when_present(
        self, db_path: Path
    ):
        conn = open_pipeline_db(db_path)
        try:
            conn.execute(
                "INSERT INTO master_content (section, content_blob, etag, loaded_at) "
                "VALUES ('work', '- title: Engineer\\n  start: 2024-01\\n', 'abc123', '2026-01-01T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()

        client = TestClient(self._make_app(db_path))
        with patch("jobsmith.api.master._get_db_path_for_master", return_value=db_path):
            resp = client.get("/api/master/work")

        assert resp.status_code == 200
        assert resp.headers.get("ETag", "").startswith('"')

    def test_get_master_aggregate_returns_404_when_any_section_missing(
        self, db_path: Path
    ):
        # DB has only 'work', not all 4.
        conn = open_pipeline_db(db_path)
        try:
            conn.execute(
                "INSERT INTO master_content (section, content_blob, etag, loaded_at) "
                "VALUES ('work', '[]', 'aaa', '2026-01-01T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()

        client = TestClient(self._make_app(db_path))
        with patch("jobsmith.api.master._get_db_path_for_master", return_value=db_path):
            resp = client.get("/api/master")

        assert resp.status_code == 404
        body = resp.json()
        assert body["detail"]["error"] == "missing_in_db"
