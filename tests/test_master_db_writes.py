"""S5: master writes to DB first; YAML becomes derived (feat-484c52b5).

Verifies:
1. PUT /api/master/{section} writes to master_content DB table only.
2. The YAML file on disk is NOT touched by PUT.
3. `jobsmith master export` regenerates YAML files from DB.
4. ETag/If-Match still works against DB-derived etags.
5. save_master_to_blob preserves comments via ruamel round-trip.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobsmith.db import open_pipeline_db
from jobsmith.master_io import save_master_to_blob

WORK_PAYLOAD = [
    {
        "title": "Senior Engineer",
        "location": "Acme Corp",
        "date": "Jan 2023 - Present",
        "description": "Remote",
        "details": ["Shipped 7 ETL pipelines"],
    }
]


# ---------------------------------------------------------------------------
# save_master_to_blob (pure function)
# ---------------------------------------------------------------------------


class TestSaveMasterToBlob:
    def test_writes_fresh_blob_when_no_existing(self):
        blob = save_master_to_blob("work", WORK_PAYLOAD, existing_blob=None)
        assert "Senior Engineer" in blob
        assert "Acme Corp" in blob

    def test_validation_error_raised_on_bad_payload(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            save_master_to_blob("work", "not-a-list", existing_blob=None)

    def test_preserves_comments_when_existing_blob_supplied(self):
        existing = (
            "# top comment — preserved\n"
            "- title: Old Engineer\n"
            "  location: Old Corp\n"
            "  date: 2020-01\n"
            "  description: Hybrid\n"
            "  details:\n"
            "    - Old bullet\n"
        )
        new_blob = save_master_to_blob("work", WORK_PAYLOAD, existing_blob=existing)
        assert "# top comment — preserved" in new_blob


# ---------------------------------------------------------------------------
# PUT /api/master/{section} writes to DB only
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
    content_dir = tmp_path / "assets" / "content"
    content_dir.mkdir(parents=True)
    (content_dir / "work.yml").write_text(
        "- title: Original\n  location: Orig Co\n  date: \"2020\"\n  description: x\n  details: [a]\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def db_path(repo_root: Path) -> Path:
    db = repo_root / "private" / "jobsmith.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    open_pipeline_db(db).close()
    return db


@pytest.fixture()
def client(db_path: Path, repo_root: Path) -> TestClient:
    from jobsmith.api.master import router
    from jobsmith.master_ingest import ingest_master_from_disk

    conn = open_pipeline_db(db_path)
    try:
        ingest_master_from_disk(
            conn, content_dir=repo_root / "assets" / "content", reload=True
        )
    finally:
        conn.close()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    c = TestClient(app)
    return c


class TestPutSectionWritesDbOnly:
    def test_put_writes_to_master_content_table(
        self, client: TestClient, db_path: Path, repo_root: Path
    ):
        with patch(
            "jobsmith.api.master._get_db_path_for_master", return_value=db_path
        ), patch("jobsmith.api.master.find_config", return_value=repo_root / ".apply-config.yaml"):
            resp = client.put("/api/master/work", json=WORK_PAYLOAD)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["section"] == "work"
        assert body["path"] == "db:master_content:work"

        conn = open_pipeline_db(db_path)
        try:
            row = conn.execute(
                "SELECT content_blob FROM master_content WHERE section = 'work'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert "Senior Engineer" in row["content_blob"]

    def test_put_does_not_modify_yaml_file(
        self, client: TestClient, db_path: Path, repo_root: Path
    ):
        target = repo_root / "assets" / "content" / "work.yml"
        before = target.read_text(encoding="utf-8")

        with patch(
            "jobsmith.api.master._get_db_path_for_master", return_value=db_path
        ), patch("jobsmith.api.master.find_config", return_value=repo_root / ".apply-config.yaml"):
            client.put("/api/master/work", json=WORK_PAYLOAD)

        after = target.read_text(encoding="utf-8")
        assert before == after, "PUT must NOT touch the YAML file (S5 contract)"

    def test_put_if_match_uses_db_etag(
        self, client: TestClient, db_path: Path, repo_root: Path
    ):
        with patch(
            "jobsmith.api.master._get_db_path_for_master", return_value=db_path
        ), patch("jobsmith.api.master.find_config", return_value=repo_root / ".apply-config.yaml"):
            etag = client.get("/api/master/work").headers["etag"]
            resp = client.put(
                "/api/master/work",
                json=WORK_PAYLOAD,
                headers={"If-Match": etag},
            )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# `jobsmith master export` CLI
# ---------------------------------------------------------------------------


class TestMasterExportCli:
    def test_export_all_writes_files_from_db(
        self, db_path: Path, repo_root: Path, client: TestClient
    ):
        # client fixture seeds DB. Now mutate work via PUT then export.
        with patch(
            "jobsmith.api.master._get_db_path_for_master", return_value=db_path
        ), patch("jobsmith.api.master.find_config", return_value=repo_root / ".apply-config.yaml"):
            client.put("/api/master/work", json=WORK_PAYLOAD)

        from jobsmith.cli import app as cli_app

        runner = CliRunner()
        with patch(
            "jobsmith.cli.find_config", return_value=repo_root / ".apply-config.yaml"
        ), patch("jobsmith.cli.repo_root_for", return_value=repo_root):
            result = runner.invoke(cli_app, ["master", "export", "--all"])

        assert result.exit_code == 0, result.output
        target = repo_root / "assets" / "content" / "work.yml"
        assert "Senior Engineer" in target.read_text(encoding="utf-8")

    def test_export_section_writes_single_file(
        self, db_path: Path, repo_root: Path, client: TestClient
    ):
        from jobsmith.cli import app as cli_app

        runner = CliRunner()
        with patch(
            "jobsmith.cli.find_config", return_value=repo_root / ".apply-config.yaml"
        ), patch("jobsmith.cli.repo_root_for", return_value=repo_root):
            result = runner.invoke(cli_app, ["master", "export", "--section", "work"])

        assert result.exit_code == 0, result.output
        assert "exported" in result.output
        assert "work" in result.output

    def test_export_skips_missing_db_rows(
        self, db_path: Path, repo_root: Path
    ):
        # Empty DB, no master_content rows
        from jobsmith.cli import app as cli_app

        runner = CliRunner()
        with patch(
            "jobsmith.cli.find_config", return_value=repo_root / ".apply-config.yaml"
        ), patch("jobsmith.cli.repo_root_for", return_value=repo_root):
            result = runner.invoke(cli_app, ["master", "export", "--all"])

        assert result.exit_code == 0
        assert "skip" in result.output
