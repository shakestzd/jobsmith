"""Tests for GET /api/applications and GET /api/applications/{slug} endpoints.

Coverage:
- role + company fields are extracted from the jd-parsed artifact and
  surfaced in both the list and detail responses.
- ui_phase derived field follows the documented taxonomy mapping raw DB
  (phase, status) pairs to UI-facing states: running, review, rendered, failed.
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
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, *, with_jd_parsed: bool = True) -> tuple[Path, str, str]:
    """Create pipeline DB with one run. Optionally includes a jd-parsed artifact."""
    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)

    slug = "acme-swe-2025"
    run_id = "run-abc123"

    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, "gather", "2025-01-01T10:00:00Z", "2025-01-01T10:05:00Z", "done"),
    )
    if with_jd_parsed:
        conn.execute(
            "INSERT INTO specialist_outputs "
            "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "apply-jd-parser",
                "jd-parsed",
                json.dumps({"company": "Acme Corp", "position": "Senior SWE"}),
                None,
                "2025-01-01T10:02:00Z",
            ),
        )
    conn.commit()
    conn.close()

    return db_path, slug, run_id


@pytest.fixture()
def client_with_jd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str, str]:
    """TestClient wired to a DB that has a jd-parsed artifact."""
    db_path, slug, run_id = _make_db(tmp_path, with_jd_parsed=True)
    monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
    app = FastAPI()
    app.include_router(applications_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=True), slug, run_id


@pytest.fixture()
def client_no_jd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str, str]:
    """TestClient wired to a DB that has NO jd-parsed artifact."""
    db_path, slug, run_id = _make_db(tmp_path, with_jd_parsed=False)
    monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
    app = FastAPI()
    app.include_router(applications_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=True), slug, run_id


# ---------------------------------------------------------------------------
# List endpoint — role + company
# ---------------------------------------------------------------------------


class TestListApplicationsRoleCompany:
    def test_role_and_company_present_in_list(
        self, client_with_jd: tuple[TestClient, str, str]
    ) -> None:
        """GET /api/applications includes role + company from jd-parsed artifact."""
        client, slug, _ = client_with_jd
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert row["role"] == "Senior SWE"
        assert row["company"] == "Acme Corp"

    def test_role_company_null_when_no_jd_parsed(
        self, client_no_jd: tuple[TestClient, str, str]
    ) -> None:
        """GET /api/applications returns null role + company when jd-parsed absent."""
        client, _, _ = client_no_jd
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert row["role"] is None
        assert row["company"] is None


# ---------------------------------------------------------------------------
# Detail endpoint — role + company
# ---------------------------------------------------------------------------


class TestGetApplicationRoleCompany:
    def test_role_and_company_in_detail(
        self, client_with_jd: tuple[TestClient, str, str]
    ) -> None:
        """GET /api/applications/{slug} includes role + company from jd-parsed artifact."""
        client, slug, _ = client_with_jd
        resp = client.get(f"/api/applications/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["role"] == "Senior SWE"
        assert data["company"] == "Acme Corp"

    def test_role_company_null_in_detail_when_no_jd_parsed(
        self, client_no_jd: tuple[TestClient, str, str]
    ) -> None:
        """GET /api/applications/{slug} returns null role + company when jd-parsed absent."""
        client, slug, _ = client_no_jd
        resp = client.get(f"/api/applications/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["role"] is None
        assert data["company"] is None


# ---------------------------------------------------------------------------
# ui_phase taxonomy mapping
# ---------------------------------------------------------------------------


def _make_db_with_phase(
    tmp_path: Path, *, phase: str, status: str, suffix: str = ""
) -> tuple[Path, str, str]:
    """Create a pipeline DB with a single run using specified phase + status."""
    db_path = tmp_path / f"jobsmith{suffix}.db"
    conn = open_pipeline_db(db_path)
    slug = f"test-slug{suffix}"
    run_id = f"run-test{suffix}"
    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, phase, "2025-01-01T10:00:00Z", "2025-01-01T10:05:00Z", status),
    )
    conn.commit()
    conn.close()
    return db_path, slug, run_id


def _make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, phase: str, status: str, suffix: str = ""
) -> tuple[TestClient, str]:
    db_path, slug, _ = _make_db_with_phase(tmp_path, phase=phase, status=status, suffix=suffix)
    monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
    app = FastAPI()
    app.include_router(applications_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=True), slug


class TestUiPhaseMapping:
    def test_gather_phase_maps_to_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """raw phase='gather' + status='running' → ui_phase='running'."""
        client, _ = _make_client(tmp_path, monkeypatch, phase="gather", status="running")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "running"

    def test_render_phase_maps_to_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """raw phase='render' + status='running' → ui_phase='running'."""
        client, _ = _make_client(tmp_path, monkeypatch, phase="render", status="running")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "running"

    def test_render_done_maps_to_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """raw phase='render' + status='done' → ui_phase='rendered'."""
        client, _ = _make_client(tmp_path, monkeypatch, phase="render", status="done")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "rendered"

    def test_failed_status_maps_to_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """raw status='failed' → ui_phase='failed' regardless of phase."""
        client, _ = _make_client(tmp_path, monkeypatch, phase="gather", status="failed")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "failed"

    def test_detail_endpoint_exposes_ui_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /api/applications/{slug} also includes ui_phase field."""
        client, slug = _make_client(tmp_path, monkeypatch, phase="render", status="done")
        resp = client.get(f"/api/applications/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "ui_phase" in data
        assert data["ui_phase"] == "rendered"
