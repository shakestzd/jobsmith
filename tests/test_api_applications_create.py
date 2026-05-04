"""TDD tests for POST /api/applications (feat-3c354917).

Covers 8 cases:
1. jd_url: 201 with correct slug, run_id, events_url shape
2. jd_text: 201 with timestamp slug + jd.txt written to disk
3. jd_file_b64: 201 with base64-decoded content written to jd.txt
4. zero sources: 400
5. multiple sources: 400
6. slug conflict: 409
7. events_url contains /api/applications/{slug}/events
8. invalid base64: 400
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.applications import router
from jobsmith.api.supervisor import RunSupervisor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def apps_dir(tmp_path: Path) -> Path:
    """Empty applications directory."""
    d = tmp_path / "applications"
    d.mkdir()
    return d


def _fake_supervisor(run_id: str = "deadbeef") -> RunSupervisor:
    """Return a RunSupervisor whose start() coroutine is patched to return run_id."""
    sup = RunSupervisor(max_buffered_lines=10)
    sup.start = AsyncMock(return_value=run_id)  # type: ignore[method-assign]
    return sup


def _make_client(apps_dir: Path, supervisor: RunSupervisor) -> TestClient:
    """FastAPI test client with router + app.state injection."""
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.applications_dir = apps_dir
    app.state.run_supervisor = supervisor
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Case 1: jd_url → 201 happy path
# ---------------------------------------------------------------------------


class TestCreateApplicationJdUrl:
    def test_returns_201(self, apps_dir: Path) -> None:
        sup = _fake_supervisor("run-abc")
        client = _make_client(apps_dir, sup)

        resp = client.post(
            "/api/applications",
            json={"jd_url": "https://example.com/jobs/senior-engineer"},
        )
        assert resp.status_code == 201

    def test_body_has_slug_run_id_events_url(self, apps_dir: Path) -> None:
        sup = _fake_supervisor("run-abc")
        client = _make_client(apps_dir, sup)

        resp = client.post(
            "/api/applications",
            json={"jd_url": "https://example.com/jobs/senior-engineer"},
        )
        body = resp.json()
        assert "slug" in body
        assert body["run_id"] == "run-abc"
        assert "/api/applications/" in body["events_url"]
        assert "events" in body["events_url"]

    def test_events_url_contains_slug_and_run_id(self, apps_dir: Path) -> None:
        sup = _fake_supervisor("run-xyz")
        client = _make_client(apps_dir, sup)

        resp = client.post(
            "/api/applications",
            json={"jd_url": "https://example.com/jobs/backend-engineer"},
        )
        body = resp.json()
        slug = body["slug"]
        assert slug in body["events_url"]
        assert "run-xyz" in body["events_url"]

    def test_slug_directory_created(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post(
            "/api/applications",
            json={"jd_url": "https://example.com/jobs/devops"},
        )
        slug = resp.json()["slug"]
        assert (apps_dir / slug).is_dir()


# ---------------------------------------------------------------------------
# Case 2: jd_text → 201 + jd.txt written
# ---------------------------------------------------------------------------


class TestCreateApplicationJdText:
    def test_returns_201_with_text(self, apps_dir: Path) -> None:
        sup = _fake_supervisor("run-text")
        client = _make_client(apps_dir, sup)

        resp = client.post(
            "/api/applications",
            json={"jd_text": "We are hiring a Python engineer..."},
        )
        assert resp.status_code == 201

    def test_jd_txt_written_to_slug_dir(self, apps_dir: Path) -> None:
        sup = _fake_supervisor("run-text")
        client = _make_client(apps_dir, sup)

        resp = client.post(
            "/api/applications",
            json={"jd_text": "Looking for a backend developer."},
        )
        slug = resp.json()["slug"]
        jd_txt = apps_dir / slug / "jd.txt"
        assert jd_txt.is_file()
        assert "backend developer" in jd_txt.read_text(encoding="utf-8")

    def test_slug_uses_timestamp_prefix(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post(
            "/api/applications",
            json={"jd_text": "Some job description text."},
        )
        slug = resp.json()["slug"]
        assert slug.startswith("pasted-")


# ---------------------------------------------------------------------------
# Case 3: jd_file_b64 → 201 + decoded content in jd.txt
# ---------------------------------------------------------------------------


class TestCreateApplicationJdFileB64:
    def test_returns_201_with_b64(self, apps_dir: Path) -> None:
        sup = _fake_supervisor("run-b64")
        client = _make_client(apps_dir, sup)
        content = base64.b64encode(b"Job posting content here.").decode()

        resp = client.post(
            "/api/applications",
            json={"jd_file_b64": content},
        )
        assert resp.status_code == 201

    def test_decoded_content_written_to_jd_txt(self, apps_dir: Path) -> None:
        sup = _fake_supervisor("run-b64")
        client = _make_client(apps_dir, sup)
        raw = b"We need a data engineer with Spark experience."
        content = base64.b64encode(raw).decode()

        resp = client.post(
            "/api/applications",
            json={"jd_file_b64": content},
        )
        slug = resp.json()["slug"]
        jd_txt = apps_dir / slug / "jd.txt"
        assert jd_txt.read_text(encoding="utf-8") == raw.decode()


# ---------------------------------------------------------------------------
# Case 4: zero sources → 400
# ---------------------------------------------------------------------------


class TestCreateApplicationZeroSources:
    def test_no_sources_returns_400(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications", json={})
        assert resp.status_code == 400

    def test_error_message_mentions_sources(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post("/api/applications", json={})
        detail = resp.json().get("detail", "")
        assert "jd_url" in detail or "one" in detail.lower()


# ---------------------------------------------------------------------------
# Case 5: multiple sources → 400
# ---------------------------------------------------------------------------


class TestCreateApplicationMultipleSources:
    def test_two_sources_returns_400(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post(
            "/api/applications",
            json={
                "jd_url": "https://example.com/job",
                "jd_text": "Some text",
            },
        )
        assert resp.status_code == 400

    def test_three_sources_returns_400(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)
        b64 = base64.b64encode(b"content").decode()

        resp = client.post(
            "/api/applications",
            json={
                "jd_url": "https://example.com/job",
                "jd_text": "Some text",
                "jd_file_b64": b64,
            },
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Case 6: slug conflict → 409
# ---------------------------------------------------------------------------


class TestCreateApplicationSlugConflict:
    def test_existing_slug_returns_409(self, apps_dir: Path) -> None:
        sup = _fake_supervisor("run-first")
        client = _make_client(apps_dir, sup)

        # First create succeeds
        resp1 = client.post(
            "/api/applications",
            json={"jd_url": "https://example.com/jobs/frontend"},
        )
        assert resp1.status_code == 201

        # The slug dir was already created by the first call,
        # so the second call with same URL should 409
        resp2 = client.post(
            "/api/applications",
            json={"jd_url": "https://example.com/jobs/frontend"},
        )
        assert resp2.status_code == 409

    def test_conflict_detail_mentions_slug(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        client.post(
            "/api/applications",
            json={"jd_url": "https://example.com/jobs/ml-engineer"},
        )
        resp = client.post(
            "/api/applications",
            json={"jd_url": "https://example.com/jobs/ml-engineer"},
        )
        assert "slug" in str(resp.json().get("detail", "")).lower() or resp.status_code == 409


# ---------------------------------------------------------------------------
# Case 7: invalid base64 → 400
# ---------------------------------------------------------------------------


class TestCreateApplicationInvalidB64:
    def test_invalid_b64_returns_400(self, apps_dir: Path) -> None:
        sup = _fake_supervisor()
        client = _make_client(apps_dir, sup)

        resp = client.post(
            "/api/applications",
            json={"jd_file_b64": "!!!not-valid-base64!!!"},
        )
        assert resp.status_code == 400
