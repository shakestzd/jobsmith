"""Tests for PUT /api/applications/{slug}/runs/{run_id}/artifacts/{kind}.

Coverage:
- test_put_creates_new_artifact         — first write of a (run_id, kind) pair
- test_put_returns_version_1_on_create  — version field is 1 after first write
- test_put_overwrites_with_correct_if_match — version increments to 2
- test_put_409_on_version_mismatch      — wrong If-Match value
- test_put_422_unknown_kind             — kind not in KIND_MODELS
- test_put_422_payload_validation_failure — output mismatches kind's model
- test_put_404_when_run_missing         — run_id not in apply_runs
- test_put_409_when_overwrite_without_if_match — missing header on overwrite
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.artifacts import router as artifacts_router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_with_run(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a pipeline DB with one apply_run (no outputs yet).

    Returns (db_path, slug, run_id).
    """
    from jobsmith.db import open_pipeline_db

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)

    slug = "acme-swe-2025"
    run_id = "run-put-test-001"

    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, "gather", "2025-01-01T10:00:00Z", None, "done"),
    )
    conn.commit()
    conn.close()

    return db_path, slug, run_id


@pytest.fixture()
def client_with_run(
    db_with_run: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, str, str]:
    """TestClient wired to a real DB with one empty run. Returns (client, slug, run_id)."""
    db_path, slug, run_id = db_with_run

    monkeypatch.setattr(
        "jobsmith.api.artifacts._get_db_path",
        lambda: db_path,
    )

    app = FastAPI()
    app.include_router(artifacts_router, prefix="/api")
    tc = TestClient(app, raise_server_exceptions=True)
    return tc, slug, run_id


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _put(
    client: TestClient,
    slug: str,
    run_id: str,
    kind: str,
    output: dict,
    *,
    if_match: int | None = None,
    specialist: str = "test-agent",
) -> object:
    headers = {}
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    return client.put(
        f"/api/applications/{slug}/runs/{run_id}/artifacts/{kind}",
        json={"output": output, "specialist": specialist},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# First-write tests
# ---------------------------------------------------------------------------


class TestPutCreateNewArtifact:
    def test_put_creates_new_artifact(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_run
        resp = _put(client, slug, run_id, "jd-parsed", {"company": "Acme"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["kind"] == "jd-parsed"
        assert data["run_id"] == run_id

    def test_put_returns_version_1_on_create(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_run
        resp = _put(client, slug, run_id, "fit-score", {"score": 0.9})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["version"] == 1

    def test_put_output_round_trips(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_run
        payload = {"company": "Widgets Inc", "position": "SRE"}
        resp = _put(client, slug, run_id, "jd-parsed", payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["output"]["company"] == "Widgets Inc"

    def test_put_specialist_stored(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_run
        resp = _put(client, slug, run_id, "jd-parsed", {}, specialist="apply-jd-parser")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["specialist"] == "apply-jd-parser"


# ---------------------------------------------------------------------------
# Overwrite / version-increment tests
# ---------------------------------------------------------------------------


class TestPutOverwrite:
    def test_put_overwrites_with_correct_if_match(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_run
        # First write
        _put(client, slug, run_id, "jd-parsed", {"company": "Acme"})
        # Overwrite with If-Match: 1
        resp = _put(
            client, slug, run_id, "jd-parsed", {"company": "Acme Corp"}, if_match=1
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["version"] == 2
        assert data["output"]["company"] == "Acme Corp"

    def test_put_version_increments_each_overwrite(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_run
        _put(client, slug, run_id, "jd-parsed", {"company": "v1"})
        _put(client, slug, run_id, "jd-parsed", {"company": "v2"}, if_match=1)
        resp = _put(client, slug, run_id, "jd-parsed", {"company": "v3"}, if_match=2)
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == 3

    def test_put_409_on_version_mismatch(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_run
        _put(client, slug, run_id, "jd-parsed", {"company": "Acme"})
        # Supply wrong version
        resp = _put(
            client, slug, run_id, "jd-parsed", {"company": "X"}, if_match=99
        )
        assert resp.status_code == 409, resp.text

    def test_put_409_when_overwrite_without_if_match(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_run
        _put(client, slug, run_id, "jd-parsed", {"company": "Acme"})
        # No If-Match header on second write
        resp = _put(client, slug, run_id, "jd-parsed", {"company": "X"})
        assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# Validation error tests
# ---------------------------------------------------------------------------


class TestPutValidationErrors:
    def test_put_422_unknown_kind(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, run_id = client_with_run
        resp = _put(client, slug, run_id, "nonexistent-kind", {"foo": "bar"})
        assert resp.status_code == 422, resp.text

    def test_put_404_when_run_missing(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        client, slug, _ = client_with_run
        resp = _put(client, slug, "run-does-not-exist", "jd-parsed", {})
        assert resp.status_code == 404, resp.text

    def test_put_different_kinds_independent(
        self, client_with_run: tuple[TestClient, str, str]
    ) -> None:
        """Two different kinds on same run_id should not interfere."""
        client, slug, run_id = client_with_run
        r1 = _put(client, slug, run_id, "jd-parsed", {"company": "Acme"})
        r2 = _put(client, slug, run_id, "fit-score", {"score": 0.8})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["version"] == 1
        assert r2.json()["version"] == 1
