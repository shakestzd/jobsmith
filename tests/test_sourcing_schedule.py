"""Tests for sourcing schedule, doctor check, run-health API, and plist template.

TDD: written before implementation (feat-80affa8a).

Covers:
  - check_sourcing_health: last-run age vs cadence, degraded sources
  - plist template rendering (render_plist)
  - install-schedule launchctl call is mocked (no live system changes)
  - run-health API endpoint
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobsmith.cli import app
from jobsmith.doctor import run_all_checks

runner_cli = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_with_runs(tmp_path: Path):
    """Return (db_path, conn) with sourcing_runs rows seeded."""
    from jobsmith.db import open_pipeline_db
    from jobsmith.sourcing.store import upsert_sourcing_run

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    # Successful run 2 hours ago
    upsert_sourcing_run(conn, run_id="run-ok-1")
    conn.execute(
        "UPDATE sourcing_runs SET started_at=?, finished_at=?, status='done', "
        "new_count=5, updated_count=3 WHERE run_id='run-ok-1'",
        (
            (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat(),
            (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat(),
        ),
    )
    conn.commit()
    return db_path, conn


@pytest.fixture()
def db_with_failed_run(tmp_path: Path):
    """Return (db_path, conn) with a failed sourcing run."""
    from jobsmith.db import open_pipeline_db
    from jobsmith.sourcing.store import upsert_sourcing_run

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    upsert_sourcing_run(conn, run_id="run-fail-1")
    conn.execute(
        "UPDATE sourcing_runs SET started_at=?, finished_at=?, status='failed', "
        "error='network timeout' WHERE run_id='run-fail-1'",
        (
            (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat(),
            (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat(),
        ),
    )
    conn.commit()
    return db_path, conn


@pytest.fixture()
def db_with_degraded_run(tmp_path: Path):
    """Return (db_path, conn) with a degraded run (some sources failed)."""
    from jobsmith.db import open_pipeline_db
    from jobsmith.sourcing.store import finish_sourcing_run, upsert_sourcing_run

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    upsert_sourcing_run(conn, run_id="run-deg-1")
    finish_sourcing_run(
        conn,
        run_id="run-deg-1",
        status="degraded",
        new_count=2,
        updated_count=1,
        degraded_sources=["greenhouse/somecompany"],
    )
    conn.execute(
        "UPDATE sourcing_runs SET started_at=?, finished_at=? WHERE run_id='run-deg-1'",
        (
            (datetime.now(tz=timezone.utc) - timedelta(hours=3)).isoformat(),
            (datetime.now(tz=timezone.utc) - timedelta(hours=3)).isoformat(),
        ),
    )
    conn.commit()
    return db_path, conn


@pytest.fixture()
def db_stale(tmp_path: Path):
    """Return (db_path, conn) with a run older than 26 hours (beyond daily cadence)."""
    from jobsmith.db import open_pipeline_db
    from jobsmith.sourcing.store import upsert_sourcing_run

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    upsert_sourcing_run(conn, run_id="run-stale-1")
    conn.execute(
        "UPDATE sourcing_runs SET started_at=?, finished_at=?, status='done' "
        "WHERE run_id='run-stale-1'",
        (
            (datetime.now(tz=timezone.utc) - timedelta(hours=26)).isoformat(),
            (datetime.now(tz=timezone.utc) - timedelta(hours=26)).isoformat(),
        ),
    )
    conn.commit()
    return db_path, conn


# ---------------------------------------------------------------------------
# check_sourcing_health
# ---------------------------------------------------------------------------


def test_sourcing_health_no_db_pass(tmp_path: Path) -> None:
    """No DB at all → PASS (skip) with informative message."""
    from jobsmith.doctor import check_sourcing_health

    result = check_sourcing_health(db_path=tmp_path / "nonexistent.db")
    assert result.ok is True
    assert result.name == "sourcing_health"
    assert "no DB" in result.message or "no runs" in result.message or "skip" in result.message.lower()


def test_sourcing_health_no_runs_pass(tmp_path: Path) -> None:
    """DB exists but sourcing_runs is empty → PASS (skip)."""
    from jobsmith.db import open_pipeline_db
    from jobsmith.doctor import check_sourcing_health

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    conn.close()

    result = check_sourcing_health(db_path=db_path)
    assert result.ok is True
    assert "no runs" in result.message or "skip" in result.message.lower()


def test_sourcing_health_recent_success_pass(db_with_runs) -> None:
    """Last run done 2 hours ago → PASS (within 25h daily cadence)."""
    from jobsmith.doctor import check_sourcing_health

    db_path, conn = db_with_runs
    conn.close()

    result = check_sourcing_health(db_path=db_path)
    assert result.ok is True
    assert result.name == "sourcing_health"


def test_sourcing_health_stale_fail(db_stale) -> None:
    """Last run > 25h ago → FAIL with stale message."""
    from jobsmith.doctor import check_sourcing_health

    db_path, conn = db_stale
    conn.close()

    result = check_sourcing_health(db_path=db_path)
    assert result.ok is False
    assert "stale" in result.message.lower() or "overdue" in result.message.lower()
    assert result.remediation is not None


def test_sourcing_health_failed_run_fail(db_with_failed_run) -> None:
    """Last run status=failed → FAIL."""
    from jobsmith.doctor import check_sourcing_health

    db_path, conn = db_with_failed_run
    conn.close()

    result = check_sourcing_health(db_path=db_path)
    assert result.ok is False
    assert "failed" in result.message.lower()
    assert result.remediation is not None


def test_sourcing_health_degraded_warn(db_with_degraded_run) -> None:
    """Last run status=degraded + degraded sources → ok=False, message mentions degraded source."""
    from jobsmith.doctor import check_sourcing_health

    db_path, conn = db_with_degraded_run
    conn.close()

    result = check_sourcing_health(db_path=db_path)
    assert result.ok is False
    assert "degraded" in result.message.lower()


def test_sourcing_health_included_in_run_all_checks(db_with_runs, monkeypatch) -> None:
    """check_sourcing_health is called as part of run_all_checks when db_path is supplied."""

    db_path, conn = db_with_runs
    conn.close()

    # Provide the db_path via monkeypatching the resolver, or pass explicitly
    # We call run_all_checks; check sourcing_health name appears in results.
    results = run_all_checks(db_path=db_path)
    names = [r.name for r in results]
    assert "sourcing_health" in names


# ---------------------------------------------------------------------------
# plist template rendering
# ---------------------------------------------------------------------------


def test_render_plist_contains_label() -> None:
    """Rendered plist has the correct Label."""
    from jobsmith.sourcing.schedule import render_plist

    plist_str = render_plist(
        binary_path=Path("/usr/local/bin/jobsmith"),
        repo_root=Path("/Users/testuser/DevProjects/myrepo"),
        log_dir=Path("/tmp/logs"),
    )
    assert "com.jobsmith.sourcing" in plist_str
    assert "/usr/local/bin/jobsmith" in plist_str
    assert "/Users/testuser/DevProjects/myrepo" in plist_str


def test_render_plist_sets_jobsmith_repo_root() -> None:
    """JOBSMITH_REPO_ROOT env var is set in the rendered plist."""
    from jobsmith.sourcing.schedule import render_plist

    plist_str = render_plist(
        binary_path=Path("/opt/homebrew/bin/jobsmith"),
        repo_root=Path("/home/user/jobs"),
        log_dir=Path("/tmp/logs"),
    )
    assert "JOBSMITH_REPO_ROOT" in plist_str
    assert "/home/user/jobs" in plist_str


def test_render_plist_is_valid_plist_xml() -> None:
    """Rendered plist is parseable XML with a valid dict structure."""
    import xml.etree.ElementTree as ET

    from jobsmith.sourcing.schedule import render_plist

    plist_str = render_plist(
        binary_path=Path("/usr/bin/jobsmith"),
        repo_root=Path("/tmp/repo"),
        log_dir=Path("/tmp/logs"),
    )
    root = ET.fromstring(plist_str)
    assert root.tag == "plist"
    dict_elem = root.find("dict")
    assert dict_elem is not None


def test_render_plist_hour_minute_in_range() -> None:
    """StartCalendarInterval contains Hour and Minute keys."""
    from jobsmith.sourcing.schedule import render_plist

    plist_str = render_plist(
        binary_path=Path("/usr/bin/jobsmith"),
        repo_root=Path("/tmp/repo"),
        log_dir=Path("/tmp/logs"),
    )
    assert "<key>Hour</key>" in plist_str
    assert "<key>Minute</key>" in plist_str


# ---------------------------------------------------------------------------
# install-schedule CLI (launchctl mocked)
# ---------------------------------------------------------------------------


def test_source_install_schedule_renders_and_mocks_launchctl(
    tmp_path: Path, monkeypatch
) -> None:
    """'jobsmith source install-schedule' renders the plist and calls launchctl (mocked)."""
    import shutil

    # Point JOBSMITH_REPO_ROOT to tmp_path
    monkeypatch.setenv("JOBSMITH_REPO_ROOT", str(tmp_path))
    # Create a minimal config
    cfg = tmp_path / ".apply-config.yaml"
    cfg.write_text(
        "master:\n"
        "  work_yml: w.yml\n"
        "  skill_yml: s.yml\n"
        "  education_yml: e.yml\n"
        "  author_yml: a.yml\n"
        "  publication_yml: null\n"
        "output:\n"
        "  applications_dir: private/applications\n"
        "  job_search_db: private/job_search.db\n"
        "  jobsmith_db: private/jobsmith.db\n"
    )

    called_args: list = []

    def mock_run(args, **kwargs):
        called_args.extend(args)
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(subprocess, "run", mock_run)
    # Mock shutil.which so the binary path resolves
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/jobsmith")

    result = runner_cli.invoke(
        app, ["source", "install-schedule"], catch_exceptions=False,
        env={**os.environ, "JOBSMITH_REPO_ROOT": str(tmp_path)},
    )
    assert result.exit_code == 0, result.output
    # launchctl should have been called
    assert any("launchctl" in str(a) for a in called_args)


def test_source_install_schedule_help() -> None:
    """'jobsmith source install-schedule --help' exits 0."""
    result = runner_cli.invoke(app, ["source", "install-schedule", "--help"])
    assert result.exit_code == 0
    assert "install-schedule" in result.output.lower() or "schedule" in result.output.lower()


# ---------------------------------------------------------------------------
# Run-health API
# ---------------------------------------------------------------------------


def _make_test_app(db_path: Path):
    """Create a TestClient for the jobsmith API with a wired db_path."""
    from jobsmith.api.main import create_app

    application = create_app()

    # Wire up a minimal state so the run-health router can resolve the DB
    class _State:
        pass

    application.state.repo_root = db_path.parent

    # Patch the db resolver in run_health_router to return db_path directly
    return TestClient(application), db_path


TOKEN = "test-run-health-token-xyz"


@pytest.fixture()
def _clear_token_cache():
    from jobsmith.api.auth import _get_expected_token
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_run_health_no_auth_401() -> None:
    """GET /api/sourcing/run-health without auth returns 401."""
    import os

    from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
    from jobsmith.api.main import create_app

    _get_expected_token.cache_clear()
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}):
        test_app = TestClient(create_app())
        resp = test_app.get("/api/sourcing/run-health")
    assert resp.status_code == 401


def test_run_health_with_no_db(tmp_path: Path) -> None:
    """GET /api/sourcing/run-health with auth and no DB returns ok state."""
    import os

    from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
    from jobsmith.api.main import create_app

    _get_expected_token.cache_clear()
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}):
        application = create_app()
        with patch(
            "jobsmith.api.run_health._resolve_db_path",
            return_value=None,
        ):
            client = TestClient(application)
            resp = client.get("/api/sourcing/run-health", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert "state" in data
    assert data["state"] in ("ok", "no_runs", "unknown")


def test_run_health_failed_state(tmp_path: Path) -> None:
    """Run-health returns state=failed when last run was failed."""
    import os

    from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
    from jobsmith.api.main import create_app
    from jobsmith.db import open_pipeline_db
    from jobsmith.sourcing.store import upsert_sourcing_run

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    upsert_sourcing_run(conn, run_id="run-f-1")
    conn.execute(
        "UPDATE sourcing_runs SET finished_at=datetime('now'), "
        "status='failed', error='boom' WHERE run_id='run-f-1'"
    )
    conn.commit()
    conn.close()

    _get_expected_token.cache_clear()
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}):
        application = create_app()
        with patch(
            "jobsmith.api.run_health._resolve_db_path",
            return_value=db_path,
        ):
            client = TestClient(application)
            resp = client.get("/api/sourcing/run-health", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "failed"
    assert data.get("last_run_id") == "run-f-1"


def test_run_health_ok_state(db_with_runs) -> None:
    """Run-health returns state=ok when last run was done recently."""
    import os

    from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
    from jobsmith.api.main import create_app

    db_path, conn = db_with_runs
    conn.close()

    _get_expected_token.cache_clear()
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}):
        application = create_app()
        with patch(
            "jobsmith.api.run_health._resolve_db_path",
            return_value=db_path,
        ):
            client = TestClient(application)
            resp = client.get("/api/sourcing/run-health", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ok"


def test_run_health_degraded_state(db_with_degraded_run) -> None:
    """Run-health returns state=degraded when last run was degraded."""
    import os

    from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
    from jobsmith.api.main import create_app

    db_path, conn = db_with_degraded_run
    conn.close()

    _get_expected_token.cache_clear()
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}):
        application = create_app()
        with patch(
            "jobsmith.api.run_health._resolve_db_path",
            return_value=db_path,
        ):
            client = TestClient(application)
            resp = client.get("/api/sourcing/run-health", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "degraded"
    assert data.get("degraded_sources") is not None


def test_run_health_stale_state(db_stale) -> None:
    """Run-health returns state=stale when last successful run > 25h ago."""
    import os

    from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
    from jobsmith.api.main import create_app

    db_path, conn = db_stale
    conn.close()

    _get_expected_token.cache_clear()
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}):
        application = create_app()
        with patch(
            "jobsmith.api.run_health._resolve_db_path",
            return_value=db_path,
        ):
            client = TestClient(application)
            resp = client.get("/api/sourcing/run-health", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "stale"
