"""Tests for master YAML → DB ingest (feat-bf06bdea, S1).

Coverage
--------
Unit:
  - ingest_master_from_disk() loads YAML files and writes rows with correct etag
  - ingest_master_from_disk() skips existing sections without reload
  - ingest_master_from_disk() replaces existing rows with reload=True
  - etag is sha256(content_blob)[:16]

Integration:
  - ensure_master_loaded() on empty DB populates the table and logs "Loaded N"
  - GET /api/master/work returns data from the DB row

CLI:
  - `jobsmith db load-master` loads from disk into DB
  - `jobsmith db load-master --reload` replaces existing rows
  - without --reload, existing sections are not overwritten
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.db import open_pipeline_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_WORK = Path(__file__).parent / "fixtures" / "master_work.yml"


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Minimal jobsmith repo with master YAML files seeded."""
    config_path = tmp_path / ".apply-config.yaml"
    config_path.write_text(
        "master:\n"
        "  work_yml: assets/content/work.yml\n"
        "  skill_yml: assets/content/skill.yml\n"
        "  education_yml: assets/content/education.yml\n"
        "  author_yml: assets/content/author.yml\n"
        "output:\n"
        "  jobsmith_db: private/jobsmith.db\n",
        encoding="utf-8",
    )
    content_dir = tmp_path / "assets" / "content"
    content_dir.mkdir(parents=True)

    shutil.copy(FIXTURE_WORK, content_dir / "work.yml")

    (content_dir / "skill.yml").write_text(
        "- title: Python\n  description: Advanced\n  details:\n    - Python 3.x\n",
        encoding="utf-8",
    )
    (content_dir / "education.yml").write_text(
        "- title: B.Sc. Computer Science\n  location: State University\n"
        "  date: 2020\n  description: GPA 3.9\n",
        encoding="utf-8",
    )
    (content_dir / "author.yml").write_text(
        "author:\n  - name: Pat Doe\n    email: pat@example.com\n",
        encoding="utf-8",
    )

    (tmp_path / "private").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def db_conn(repo_root: Path):
    """Open pipeline DB with schema applied (includes master_content table)."""
    db_path = repo_root / "private" / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    yield conn, db_path
    conn.close()


# ---------------------------------------------------------------------------
# Unit tests — ingest_master_from_disk
# ---------------------------------------------------------------------------


class TestIngestMasterFromDisk:
    def test_loads_yaml_writes_rows(self, db_conn, repo_root):
        """ingest_master_from_disk writes one row per section with correct etag."""
        from jobsmith.master_ingest import ingest_master_from_disk

        conn, db_path = db_conn
        content_dir = repo_root / "assets" / "content"
        n = ingest_master_from_disk(conn, content_dir=content_dir)

        assert n >= 1

        row = conn.execute(
            "SELECT * FROM master_content WHERE section = 'work'"
        ).fetchone()
        assert row is not None
        assert row["section"] == "work"
        assert len(row["content_blob"]) > 0
        assert row["loaded_at"] is not None

        expected_etag = hashlib.sha256(row["content_blob"].encode("utf-8")).hexdigest()[:16]
        assert row["etag"] == expected_etag

    def test_skips_existing_sections_without_reload(self, db_conn, repo_root):
        """Without reload=True, existing rows are not replaced."""
        from jobsmith.master_ingest import ingest_master_from_disk

        conn, _ = db_conn
        content_dir = repo_root / "assets" / "content"

        # First load
        ingest_master_from_disk(conn, content_dir=content_dir)

        # Corrupt the row
        conn.execute(
            "UPDATE master_content SET content_blob = 'SENTINEL' WHERE section = 'work'"
        )
        conn.commit()

        # Second load — must NOT overwrite
        n2 = ingest_master_from_disk(conn, content_dir=content_dir)

        row = conn.execute(
            "SELECT content_blob FROM master_content WHERE section = 'work'"
        ).fetchone()
        assert row["content_blob"] == "SENTINEL"
        assert n2 == 0

    def test_reload_replaces_existing_rows(self, db_conn, repo_root):
        """With reload=True, existing rows are replaced from disk."""
        from jobsmith.master_ingest import ingest_master_from_disk

        conn, _ = db_conn
        content_dir = repo_root / "assets" / "content"

        ingest_master_from_disk(conn, content_dir=content_dir)

        conn.execute(
            "UPDATE master_content SET content_blob = 'SENTINEL' WHERE section = 'work'"
        )
        conn.commit()

        n = ingest_master_from_disk(conn, content_dir=content_dir, reload=True)

        row = conn.execute(
            "SELECT content_blob FROM master_content WHERE section = 'work'"
        ).fetchone()
        assert row["content_blob"] != "SENTINEL"
        assert n >= 1

    def test_etag_is_sha256_first_16(self, db_conn, repo_root):
        """etag == sha256(content_blob.encode('utf-8')).hexdigest()[:16]."""
        from jobsmith.master_ingest import ingest_master_from_disk

        conn, _ = db_conn
        content_dir = repo_root / "assets" / "content"
        ingest_master_from_disk(conn, content_dir=content_dir)

        rows = conn.execute(
            "SELECT section, content_blob, etag FROM master_content"
        ).fetchall()
        assert len(rows) > 0
        for row in rows:
            expected = hashlib.sha256(row["content_blob"].encode("utf-8")).hexdigest()[:16]
            assert row["etag"] == expected, (
                f"etag mismatch for section={row['section']}: "
                f"got {row['etag']!r}, expected {expected!r}"
            )

    def test_returns_count_of_loaded_sections(self, db_conn, repo_root):
        """Return value equals the number of sections actually written."""
        from jobsmith.master_ingest import ingest_master_from_disk

        conn, _ = db_conn
        content_dir = repo_root / "assets" / "content"
        n = ingest_master_from_disk(conn, content_dir=content_dir)
        # work + skill + education + author = 4
        assert n == 4

    def test_missing_file_is_skipped(self, db_conn, repo_root):
        """A missing YAML file is skipped gracefully."""
        from jobsmith.master_ingest import ingest_master_from_disk

        conn, _ = db_conn
        content_dir = repo_root / "assets" / "content"
        (content_dir / "education.yml").unlink()

        n = ingest_master_from_disk(conn, content_dir=content_dir)
        assert n == 3  # work + skill + author

        row = conn.execute(
            "SELECT section FROM master_content WHERE section = 'education'"
        ).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# Integration — ensure_master_loaded (startup hook)
# ---------------------------------------------------------------------------


class TestEnsureMasterLoaded:
    def test_empty_db_populates_table(self, repo_root, caplog):
        """ensure_master_loaded() on an empty DB loads sections and logs 'Loaded N'."""
        import logging

        from jobsmith.master_ingest import ensure_master_loaded

        db_path = repo_root / "private" / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        conn.close()

        with caplog.at_level(logging.INFO, logger="jobsmith.master_ingest"):
            ensure_master_loaded(db_path, repo_root=repo_root)

        assert "Loaded" in caplog.text

        conn = open_pipeline_db(db_path)
        try:
            row = conn.execute(
                "SELECT section FROM master_content WHERE section = 'work'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_already_populated_skips_load(self, repo_root, caplog):
        """ensure_master_loaded() is a no-op when master_content already has rows."""
        import logging

        from jobsmith.master_ingest import ensure_master_loaded

        db_path = repo_root / "private" / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        conn.execute(
            "INSERT INTO master_content (section, content_blob, etag, loaded_at) "
            "VALUES ('work', 'EXISTING', 'abc123ab', '2020-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        with caplog.at_level(logging.INFO, logger="jobsmith.master_ingest"):
            ensure_master_loaded(db_path, repo_root=repo_root)

        # Row should be unchanged (no reload triggered)
        conn = open_pipeline_db(db_path)
        try:
            row = conn.execute(
                "SELECT content_blob FROM master_content WHERE section = 'work'"
            ).fetchone()
            assert row["content_blob"] == "EXISTING"
        finally:
            conn.close()

    def test_reload_master_flag_replaces_rows(self, repo_root, caplog):
        """ensure_master_loaded(reload=True) replaces existing rows."""
        import logging

        from jobsmith.master_ingest import ensure_master_loaded

        db_path = repo_root / "private" / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        conn.execute(
            "INSERT INTO master_content (section, content_blob, etag, loaded_at) "
            "VALUES ('work', 'SENTINEL', 'abc123ab', '2020-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        with caplog.at_level(logging.INFO, logger="jobsmith.master_ingest"):
            ensure_master_loaded(db_path, repo_root=repo_root, reload=True)

        conn = open_pipeline_db(db_path)
        try:
            row = conn.execute(
                "SELECT content_blob FROM master_content WHERE section = 'work'"
            ).fetchone()
            assert row["content_blob"] != "SENTINEL"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Integration — GET /api/master/{section} queries DB first
# ---------------------------------------------------------------------------


class TestMasterApiDbRead:
    def _make_app(self):
        from jobsmith.api.master import router

        app = FastAPI()
        app.include_router(router, prefix="/api")
        return app

    def test_get_work_returns_db_row(self, repo_root):
        """GET /api/master/work returns data from DB (DB-first path)."""
        from jobsmith.master_ingest import ingest_master_from_disk

        db_path = repo_root / "private" / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        try:
            ingest_master_from_disk(
                conn, content_dir=repo_root / "assets" / "content"
            )
        finally:
            conn.close()

        app = self._make_app()
        client = TestClient(app)

        with patch(
            "jobsmith.api.master.find_config",
            return_value=repo_root / ".apply-config.yaml",
        ), patch(
            "jobsmith.api.master._get_db_path_for_master",
            return_value=db_path,
        ):
            resp = client.get("/api/master/work")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        # The fixture has two positions
        titles = [entry["title"] for entry in data]
        assert "Senior Data Engineer" in titles

    def test_get_section_returns_404_when_db_empty(self, repo_root):
        """GET /api/master/work returns structured 404 when DB has no row (S3, feat-eb6c99cb)."""
        db_path = repo_root / "private" / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        conn.close()

        app = self._make_app()
        client = TestClient(app)

        with patch(
            "jobsmith.api.master.find_config",
            return_value=repo_root / ".apply-config.yaml",
        ), patch(
            "jobsmith.api.master._get_db_path_for_master",
            return_value=db_path,
        ):
            resp = client.get("/api/master/work")

        assert resp.status_code == 404
        body = resp.json()
        assert body["detail"]["error"] == "missing_in_db"
        assert "jobsmith db load-master" in body["detail"]["suggestion"]


# ---------------------------------------------------------------------------
# CLI — jobsmith db load-master
# ---------------------------------------------------------------------------


class TestCliDbLoadMaster:
    def _invoke(self, runner, args, *, repo_root):
        from jobsmith.cli import app

        with patch("jobsmith.cli.find_config", return_value=repo_root / ".apply-config.yaml"), \
             patch("jobsmith.cli.repo_root_for", return_value=repo_root):
            return runner.invoke(app, ["db", "load-master"] + args)

    def test_load_master_populates_db(self, repo_root):
        """load-master without flags loads missing sections into DB."""
        from typer.testing import CliRunner

        runner = CliRunner()
        db_path = repo_root / "private" / "jobsmith.db"
        open_pipeline_db(db_path).close()

        result = self._invoke(runner, [], repo_root=repo_root)

        assert result.exit_code == 0, result.output
        assert "Loaded" in result.output

        conn = open_pipeline_db(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM master_content"
            ).fetchone()[0]
            assert count >= 1
        finally:
            conn.close()

    def test_load_master_reload_replaces_rows(self, repo_root):
        """load-master --reload replaces existing rows."""
        from typer.testing import CliRunner

        runner = CliRunner()
        db_path = repo_root / "private" / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        conn.execute(
            "INSERT INTO master_content (section, content_blob, etag, loaded_at) "
            "VALUES ('work', 'SENTINEL', 'abc123ab', '2020-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        result = self._invoke(runner, ["--reload"], repo_root=repo_root)

        assert result.exit_code == 0, result.output

        conn = open_pipeline_db(db_path)
        try:
            row = conn.execute(
                "SELECT content_blob FROM master_content WHERE section = 'work'"
            ).fetchone()
            assert row is not None
            assert row["content_blob"] != "SENTINEL"
        finally:
            conn.close()

    def test_load_master_no_reload_skips_existing(self, repo_root):
        """load-master without --reload does not overwrite existing sections."""
        from typer.testing import CliRunner

        runner = CliRunner()
        db_path = repo_root / "private" / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        conn.execute(
            "INSERT INTO master_content (section, content_blob, etag, loaded_at) "
            "VALUES ('work', 'SENTINEL', 'abc123ab', '2020-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        result = self._invoke(runner, [], repo_root=repo_root)

        assert result.exit_code == 0, result.output

        conn = open_pipeline_db(db_path)
        try:
            row = conn.execute(
                "SELECT content_blob FROM master_content WHERE section = 'work'"
            ).fetchone()
            assert row["content_blob"] == "SENTINEL"
        finally:
            conn.close()
