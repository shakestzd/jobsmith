"""Tests for POST /api/applications/{slug}/run re-run endpoint.

TDD — written before implementation. Tests use FastAPI TestClient with tmp_path
fixtures that replicate real artifact layouts. A fake supervisor (distinct from
any feat-4d9cc3e5 fixtures) is used to avoid spawning real subprocesses.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake supervisor fixture
# (named _rerun_fake_supervisor to avoid collision with feat-4d9cc3e5 fixtures)
# ---------------------------------------------------------------------------


class _FakeSupervisor:
    """Minimal fake RunSupervisor for re-run endpoint tests."""

    def __init__(self, *, active_run_id: str | None = None, new_run_id: str = "abc123") -> None:
        self._active_run_id = active_run_id
        self._new_run_id = new_run_id
        self.started_argv: list[str] | None = None
        self.started_slug: str | None = None
        self.started_cwd: Path | None = None

    def get_active_for_slug(self, slug: str) -> str | None:
        return self._active_run_id

    async def start(self, slug: str, argv: list[str], cwd: Path) -> str:
        self.started_slug = slug
        self.started_argv = argv
        self.started_cwd = cwd
        return self._new_run_id


@pytest.fixture()
def rerun_fake_supervisor():
    """Return a fresh _FakeSupervisor with no active run."""
    return _FakeSupervisor()


# ---------------------------------------------------------------------------
# App factory helpers
# ---------------------------------------------------------------------------


def _make_rerun_app(
    applications_dir: Path,
    supervisor: _FakeSupervisor | None = None,
) -> TestClient:
    """Construct a TestClient with applications_dir + optional supervisor injected."""
    from jobsmith.api.main import create_app

    app = create_app(applications_dir=applications_dir)
    if supervisor is not None:
        # Patch the supervisor getter used by the re-run handler
        import jobsmith.api.applications as apps_mod
        app.state._test_supervisor = supervisor

        # Monkey-patch _supervisor() at module level for this test
        import jobsmith.api.applications as apps_module
        original = getattr(apps_module, "_supervisor", None)
        apps_module._supervisor = lambda: supervisor  # type: ignore[attr-defined]

    return TestClient(app)


def _slug_dir_rerun(applications_dir: Path, slug: str) -> Path:
    d = applications_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_dir_rerun(slug_dir: Path) -> Path:
    d = slug_dir / ".apply-state"
    d.mkdir(exist_ok=True)
    return d


def _write_jd_parsed_rerun(state_dir: Path, jd_url: str = "https://example.com/jobs/1") -> None:
    (state_dir / "jd-parsed.json").write_text(
        json.dumps({"position": "Engineer", "company": "Acme", "jd_url": jd_url})
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_rerun_404_for_missing_slug(tmp_path: Path) -> None:
    """POST /run on non-existent slug → 404."""
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    supervisor = _FakeSupervisor()

    import jobsmith.api.applications as apps_module
    apps_module._supervisor = lambda: supervisor  # type: ignore[attr-defined]

    from jobsmith.api.main import create_app
    app = create_app(applications_dir=apps_dir)
    client = TestClient(app)

    resp = client.post("/api/applications/no-such-slug/run", json={})
    assert resp.status_code == 404
    assert "no-such-slug" in resp.json()["detail"]


def test_rerun_starts_supervisor_and_returns_run_id(tmp_path: Path) -> None:
    """POST /run with jd-parsed.json present → 202 with run_id."""
    apps_dir = tmp_path / "applications"
    slug = "acme-swe"
    slug_dir = _slug_dir_rerun(apps_dir, slug)
    state_dir = _state_dir_rerun(slug_dir)
    _write_jd_parsed_rerun(state_dir, jd_url="https://example.com/jobs/42")

    supervisor = _FakeSupervisor(new_run_id="run-xyz")

    import jobsmith.api.applications as apps_module
    apps_module._supervisor = lambda: supervisor  # type: ignore[attr-defined]

    from jobsmith.api.main import create_app
    app = create_app(applications_dir=apps_dir)
    client = TestClient(app)

    resp = client.post(f"/api/applications/{slug}/run", json={})

    assert resp.status_code == 202
    body = resp.json()
    assert body["slug"] == slug
    assert body["run_id"] == "run-xyz"
    assert "events_url" in body
    assert "run-xyz" in body["events_url"]


def test_rerun_409_if_already_running(tmp_path: Path) -> None:
    """POST /run when a run is already in flight → 409 with existing run_id."""
    apps_dir = tmp_path / "applications"
    slug = "beta-pm"
    slug_dir = _slug_dir_rerun(apps_dir, slug)
    state_dir = _state_dir_rerun(slug_dir)
    _write_jd_parsed_rerun(state_dir)

    supervisor = _FakeSupervisor(active_run_id="existing-run-99")

    import jobsmith.api.applications as apps_module
    apps_module._supervisor = lambda: supervisor  # type: ignore[attr-defined]

    from jobsmith.api.main import create_app
    app = create_app(applications_dir=apps_dir)
    client = TestClient(app)

    resp = client.post(f"/api/applications/{slug}/run", json={})

    assert resp.status_code == 409
    body = resp.json()
    # FastAPI wraps HTTPException body in "detail"
    detail = body.get("detail", body)
    assert detail["run_id"] == "existing-run-99"
    assert detail["status"] == "running"
    assert detail["slug"] == slug


def test_rerun_force_passes_through(tmp_path: Path) -> None:
    """POST /run with force:true → --force in argv passed to supervisor."""
    apps_dir = tmp_path / "applications"
    slug = "gamma-data"
    slug_dir = _slug_dir_rerun(apps_dir, slug)
    state_dir = _state_dir_rerun(slug_dir)
    _write_jd_parsed_rerun(state_dir, jd_url="https://example.com/jobs/99")

    supervisor = _FakeSupervisor()

    import jobsmith.api.applications as apps_module
    apps_module._supervisor = lambda: supervisor  # type: ignore[attr-defined]

    from jobsmith.api.main import create_app
    app = create_app(applications_dir=apps_dir)
    client = TestClient(app)

    resp = client.post(f"/api/applications/{slug}/run", json={"force": True})

    assert resp.status_code == 202
    assert supervisor.started_argv is not None
    assert "--force" in supervisor.started_argv


def test_rerun_text_based_uses_jd_txt(tmp_path: Path) -> None:
    """POST /run when only jd.txt exists → --jd-text-file in argv."""
    apps_dir = tmp_path / "applications"
    slug = "delta-eng"
    slug_dir = _slug_dir_rerun(apps_dir, slug)
    # No .apply-state/jd-parsed.json — only jd.txt at slug root
    (slug_dir / "jd.txt").write_text("We are hiring a Delta Engineer...")

    supervisor = _FakeSupervisor()

    import jobsmith.api.applications as apps_module
    apps_module._supervisor = lambda: supervisor  # type: ignore[attr-defined]

    from jobsmith.api.main import create_app
    app = create_app(applications_dir=apps_dir)
    client = TestClient(app)

    resp = client.post(f"/api/applications/{slug}/run", json={})

    assert resp.status_code == 202
    assert supervisor.started_argv is not None
    assert "--jd-text-file" in supervisor.started_argv
    jd_txt_path = supervisor.started_argv[supervisor.started_argv.index("--jd-text-file") + 1]
    assert jd_txt_path.endswith("jd.txt")


def test_rerun_400_if_no_jd_source(tmp_path: Path) -> None:
    """POST /run when slug dir exists but no jd-parsed.json or jd.txt → 400."""
    apps_dir = tmp_path / "applications"
    slug = "orphan-slug"
    _slug_dir_rerun(apps_dir, slug)  # empty slug dir

    supervisor = _FakeSupervisor()

    import jobsmith.api.applications as apps_module
    apps_module._supervisor = lambda: supervisor  # type: ignore[attr-defined]

    from jobsmith.api.main import create_app
    app = create_app(applications_dir=apps_dir)
    client = TestClient(app)

    resp = client.post(f"/api/applications/{slug}/run", json={})

    assert resp.status_code == 400
    assert "jd" in resp.json()["detail"].lower()


def test_rerun_returns_events_url_with_run_id_query_param(tmp_path: Path) -> None:
    """events_url must include run_id as a query param."""
    apps_dir = tmp_path / "applications"
    slug = "epsilon-co"
    slug_dir = _slug_dir_rerun(apps_dir, slug)
    state_dir = _state_dir_rerun(slug_dir)
    _write_jd_parsed_rerun(state_dir, jd_url="https://example.com/j/7")

    supervisor = _FakeSupervisor(new_run_id="run-qp-test")

    import jobsmith.api.applications as apps_module
    apps_module._supervisor = lambda: supervisor  # type: ignore[attr-defined]

    from jobsmith.api.main import create_app
    app = create_app(applications_dir=apps_dir)
    client = TestClient(app)

    resp = client.post(f"/api/applications/{slug}/run", json={})

    assert resp.status_code == 202
    body = resp.json()
    events_url = body["events_url"]
    assert f"/api/applications/{slug}/events" in events_url
    assert "run_id=run-qp-test" in events_url
