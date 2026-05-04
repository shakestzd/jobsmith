"""Tests for POST /api/applications — create + queue new application run.

TDD — written before implementation (Step 1).
The supervisor is always mocked — no real subprocesses spawned.

7 tests:
  1. test_create_with_jd_url
  2. test_create_with_jd_text
  3. test_create_with_jd_file_b64
  4. test_create_400_if_no_input_set
  5. test_create_400_if_multiple_inputs_set
  6. test_create_409_if_slug_already_exists
  7. test_create_returns_events_url_with_run_id_query_param
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake supervisor
# ---------------------------------------------------------------------------

class _FakeSupervisor:
    """Minimal stub that records calls to start() and returns a stable run_id."""

    STABLE_RUN_ID = "test-run-1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], Path]] = []

    async def start(self, slug: str, argv: list[str], cwd: Path) -> str:
        self.calls.append((slug, argv, cwd))
        return self.STABLE_RUN_ID

    def get_active_for_slug(self, slug: str) -> str | None:
        return None

    def get(self, run_id: str):
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_supervisor() -> _FakeSupervisor:
    return _FakeSupervisor()


@pytest.fixture()
def apps_dir(tmp_path: Path) -> Path:
    d = tmp_path / "applications"
    d.mkdir()
    return d


def _make_client(apps_dir: Path, fake_sup: _FakeSupervisor) -> TestClient:
    from jobsmith.api.main import create_app

    app = create_app(applications_dir=apps_dir)

    # Patch the _supervisor() helper inside the applications module so tests
    # never touch the real supervisor.
    import jobsmith.api.applications as apps_mod
    apps_mod._supervisor = lambda: fake_sup  # type: ignore[attr-defined]

    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. test_create_with_jd_url
# ---------------------------------------------------------------------------


def test_create_with_jd_url(apps_dir: Path, fake_supervisor: _FakeSupervisor) -> None:
    """POST with jd_url → 201, slug derived from URL, supervisor.start called."""
    client = _make_client(apps_dir, fake_supervisor)

    resp = client.post(
        "/api/applications",
        json={"jd_url": "https://example.com/jobs/senior-engineer"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "senior-engineer"
    assert body["run_id"] == _FakeSupervisor.STABLE_RUN_ID
    assert "events_url" in body

    # Supervisor was called once with the right slug
    assert len(fake_supervisor.calls) == 1
    slug_arg, argv, cwd = fake_supervisor.calls[0]
    assert slug_arg == "senior-engineer"
    assert "https://example.com/jobs/senior-engineer" in argv
    assert "jobsmith" in argv
    assert "apply" in argv


# ---------------------------------------------------------------------------
# 2. test_create_with_jd_text
# ---------------------------------------------------------------------------


def test_create_with_jd_text(apps_dir: Path, fake_supervisor: _FakeSupervisor) -> None:
    """POST with jd_text → jd.txt written, supervisor called with --jd-text-file."""
    client = _make_client(apps_dir, fake_supervisor)

    resp = client.post(
        "/api/applications",
        json={"jd_text": "Senior Backend Engineer at Acme Corp\n\nWe are looking for..."},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    slug = body["slug"]
    assert slug  # some slug was generated

    # jd.txt must exist under the slug dir
    jd_file = apps_dir / slug / "jd.txt"
    assert jd_file.exists()
    content = jd_file.read_text()
    assert "Senior Backend Engineer at Acme Corp" in content

    # Supervisor was called with --jd-text-file flag
    assert len(fake_supervisor.calls) == 1
    _, argv, _ = fake_supervisor.calls[0]
    assert "--jd-text-file" in argv
    # The path after --jd-text-file should point to the jd.txt we wrote
    idx = argv.index("--jd-text-file")
    assert argv[idx + 1] == str(jd_file)


# ---------------------------------------------------------------------------
# 3. test_create_with_jd_file_b64
# ---------------------------------------------------------------------------


def test_create_with_jd_file_b64(apps_dir: Path, fake_supervisor: _FakeSupervisor) -> None:
    """POST with base64-encoded JD → content decoded, jd.txt written."""
    raw_text = "Product Manager at Startup\n\nExciting opportunity..."
    b64 = base64.b64encode(raw_text.encode()).decode()

    client = _make_client(apps_dir, fake_supervisor)

    resp = client.post(
        "/api/applications",
        json={"jd_file_b64": b64},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    slug = body["slug"]

    jd_file = apps_dir / slug / "jd.txt"
    assert jd_file.exists()
    assert jd_file.read_text() == raw_text

    # Supervisor called with --jd-text-file
    assert len(fake_supervisor.calls) == 1
    _, argv, _ = fake_supervisor.calls[0]
    assert "--jd-text-file" in argv


# ---------------------------------------------------------------------------
# 4. test_create_400_if_no_input_set
# ---------------------------------------------------------------------------


def test_create_400_if_no_input_set(apps_dir: Path, fake_supervisor: _FakeSupervisor) -> None:
    """POST with no jd_url/jd_text/jd_file_b64 → 400."""
    client = _make_client(apps_dir, fake_supervisor)

    resp = client.post("/api/applications", json={})

    assert resp.status_code == 400
    assert "one" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 5. test_create_400_if_multiple_inputs_set
# ---------------------------------------------------------------------------


def test_create_400_if_multiple_inputs_set(apps_dir: Path, fake_supervisor: _FakeSupervisor) -> None:
    """POST with both jd_url and jd_text → 400."""
    client = _make_client(apps_dir, fake_supervisor)

    resp = client.post(
        "/api/applications",
        json={
            "jd_url": "https://example.com/jobs/eng",
            "jd_text": "Some text",
        },
    )

    assert resp.status_code == 400
    assert "one" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 6. test_create_409_if_slug_already_exists
# ---------------------------------------------------------------------------


def test_create_409_if_slug_already_exists(apps_dir: Path, fake_supervisor: _FakeSupervisor) -> None:
    """If slug dir already exists → 409 Conflict."""
    # Pre-create the slug dir
    (apps_dir / "senior-engineer").mkdir()

    client = _make_client(apps_dir, fake_supervisor)

    resp = client.post(
        "/api/applications",
        json={"jd_url": "https://example.com/jobs/senior-engineer"},
    )

    assert resp.status_code == 409
    assert fake_supervisor.calls == []  # supervisor must NOT be called


# ---------------------------------------------------------------------------
# 7. test_create_returns_events_url_with_run_id_query_param
# ---------------------------------------------------------------------------


def test_create_returns_events_url_with_run_id_query_param(
    apps_dir: Path, fake_supervisor: _FakeSupervisor
) -> None:
    """events_url must contain the run_id as a query param."""
    client = _make_client(apps_dir, fake_supervisor)

    resp = client.post(
        "/api/applications",
        json={"jd_url": "https://example.com/jobs/senior-engineer"},
    )

    assert resp.status_code == 201
    body = resp.json()
    slug = body["slug"]
    run_id = body["run_id"]
    events_url = body["events_url"]

    assert slug in events_url
    assert f"run_id={run_id}" in events_url
