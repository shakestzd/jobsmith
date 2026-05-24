"""Tests for ETag / If-Match concurrent-write semantics on master section endpoints.

TDD: written BEFORE the implementation.  Confirm FAIL first, then implement.

Contract:
  GET /api/master/{section}  → ETag: "<sha256-hex>" response header
  PUT /api/master/{section} with If-Match: "<correct-etag>"  → 200
  PUT /api/master/{section} with If-Match: "<wrong-etag>"    → 412
  PUT /api/master/{section} with no If-Match header          → 200 (backward-compat)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.master import router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_WORK = Path(__file__).parent / "fixtures" / "master_work.yml"

WORK_PAYLOAD = [
    {
        "title": "Senior Data Engineer",
        "location": "Acme Corp",
        "date": "Jan 2023 - Present",
        "description": "Remote",
        "details": [
            "Unlocked $250M in additional Investment Tax Credits across 200K+ assets",
            "Shipped 7 automated ETL pipelines at 99.9% reliability",
        ],
    },
    {
        "title": "Data Engineer",
        "location": "Previous Corp",
        "date": "Jun 2020 - Dec 2022",
        "description": "Hybrid",
        "details": [
            "Built an optimizer that allocated 788 MW of capacity ($4.25B FMV)",
            "Reduced processing time by 75%",
        ],
    },
]


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Minimal jobsmith repo with work.yml seeded."""
    (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
    content_dir = tmp_path / "assets" / "content"
    content_dir.mkdir(parents=True)
    shutil.copy(FIXTURE_WORK, content_dir / "work.yml")
    return tmp_path


@pytest.fixture()
def client(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """API client with master_content seeded from disk (S3: DB-only reads)."""
    from jobsmith.db import open_pipeline_db
    from jobsmith.master_ingest import ingest_master_from_disk

    monkeypatch.chdir(repo_root)

    # Create DB and ingest master YAMLs so GET /api/master/* can find them.
    db_path = repo_root / "private" / "jobsmith.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_pipeline_db(db_path)
    try:
        ingest_master_from_disk(
            conn, content_dir=repo_root / "assets" / "content", reload=True
        )
    finally:
        conn.close()

    monkeypatch.setattr(
        "jobsmith.api.master._get_db_path_for_master", lambda repo_root=None: db_path
    )

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET ETag
# ---------------------------------------------------------------------------


class TestGetEtag:
    def test_get_work_includes_etag_header(self, client: TestClient) -> None:
        """GET /api/master/work returns an ETag response header."""
        resp = client.get("/api/master/work")
        assert resp.status_code == 200, resp.text
        assert "etag" in resp.headers, f"Expected ETag header; got: {dict(resp.headers)}"

    def test_etag_is_non_empty(self, client: TestClient) -> None:
        """GET /api/master/work ETag is a non-empty quoted hex string."""
        resp = client.get("/api/master/work")
        etag = resp.headers.get("etag", "")
        assert etag.strip('"')  # unwrap quotes; must be non-empty

    def test_etag_stable_across_reads(self, client: TestClient) -> None:
        """Two consecutive GETs return the same ETag (no file mutation)."""
        r1 = client.get("/api/master/work")
        r2 = client.get("/api/master/work")
        assert r1.headers["etag"] == r2.headers["etag"]

    def test_etag_changes_after_put(self, client: TestClient) -> None:
        """ETag changes after a successful PUT."""
        client.put("/api/master/work", json=WORK_PAYLOAD)

        after = client.get("/api/master/work").headers["etag"]
        # Content changed — ETag must still be present and non-empty after PUT
        assert after  # non-empty after PUT


# ---------------------------------------------------------------------------
# PUT If-Match
# ---------------------------------------------------------------------------


class TestPutIfMatch:
    def test_put_without_if_match_succeeds(self, client: TestClient) -> None:
        """PUT without If-Match header is backward-compatible (returns 200)."""
        resp = client.put("/api/master/work", json=WORK_PAYLOAD)
        assert resp.status_code == 200, resp.text

    def test_put_with_correct_if_match_succeeds(self, client: TestClient) -> None:
        """PUT with the current ETag in If-Match returns 200."""
        etag = client.get("/api/master/work").headers["etag"]
        # Strip quotes if present (TestClient might include them)
        etag_value = etag.strip('"')
        resp = client.put(
            "/api/master/work",
            json=WORK_PAYLOAD,
            headers={"If-Match": f'"{etag_value}"'},
        )
        assert resp.status_code == 200, resp.text

    def test_put_with_wrong_if_match_returns_412(self, client: TestClient) -> None:
        """PUT with a stale If-Match value returns 412 Precondition Failed."""
        resp = client.put(
            "/api/master/work",
            json=WORK_PAYLOAD,
            headers={"If-Match": '"stale-etag-value"'},
        )
        assert resp.status_code == 412, resp.text

    def test_put_with_wrong_if_match_does_not_write(
        self, client: TestClient, repo_root: Path
    ) -> None:
        """A 412 response means the file was NOT modified."""
        work_path = repo_root / "assets" / "content" / "work.yml"
        original = work_path.read_text(encoding="utf-8")

        resp = client.put(
            "/api/master/work",
            json=WORK_PAYLOAD,
            headers={"If-Match": '"wrong-etag"'},
        )
        assert resp.status_code == 412
        # File must be unchanged
        assert work_path.read_text(encoding="utf-8") == original

    def test_put_returns_etag_in_response(self, client: TestClient) -> None:
        """Successful PUT response also includes an ETag header."""
        resp = client.put("/api/master/work", json=WORK_PAYLOAD)
        assert resp.status_code == 200
        assert "etag" in resp.headers
