"""Tests for the artifacts endpoints.

Coverage:
- GET /api/applications/{slug}/runs/{run_id}/artifacts        — list artifacts
- GET /api/applications/{slug}/runs/{run_id}/artifacts/{kind} — single artifact
- 404 when kind is missing
- 404 when run_id doesn't exist
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.artifacts import router as artifacts_router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_with_outputs(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a pipeline DB with one run + two specialist outputs. Returns (db_path, slug, run_id)."""
    from jobsmith.db import open_pipeline_db

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)

    slug = "acme-swe-2025"
    run_id = "run-abc123"

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
    conn.execute(
        "INSERT INTO specialist_outputs (run_id, specialist, kind, output_json, transcript_ref, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "apply-fit-scorer",
            "fit-score",
            json.dumps({"score": 0.85, "rationale": "Strong match"}),
            "/transcripts/fit-score.md",
            "2025-01-01T10:04:00Z",
        ),
    )
    conn.commit()
    conn.close()

    return db_path, slug, run_id


@pytest.fixture()
def client_with_db(
    db_with_outputs: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, str, str]:
    """TestClient wired to a real DB fixture. Returns (client, slug, run_id)."""
    db_path, slug, run_id = db_with_outputs

    # Patch the DB path resolver used by the artifacts router
    monkeypatch.setattr(
        "jobsmith.api.artifacts._get_db_path",
        lambda: db_path,
    )

    app = FastAPI()
    app.include_router(artifacts_router, prefix="/api")
    tc = TestClient(app, raise_server_exceptions=True)
    return tc, slug, run_id


# ---------------------------------------------------------------------------
# list artifacts
# ---------------------------------------------------------------------------


class TestListArtifacts:
    def test_returns_200_list(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/{run_id}/artifacts")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_envelope_fields_present(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/{run_id}/artifacts")
        assert resp.status_code == 200
        for item in resp.json():
            assert "run_id" in item
            assert "specialist" in item
            assert "kind" in item
            assert "output" in item
            assert "finished_at" in item
            assert "transcript_ref" in item

    def test_run_id_in_each_envelope(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/{run_id}/artifacts")
        assert resp.status_code == 200
        for item in resp.json():
            assert item["run_id"] == run_id

    def test_404_when_run_missing(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, _ = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/nonexistent-run/artifacts")
        assert resp.status_code == 404, resp.text

    def test_kinds_in_list(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/{run_id}/artifacts")
        assert resp.status_code == 200
        kinds = {item["kind"] for item in resp.json()}
        assert kinds == {"jd-parsed", "fit-score"}


# ---------------------------------------------------------------------------
# single artifact
# ---------------------------------------------------------------------------


class TestGetArtifact:
    def test_returns_200_for_known_kind(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/{run_id}/artifacts/jd-parsed")
        assert resp.status_code == 200, resp.text

    def test_output_is_dict(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/{run_id}/artifacts/fit-score")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["output"], dict)
        assert data["output"]["score"] == pytest.approx(0.85)

    def test_transcript_ref_present(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/{run_id}/artifacts/fit-score")
        assert resp.status_code == 200
        data = resp.json()
        assert data["transcript_ref"] == "/transcripts/fit-score.md"

    def test_transcript_ref_null_when_missing(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/{run_id}/artifacts/jd-parsed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["transcript_ref"] is None

    def test_404_for_unknown_kind(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/{run_id}/artifacts/prose-draft")
        assert resp.status_code == 404, resp.text

    def test_404_for_missing_run(
        self, client_with_db: tuple[TestClient, str, str]
    ) -> None:
        client, slug, _ = client_with_db
        resp = client.get(f"/api/applications/{slug}/runs/bad-run/artifacts/jd-parsed")
        assert resp.status_code == 404, resp.text
