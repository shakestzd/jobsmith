"""TDD tests for POST /api/applications/{slug}/run (feat-3c354917).

Covers 8 cases:
1. 202 happy path — jd-parsed.json apply_url present
2. 202 jd.txt fallback — no jd-parsed.json, jd.txt exists
3. 404 — slug directory not found
4. 409 — run already in progress
5. 400 — no JD source (no jd-parsed.json, no jd.txt)
6. apply_url read correctly (not the old jd_url key)
7. jd_url fallback key in jd-parsed.json (backward compat)
8. events_url shape — contains slug and run_id
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.applications import router
from jobsmith.api.supervisor import RunSupervisor

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def apps_dir(tmp_path: Path) -> Path:
    d = tmp_path / "applications"
    d.mkdir()
    return d


def _fake_supervisor(
    run_id: str = "rerun-001",
    *,
    active_slug: str | None = None,
    active_run_id: str | None = None,
) -> RunSupervisor:
    """RunSupervisor with patched start() and optional active run."""
    sup = RunSupervisor(max_buffered_lines=10)
    sup.start = AsyncMock(return_value=run_id)  # type: ignore[method-assign]
    if active_slug is not None and active_run_id is not None:
        sup.get_active_for_slug = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda s: active_run_id if s == active_slug else None
        )
    return sup


def _make_client(apps_dir: Path, supervisor: RunSupervisor) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.applications_dir = apps_dir
    app.state.run_supervisor = supervisor
    return TestClient(app, raise_server_exceptions=False)


def _write_jd_parsed(slug_dir: Path, *, apply_url: str | None = None, jd_url: str | None = None) -> None:
    """Write .apply-state/jd-parsed.json with given URL fields."""
    state_dir = slug_dir / ".apply-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {}
    if apply_url is not None:
        data["apply_url"] = apply_url
    if jd_url is not None:
        data["jd_url"] = jd_url
    (state_dir / "jd-parsed.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Case 1: 202 happy path via jd-parsed.json apply_url
# ---------------------------------------------------------------------------


class TestRerunHappyPathApplyUrl:
    def test_returns_202(self, apps_dir: Path) -> None:
        slug_dir = apps_dir / "acme-backend"
        slug_dir.mkdir()
        _write_jd_parsed(slug_dir, apply_url="https://jobs.acme.com/backend")

        sup = _fake_supervisor("rerun-001")
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/acme-backend/run", json={})
        assert resp.status_code == 202

    def test_body_has_slug_run_id_events_url(self, apps_dir: Path) -> None:
        slug_dir = apps_dir / "acme-backend"
        slug_dir.mkdir()
        _write_jd_parsed(slug_dir, apply_url="https://jobs.acme.com/backend")

        sup = _fake_supervisor("rerun-001")
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/acme-backend/run", json={})
        body = resp.json()
        assert body["slug"] == "acme-backend"
        assert body["run_id"] == "rerun-001"
        assert "events" in body["events_url"]

    def test_events_url_canonical_shape(self, apps_dir: Path) -> None:
        """events_url is the canonical SSE path /api/applications/{slug}/events
        — no run_id query string, since events.py:openEventStream uses
        ?verbosity= and ignores run_id (review job 938)."""
        slug_dir = apps_dir / "techcorp-sre"
        slug_dir.mkdir()
        _write_jd_parsed(slug_dir, apply_url="https://techcorp.io/jobs/sre")

        sup = _fake_supervisor("run-sre-01")
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/techcorp-sre/run", json={})
        body = resp.json()
        assert body["events_url"] == "/api/applications/techcorp-sre/events"


# ---------------------------------------------------------------------------
# Case 2: 202 fallback to jd.txt
# ---------------------------------------------------------------------------


class TestRerunJdTxtFallback:
    def test_returns_202_with_jd_txt(self, apps_dir: Path) -> None:
        slug_dir = apps_dir / "pasted-20250101"
        slug_dir.mkdir()
        (slug_dir / "jd.txt").write_text("Senior Python Engineer ...", encoding="utf-8")

        sup = _fake_supervisor("rerun-txt-01")
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/pasted-20250101/run", json={})
        assert resp.status_code == 202

    def test_slug_and_run_id_in_response(self, apps_dir: Path) -> None:
        slug_dir = apps_dir / "pasted-20250102"
        slug_dir.mkdir()
        (slug_dir / "jd.txt").write_text("Backend Engineer role ...", encoding="utf-8")

        sup = _fake_supervisor("rerun-txt-02")
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/pasted-20250102/run")
        body = resp.json()
        assert body["slug"] == "pasted-20250102"
        assert body["run_id"] == "rerun-txt-02"


# ---------------------------------------------------------------------------
# Case 3: 404 — slug not found
# ---------------------------------------------------------------------------


class TestRerunSlugNotFound:
    def test_returns_404_for_missing_slug(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/nonexistent-slug/run", json={})
        assert resp.status_code == 404

    def test_detail_mentions_slug(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/ghost-slug/run", json={})
        assert "ghost-slug" in str(resp.json().get("detail", ""))


# ---------------------------------------------------------------------------
# Case 4: 409 — run already in progress
# ---------------------------------------------------------------------------


class TestRerunConflict:
    def test_returns_409_when_active(self, apps_dir: Path) -> None:
        slug = "active-app"
        slug_dir = apps_dir / slug
        slug_dir.mkdir()
        _write_jd_parsed(slug_dir, apply_url="https://example.com/job")

        sup = _fake_supervisor(active_slug=slug, active_run_id="ongoing-run")
        client = _make_client(apps_dir, sup)

        resp = client.post(f"/api/applications/{slug}/run", json={})
        assert resp.status_code == 409

    def test_conflict_detail_has_run_id(self, apps_dir: Path) -> None:
        slug = "running-app"
        slug_dir = apps_dir / slug
        slug_dir.mkdir()
        _write_jd_parsed(slug_dir, apply_url="https://example.com/job")

        sup = _fake_supervisor(active_slug=slug, active_run_id="in-flight-run")
        client = _make_client(apps_dir, sup)

        resp = client.post(f"/api/applications/{slug}/run", json={})
        detail = resp.json().get("detail", {})
        # detail is a dict embedded in the 409
        if isinstance(detail, dict):
            assert detail.get("run_id") == "in-flight-run"
        else:
            assert "in-flight-run" in str(detail)


# ---------------------------------------------------------------------------
# Case 5: 400 — no JD source
# ---------------------------------------------------------------------------


class TestRerunNoJdSource:
    def test_returns_400_no_source(self, apps_dir: Path) -> None:
        slug_dir = apps_dir / "empty-app"
        slug_dir.mkdir()
        # No jd-parsed.json, no jd.txt

        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/empty-app/run", json={})
        assert resp.status_code == 400

    def test_error_mentions_source(self, apps_dir: Path) -> None:
        slug_dir = apps_dir / "bare-app"
        slug_dir.mkdir()

        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/bare-app/run", json={})
        detail = str(resp.json().get("detail", "")).lower()
        assert "jd" in detail or "source" in detail


# ---------------------------------------------------------------------------
# Case 6: apply_url key read (not jd_url) — bug fix from commit 554201b
# ---------------------------------------------------------------------------


class TestRerunApplyUrlKey:
    def test_apply_url_key_is_used(self, apps_dir: Path) -> None:
        """Re-run with apply_url key in jd-parsed.json should succeed (202)."""
        slug_dir = apps_dir / "key-test-app"
        slug_dir.mkdir()
        # Only apply_url is set — old code would look for jd_url and fail
        _write_jd_parsed(slug_dir, apply_url="https://jobs.example.com/eng")

        sup = _fake_supervisor("key-test-run")
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/key-test-app/run", json={})
        assert resp.status_code == 202

    def test_jd_url_key_fallback(self, apps_dir: Path) -> None:
        """Older runs with jd_url key (not apply_url) still work."""
        slug_dir = apps_dir / "legacy-key-app"
        slug_dir.mkdir()
        # Only jd_url is set (old schema)
        _write_jd_parsed(slug_dir, jd_url="https://jobs.example.com/old")

        sup = _fake_supervisor("legacy-run")
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications/legacy-key-app/run", json={})
        assert resp.status_code == 202
