"""Tests for JobsmithClient SDK.

Coverage:
- Each read method parses API response into the correct Pydantic type
- 401 raises AuthError (missing / wrong token)
- 404 raises NotFoundError (e.g. get_artifact for missing kind)
- Auto-detection: env var > file > raise
- Base URL env override
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.client import (
    AuthError,
    JobsmithClient,
    NotFoundError,
)
from jobsmith.api.main import create_app
from jobsmith.api.schemas.master import Author, EducationEntry, SkillEntry, WorkEntry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


TOKEN = "test-sdk-token-xyz"
BASE_URL = "http://testserver"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Pipeline DB with one run + two outputs."""
    from jobsmith.db import open_pipeline_db

    db = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db)
    slug = "acme-swe"
    run_id = "run-sdk-001"
    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, "gather", "2025-01-01T10:00:00Z", "2025-01-01T10:05:00Z", "done"),
    )
    conn.execute(
        "INSERT INTO specialist_outputs (run_id, specialist, kind, output_json, transcript_ref, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "apply-jd-parser",
            "jd-parsed",
            json.dumps({"company": "Acme", "position": "SWE"}),
            None,
            "2025-01-01T10:02:00Z",
        ),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def full_app(tmp_path: Path, db_path: Path):
    """Full FastAPI app (create_app) with token env set + DB path patched.

    Both artifacts._get_db_path and applications._get_db_path must be patched
    because applications.py imports the helper by reference at module load.
    """
    with (
        patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}),
        patch("jobsmith.api.artifacts._get_db_path", return_value=db_path),
        patch("jobsmith.api.applications._get_db_path", return_value=db_path),
    ):
        yield create_app()


@pytest.fixture()
def sdk_client(full_app: FastAPI) -> JobsmithClient:
    """JobsmithClient backed by FastAPI's TestClient (sync ASGI httpx wrapper)."""
    test_client = TestClient(full_app, base_url=BASE_URL)
    return JobsmithClient(base_url=BASE_URL, token=TOKEN, http_client=test_client)


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


class TestHealth:
    def test_returns_status_ok(self, sdk_client: JobsmithClient) -> None:
        result = sdk_client.health()
        assert result.status == "ok"


# ---------------------------------------------------------------------------
# master read methods
# ---------------------------------------------------------------------------


class TestMasterReads:
    def test_get_master_work_returns_list(
        self, sdk_client: JobsmithClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        content_dir = tmp_path / "assets" / "content"
        content_dir.mkdir(parents=True)
        (content_dir / "work.yml").write_text(
            "- title: Engineer\n  location: Acme\n  date: '2023'\n  description: Remote\n  details: []\n"
        )
        result = sdk_client.get_master_work()
        assert isinstance(result, list)
        assert all(isinstance(e, WorkEntry) for e in result)

    def test_get_master_skill_returns_list(
        self, sdk_client: JobsmithClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        content_dir = tmp_path / "assets" / "content"
        content_dir.mkdir(parents=True)
        (content_dir / "skill.yml").write_text(
            "- title: Languages\n  description: Python, Go\n  details: [Python, Go]\n"
        )
        result = sdk_client.get_master_skill()
        assert isinstance(result, list)
        assert all(isinstance(e, SkillEntry) for e in result)

    def test_get_master_education_returns_list(
        self, sdk_client: JobsmithClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        content_dir = tmp_path / "assets" / "content"
        content_dir.mkdir(parents=True)
        (content_dir / "education.yml").write_text(
            "- title: State U\n  location: NY\n  date: '2018'\n  description: B.Sc CS\n  details: []\n"
        )
        result = sdk_client.get_master_education()
        assert isinstance(result, list)
        assert all(isinstance(e, EducationEntry) for e in result)

    def test_get_master_author_returns_author_or_none(
        self, sdk_client: JobsmithClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        content_dir = tmp_path / "assets" / "content"
        content_dir.mkdir(parents=True)
        (content_dir / "author.yml").write_text(
            "author:\n  - name: Jane Doe\n    email: jane@example.com\n"
        )
        result = sdk_client.get_master_author()
        assert result is None or isinstance(result, Author)


# ---------------------------------------------------------------------------
# artifact methods
# ---------------------------------------------------------------------------


class TestArtifactMethods:
    def test_list_artifacts_returns_list(self, sdk_client: JobsmithClient) -> None:
        from jobsmith.api.client import ArtifactEnvelope
        result = sdk_client.list_artifacts("acme-swe", "run-sdk-001")
        assert isinstance(result, list)
        assert all(isinstance(a, ArtifactEnvelope) for a in result)

    def test_list_artifacts_contains_jd_parsed(self, sdk_client: JobsmithClient) -> None:
        result = sdk_client.list_artifacts("acme-swe", "run-sdk-001")
        kinds = {a.kind for a in result}
        assert "jd-parsed" in kinds

    def test_get_artifact_returns_envelope(self, sdk_client: JobsmithClient) -> None:
        from jobsmith.api.client import ArtifactEnvelope
        result = sdk_client.get_artifact("acme-swe", "run-sdk-001", "jd-parsed")
        assert isinstance(result, ArtifactEnvelope)
        assert result.kind == "jd-parsed"

    def test_get_artifact_output_is_dict(self, sdk_client: JobsmithClient) -> None:
        result = sdk_client.get_artifact("acme-swe", "run-sdk-001", "jd-parsed")
        assert isinstance(result.output, dict)

    def test_get_artifact_404_for_missing_kind(self, sdk_client: JobsmithClient) -> None:
        with pytest.raises(NotFoundError):
            sdk_client.get_artifact("acme-swe", "run-sdk-001", "prose-draft")


# ---------------------------------------------------------------------------
# application methods
# ---------------------------------------------------------------------------


class TestApplicationMethods:
    def test_list_applications_returns_list(
        self, sdk_client: JobsmithClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        from jobsmith.api.client import Application
        result = sdk_client.list_applications()
        assert isinstance(result, list)
        assert all(isinstance(a, Application) for a in result)

    def test_get_application_returns_detail(
        self, sdk_client: JobsmithClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        from jobsmith.api.client import ApplicationDetail
        result = sdk_client.get_application("acme-swe")
        assert isinstance(result, ApplicationDetail)
        assert result.slug == "acme-swe"

    def test_get_application_404_for_missing_slug(
        self, sdk_client: JobsmithClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
        with pytest.raises(NotFoundError):
            sdk_client.get_application("nonexistent-slug-xyz")


# ---------------------------------------------------------------------------
# Auth error cases
# ---------------------------------------------------------------------------


class TestAuthErrors:
    def test_wrong_token_raises_auth_error(self, full_app: FastAPI) -> None:
        test_client = TestClient(full_app, base_url=BASE_URL)
        bad_client = JobsmithClient(
            base_url=BASE_URL, token="wrong-token", http_client=test_client
        )
        with pytest.raises(AuthError):
            bad_client.get_master_work()

    def test_no_token_env_no_file_raises_auth_error(self, tmp_path: Path) -> None:
        """When no token is resolvable, JobsmithClient constructor raises AuthError."""
        token_file = tmp_path / "no_such_token"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("jobsmith.api.client._TOKEN_FILE", token_file),
            pytest.raises(AuthError),
        ):
            JobsmithClient(base_url=BASE_URL)


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------


class TestAutoDetection:
    def test_token_from_env(self, full_app: FastAPI) -> None:
        test_client = TestClient(full_app, base_url=BASE_URL)
        with patch.dict(os.environ, {"JOBSMITH_API_TOKEN": TOKEN}):
            client = JobsmithClient(base_url=BASE_URL, http_client=test_client)
            result = client.health()
            assert result.status == "ok"

    def test_token_from_file(self, full_app: FastAPI, tmp_path: Path) -> None:
        test_client = TestClient(full_app, base_url=BASE_URL)
        token_file = tmp_path / "token"
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)
        env = {k: v for k, v in os.environ.items() if k != "JOBSMITH_API_TOKEN"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("jobsmith.api.client._TOKEN_FILE", token_file),
        ):
            client = JobsmithClient(base_url=BASE_URL, http_client=test_client)
            result = client.health()
            assert result.status == "ok"

    def test_base_url_from_env(self, full_app: FastAPI) -> None:
        test_client = TestClient(full_app, base_url=BASE_URL)
        with patch.dict(
            os.environ,
            {"JOBSMITH_API_TOKEN": TOKEN, "JOBSMITH_API_BASE_URL": BASE_URL},
        ):
            client = JobsmithClient(http_client=test_client)
            result = client.health()
            assert result.status == "ok"
