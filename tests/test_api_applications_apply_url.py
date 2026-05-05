"""Tests for apply_url exposure on GET /api/applications/{slug}.

feat-bb81c3ce: The detail endpoint must surface the `apply_url` field from
the jd-parsed artifact so the frontend can re-launch an apply run without
routing the user to the CLI.

Coverage:
- When the jd-parsed artifact for the run contains `apply_url`, that value
  is surfaced in GET /api/applications/{slug} under the `apply_url` key.
- When jd-parsed is absent (or lacks the field), `apply_url` is null.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.applications import router as applications_router
from jobsmith.db import open_pipeline_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(
    tmp_path: Path,
    *,
    apply_url: str | None = "https://example.com/jobs/123",
    include_jd_parsed: bool = True,
) -> tuple[Path, str, str]:
    """Create a pipeline DB with one run. Optionally inserts a jd-parsed artifact."""
    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)

    slug = "example-eng-2025"
    run_id = "run-applyurl-001"

    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, "render", "2025-06-01T09:00:00Z", "2025-06-01T09:10:00Z", "done"),
    )
    if include_jd_parsed:
        payload: dict = {"company": "Example Corp", "position": "Engineer"}
        if apply_url is not None:
            payload["apply_url"] = apply_url
        conn.execute(
            "INSERT INTO specialist_outputs "
            "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "apply-jd-parser",
                "jd-parsed",
                json.dumps(payload),
                None,
                "2025-06-01T09:01:00Z",
            ),
        )
    conn.commit()
    conn.close()
    return db_path, slug, run_id


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_path: Path) -> TestClient:
    monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
    app = FastAPI()
    app.include_router(applications_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestApplyUrlOnDetail:
    def test_apply_url_present_when_jd_parsed_has_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /api/applications/{slug} returns apply_url from jd-parsed artifact."""
        db_path, slug, _ = _make_db(
            tmp_path, apply_url="https://example.com/jobs/123"
        )
        client = _make_client(tmp_path, monkeypatch, db_path)

        resp = client.get(f"/api/applications/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "apply_url" in data, f"apply_url missing from response: {list(data.keys())}"
        assert data["apply_url"] == "https://example.com/jobs/123"

    def test_apply_url_null_when_jd_parsed_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /api/applications/{slug} returns apply_url=null when no jd-parsed artifact."""
        db_path, slug, _ = _make_db(tmp_path, include_jd_parsed=False)
        client = _make_client(tmp_path, monkeypatch, db_path)

        resp = client.get(f"/api/applications/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "apply_url" in data, f"apply_url missing from response: {list(data.keys())}"
        assert data["apply_url"] is None

    def test_apply_url_null_when_field_absent_in_jd_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /api/applications/{slug} returns null when jd-parsed exists but lacks apply_url."""
        db_path, slug, _ = _make_db(tmp_path, apply_url=None)
        client = _make_client(tmp_path, monkeypatch, db_path)

        resp = client.get(f"/api/applications/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "apply_url" in data
        assert data["apply_url"] is None

    def test_apply_url_not_exposed_on_list_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /api/applications (list) is out of scope — focus is on the detail endpoint."""
        db_path, slug, _ = _make_db(tmp_path, apply_url="https://example.com/jobs/123")
        client = _make_client(tmp_path, monkeypatch, db_path)

        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        # No assertion on apply_url here — list endpoint doesn't need it.
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["slug"] == slug
