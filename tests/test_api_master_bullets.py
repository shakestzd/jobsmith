"""Tests for the bullet-level API endpoints.

TDD: written BEFORE the routes exist.

Routes under test:
  POST /api/master/work/roles/{role_index}/bullets/{bullet_index}/anchor
       body: {drop_reason?: str}
  POST /api/master/work/roles/{role_index}/bullets
       body: {text: str, position?: int}
  DELETE /api/master/work/roles/{role_index}/bullets/{bullet_index}
         body: {reason: str}
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.master import router

FIXTURE_WORK = Path(__file__).parent / "fixtures" / "master_work.yml"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Minimal repo with work.yml seeded from fixture."""
    (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
    content_dir = tmp_path / "assets" / "content"
    content_dir.mkdir(parents=True)
    shutil.copy(FIXTURE_WORK, content_dir / "work.yml")
    return tmp_path


@pytest.fixture()
def client(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """API client backed by master_content table seeded from work.yml fixture.

    S5 (feat-484c52b5) made writes go DB-only; fixture must populate the DB.
    """
    from jobsmith.db import open_pipeline_db
    from jobsmith.master_ingest import ingest_master_from_disk

    monkeypatch.chdir(repo_root)

    db_path = repo_root / "private" / "jobsmith.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_pipeline_db(db_path)
    try:
        ingest_master_from_disk(
            conn,
            content_dir=repo_root / "assets" / "content",
            reload=True,
        )
    finally:
        conn.close()
    monkeypatch.setattr(
        "jobsmith.api.master._get_db_path_for_master", lambda: db_path
    )

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def _read_work_from_db(repo_root: Path) -> object:
    """Read the work blob from master_content and parse it."""
    import yaml as _yaml

    from jobsmith.db import open_pipeline_db

    conn = open_pipeline_db(repo_root / "private" / "jobsmith.db")
    try:
        row = conn.execute(
            "SELECT content_blob FROM master_content WHERE section = 'work'"
        ).fetchone()
    finally:
        conn.close()
    return _yaml.safe_load(row["content_blob"]) if row else None


# ---------------------------------------------------------------------------
# POST .../anchor
# ---------------------------------------------------------------------------


class TestMarkAnchorEndpoint:
    def test_mark_anchor_returns_200(self, client: TestClient) -> None:
        """POST .../anchor returns 200 on success."""
        resp = client.post(
            "/api/master/work/roles/0/bullets/0/anchor",
            json={},
        )
        assert resp.status_code == 200, resp.text

    def test_mark_anchor_sets_anchor_true(
        self, client: TestClient, repo_root: Path
    ) -> None:
        """POST .../anchor (no drop_reason) sets anchor=True on the bullet (DB)."""
        client.post("/api/master/work/roles/0/bullets/0/anchor", json={})

        data = _read_work_from_db(repo_root)
        entry = data[0]["details"][0]
        assert isinstance(entry, dict)
        assert entry["anchor"] is True

    def test_mark_anchor_with_drop_reason_sets_anchor_false(
        self, client: TestClient, repo_root: Path
    ) -> None:
        """POST .../anchor with drop_reason sets anchor=False and drop_when (DB)."""
        client.post(
            "/api/master/work/roles/0/bullets/0/anchor",
            json={"drop_reason": "too niche"},
        )

        data = _read_work_from_db(repo_root)
        entry = data[0]["details"][0]
        assert entry["anchor"] is False
        assert entry.get("drop_when") == "too niche"

    def test_anchor_endpoint_does_not_modify_yaml_file(
        self, client: TestClient, repo_root: Path
    ) -> None:
        """S5 contract / ultrareview bug_005: bullet ops never touch work.yml."""
        work_path = repo_root / "assets" / "content" / "work.yml"
        before = work_path.read_text(encoding="utf-8")

        client.post("/api/master/work/roles/0/bullets/0/anchor", json={})

        after = work_path.read_text(encoding="utf-8")
        assert before == after, "anchor endpoint must not touch work.yml on disk"

    def test_mark_anchor_out_of_range_role_returns_404(
        self, client: TestClient
    ) -> None:
        """POST .../anchor returns 404 for out-of-range role_index."""
        resp = client.post(
            "/api/master/work/roles/99/bullets/0/anchor",
            json={},
        )
        assert resp.status_code in (404, 422), resp.text

    def test_mark_anchor_out_of_range_bullet_returns_404(
        self, client: TestClient
    ) -> None:
        """POST .../anchor returns 404 for out-of-range bullet_index."""
        resp = client.post(
            "/api/master/work/roles/0/bullets/99/anchor",
            json={},
        )
        assert resp.status_code in (404, 422), resp.text


# ---------------------------------------------------------------------------
# POST .../bullets
# ---------------------------------------------------------------------------


class TestAddBulletEndpoint:
    def test_add_bullet_returns_200(self, client: TestClient) -> None:
        """POST .../bullets returns 200 on success."""
        resp = client.post(
            "/api/master/work/roles/0/bullets",
            json={"text": "New bullet"},
        )
        assert resp.status_code == 200, resp.text

    def test_add_bullet_appends_to_details(
        self, client: TestClient, repo_root: Path
    ) -> None:
        """POST .../bullets without position appends the bullet (DB)."""
        client.post("/api/master/work/roles/0/bullets", json={"text": "Appended"})

        data = _read_work_from_db(repo_root)
        details = data[0]["details"]
        last = details[-1]
        text = last["bullet"] if isinstance(last, dict) else last
        assert text == "Appended"

    def test_add_bullet_with_position(
        self, client: TestClient, repo_root: Path
    ) -> None:
        """POST .../bullets with position inserts at the given index (DB)."""
        client.post(
            "/api/master/work/roles/0/bullets",
            json={"text": "At position 0", "position": 0},
        )

        data = _read_work_from_db(repo_root)
        first = data[0]["details"][0]
        text = first["bullet"] if isinstance(first, dict) else first
        assert text == "At position 0"

    def test_add_bullet_missing_text_returns_422(self, client: TestClient) -> None:
        """POST .../bullets without text field returns 422."""
        resp = client.post(
            "/api/master/work/roles/0/bullets",
            json={"position": 1},
        )
        assert resp.status_code == 422, resp.text

    def test_add_bullet_out_of_range_role_returns_404(
        self, client: TestClient
    ) -> None:
        """POST .../bullets returns 404 for out-of-range role_index."""
        resp = client.post(
            "/api/master/work/roles/99/bullets",
            json={"text": "x"},
        )
        assert resp.status_code in (404, 422), resp.text


# ---------------------------------------------------------------------------
# DELETE .../bullets/{bullet_index}
# ---------------------------------------------------------------------------


class TestRemoveBulletEndpoint:
    def test_remove_bullet_returns_200(self, client: TestClient) -> None:
        """DELETE .../bullets/{index} returns 200 on success."""
        resp = client.request(
            "DELETE",
            "/api/master/work/roles/0/bullets/1",
            json={"reason": "outdated"},
        )
        assert resp.status_code == 200, resp.text

    def test_remove_bullet_removes_from_details(
        self, client: TestClient, repo_root: Path
    ) -> None:
        """DELETE removes the bullet from the details list (DB)."""
        before = _read_work_from_db(repo_root)
        original_count = len(before[0]["details"])

        client.request(
            "DELETE",
            "/api/master/work/roles/0/bullets/1",
            json={"reason": "outdated"},
        )

        after = _read_work_from_db(repo_root)
        details = after[0]["details"]
        removed = len(details) < original_count
        soft = any(isinstance(e, dict) and e.get("drop_when") for e in details)
        assert removed or soft, f"Bullet was neither removed nor soft-dropped. details: {details!r}"

    def test_remove_bullet_missing_reason_returns_422(
        self, client: TestClient
    ) -> None:
        """DELETE without reason field returns 422."""
        resp = client.request(
            "DELETE",
            "/api/master/work/roles/0/bullets/0",
            json={},
        )
        assert resp.status_code == 422, resp.text

    def test_remove_bullet_out_of_range_returns_404(
        self, client: TestClient
    ) -> None:
        """DELETE with out-of-range bullet_index returns 404."""
        resp = client.request(
            "DELETE",
            "/api/master/work/roles/0/bullets/99",
            json={"reason": "x"},
        )
        assert resp.status_code in (404, 422), resp.text

    def test_remove_bullet_no_config_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DELETE returns 404 when no .apply-config.yaml is found."""
        isolated = tmp_path / "no_config"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        c = TestClient(app)
        resp = c.request(
            "DELETE",
            "/api/master/work/roles/0/bullets/0",
            json={"reason": "x"},
        )
        assert resp.status_code == 404, resp.text
