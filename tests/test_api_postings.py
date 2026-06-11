"""Tests for GET /api/postings, POST /api/postings/{id}/status,
POST /api/postings/{id}/promote endpoints.

TDD: tests written before implementation (feat-827071e1).

Coverage:
- GET /api/postings returns ranked list (llm_score desc, fast_score desc, first_seen_at desc)
- GET /api/postings?status=sourced filters by status
- GET /api/postings?source=greenhouse filters by source
- GET /api/postings?specialty=backend filters by specialty
- GET /api/postings?min_score=0.7 filters by min llm_score (falls back to fast_score)
- POST /api/postings/{id}/status transitions status (dismiss, queue)
- POST /api/postings/{id}/status rejects invalid status → 422
- POST /api/postings/{id}/status for unknown id → 404
- POST /api/postings/{id}/promote creates an apply_runs row and returns run_id
- POST /api/postings/{id}/promote is idempotent (returns same run_id)
- POST /api/postings/{id}/promote for unknown id → 404
- POST /api/postings/{id}/promote with no jd_text sets jd_fetch_failed=True on response
  (when the JD fetch path returns None — tested via monkeypatch)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.postings_routes import router as postings_router
from jobsmith.db import open_pipeline_db
from jobsmith.sourcing.store import upsert_posting

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a minimal FastAPI app mounting the postings router."""
    monkeypatch.setattr(
        "jobsmith.api.postings_routes._get_db_path", lambda: db_path
    )

    # Never launch a real apply run from tests — individual tests re-patch
    # with recorders to assert launch behavior (bug-fa863c68).
    async def _noop_launch(request, *, slug, url, jd_text):  # noqa: ARG001
        return None

    monkeypatch.setattr(
        "jobsmith.api.postings_routes._launch_apply_run", _noop_launch
    )
    app = FastAPI()
    app.include_router(postings_router, prefix="/api")
    return app


def _seed_postings(db_path: Path) -> list[int]:
    """Insert three postings with varying scores and sources."""
    conn = open_pipeline_db(db_path)
    ids = []
    ids.append(
        upsert_posting(
            conn,
            source="greenhouse/stripe",
            dedup_key="key-a",
            title="Senior Engineer",
            company="Stripe",
            specialty="backend",
            llm_score=0.9,
            fast_score=0.8,
            url="https://stripe.com/jobs/1",
            jd_text="Build payments.",
        )
    )
    ids.append(
        upsert_posting(
            conn,
            source="greenhouse/acme",
            dedup_key="key-b",
            title="Data Engineer",
            company="Acme",
            specialty="data",
            llm_score=0.7,
            fast_score=0.75,
            url="https://acme.com/jobs/2",
            jd_text="Data pipelines.",
        )
    )
    ids.append(
        upsert_posting(
            conn,
            source="email/linkedin",
            dedup_key="key-c",
            title="Frontend Dev",
            company="Widgets",
            specialty="frontend",
            llm_score=None,
            fast_score=0.6,
            url="https://widgets.com/jobs/3",
            jd_text=None,  # email snippet — no jd_text
        )
    )
    conn.commit()
    conn.close()
    return ids


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_and_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "jobsmith.db"
    ids = _seed_postings(db_path)
    app = _make_app(db_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, ids, db_path


# ---------------------------------------------------------------------------
# GET /api/postings
# ---------------------------------------------------------------------------


def test_list_postings_returns_all(client_and_ids) -> None:
    client, ids, _ = client_and_ids
    resp = client.get("/api/postings")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 3


def test_list_postings_ranking(client_and_ids) -> None:
    """Sorted: llm_score desc, fast_score desc, first_seen_at desc."""
    client, ids, _ = client_and_ids
    resp = client.get("/api/postings")
    data = resp.json()
    # llm_score: 0.9, 0.7, None (fast_score 0.6)
    assert data[0]["llm_score"] == pytest.approx(0.9)
    assert data[1]["llm_score"] == pytest.approx(0.7)
    assert data[2]["llm_score"] is None


def test_list_postings_filter_status(client_and_ids) -> None:
    client, ids, db_path = client_and_ids
    # Dismiss one posting first
    conn = open_pipeline_db(db_path)
    conn.execute("UPDATE postings SET status = 'dismissed' WHERE id = ?", (ids[0],))
    conn.commit()
    conn.close()

    resp = client.get("/api/postings?status=sourced")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert all(p["status"] == "sourced" for p in data)
    assert len(data) == 2


def test_list_postings_filter_source(client_and_ids) -> None:
    client, ids, _ = client_and_ids
    resp = client.get("/api/postings?source=greenhouse")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 2
    assert all("greenhouse" in p["source"] for p in data)


def test_list_postings_filter_specialty(client_and_ids) -> None:
    client, ids, _ = client_and_ids
    resp = client.get("/api/postings?specialty=backend")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    assert data[0]["specialty"] == "backend"


def test_list_postings_filter_min_score(client_and_ids) -> None:
    """min_score filters on llm_score (falls back to fast_score when llm_score is NULL)."""
    client, ids, _ = client_and_ids
    # min_score=0.75 → only key-a (llm_score=0.9) passes; key-b 0.7 < 0.75, key-c no llm_score fast=0.6 < 0.75
    resp = client.get("/api/postings?min_score=0.75")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    assert data[0]["llm_score"] == pytest.approx(0.9)


def test_list_postings_filter_min_score_fallback_fast(client_and_ids) -> None:
    """When llm_score is NULL, fast_score is used for min_score filtering."""
    client, ids, _ = client_and_ids
    # min_score=0.55 should include key-c (fast_score=0.6, llm_score=None)
    resp = client.get("/api/postings?min_score=0.55")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 3  # all three pass


# ---------------------------------------------------------------------------
# POST /api/postings/{id}/status
# ---------------------------------------------------------------------------


def test_status_update_dismiss(client_and_ids) -> None:
    client, ids, db_path = client_and_ids
    pid = ids[0]
    resp = client.post(f"/api/postings/{pid}/status", json={"status": "dismissed"})
    assert resp.status_code == 200, resp.text
    # Verify DB
    conn = open_pipeline_db(db_path)
    row = conn.execute("SELECT status FROM postings WHERE id = ?", (pid,)).fetchone()
    conn.close()
    assert row["status"] == "dismissed"


def test_status_update_queue(client_and_ids) -> None:
    client, ids, _ = client_and_ids
    pid = ids[1]
    resp = client.post(f"/api/postings/{pid}/status", json={"status": "queued"})
    assert resp.status_code == 200, resp.text


def test_status_update_invalid_status(client_and_ids) -> None:
    client, ids, _ = client_and_ids
    resp = client.post(f"/api/postings/{ids[0]}/status", json={"status": "bogus"})
    assert resp.status_code == 422, resp.text


def test_status_update_unknown_id(client_and_ids) -> None:
    client, _, _ = client_and_ids
    resp = client.post("/api/postings/99999/status", json={"status": "dismissed"})
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# POST /api/postings/{id}/promote
# ---------------------------------------------------------------------------


def test_promote_creates_apply_run(client_and_ids) -> None:
    client, ids, db_path = client_and_ids
    pid = ids[0]  # key-a, has jd_text
    resp = client.post(f"/api/postings/{pid}/promote")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "run_id" in data
    assert data["jd_fetch_failed"] is False

    # Verify apply_runs row was created
    conn = open_pipeline_db(db_path)
    row = conn.execute(
        "SELECT * FROM apply_runs WHERE run_id = ?", (data["run_id"],)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "in-progress"


def test_promote_updates_posting_status(client_and_ids) -> None:
    client, ids, db_path = client_and_ids
    pid = ids[0]
    client.post(f"/api/postings/{pid}/promote")
    conn = open_pipeline_db(db_path)
    row = conn.execute("SELECT status FROM postings WHERE id = ?", (pid,)).fetchone()
    conn.close()
    assert row["status"] == "promoted"


def test_promote_idempotent(client_and_ids) -> None:
    client, ids, _ = client_and_ids
    pid = ids[0]
    r1 = client.post(f"/api/postings/{pid}/promote")
    r2 = client.post(f"/api/postings/{pid}/promote")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["run_id"] == r2.json()["run_id"]


def test_promote_unknown_id(client_and_ids) -> None:
    client, _, _ = client_and_ids
    resp = client.post("/api/postings/99999/promote")
    assert resp.status_code == 404, resp.text


def test_promote_no_jd_text_sets_warning(client_and_ids, monkeypatch) -> None:
    """Posting with no jd_text: fetch attempted, fails, promote still succeeds
    with jd_fetch_failed=True."""
    client, ids, db_path = client_and_ids
    pid = ids[2]  # key-c, jd_text is None

    # Monkeypatch the JD fetcher to simulate failure
    import jobsmith.api.postings_routes as pr_mod

    async def _fake_fetch(url: str) -> str | None:  # noqa: ARG001
        return None

    monkeypatch.setattr(pr_mod, "_fetch_jd_text", _fake_fetch)

    resp = client.post(f"/api/postings/{pid}/promote")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["jd_fetch_failed"] is True
    assert "run_id" in data


# ---------------------------------------------------------------------------
# bug-fa863c68 — promote must launch the apply run, not just create the row
# ---------------------------------------------------------------------------


def test_promote_launches_apply_run(client_and_ids, monkeypatch) -> None:
    """Promote launches the supervisor apply run for the new application."""
    client, ids, _ = client_and_ids
    import jobsmith.api.postings_routes as pr_mod

    calls: list[tuple] = []

    async def _fake_launch(request, *, slug, url, jd_text):  # noqa: ARG001
        calls.append((slug, url, jd_text))

    monkeypatch.setattr(pr_mod, "_launch_apply_run", _fake_launch)

    resp = client.post(f"/api/postings/{ids[0]}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["launched"] is True
    assert len(calls) == 1
    slug, url, jd_text = calls[0]
    assert slug
    assert url
    assert jd_text  # ids[0] has cached jd_text


def test_promote_second_call_does_not_relaunch(client_and_ids, monkeypatch) -> None:
    """Idempotent promote: an already-promoted posting does not relaunch."""
    client, ids, _ = client_and_ids
    import jobsmith.api.postings_routes as pr_mod

    calls: list[tuple] = []

    async def _fake_launch(request, *, slug, url, jd_text):  # noqa: ARG001
        calls.append((slug, url, jd_text))

    monkeypatch.setattr(pr_mod, "_launch_apply_run", _fake_launch)

    r1 = client.post(f"/api/postings/{ids[0]}/promote")
    r2 = client.post(f"/api/postings/{ids[0]}/promote")
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(calls) == 1
    assert r1.json()["launched"] is True
    assert r2.json()["launched"] is False


def test_promote_launch_failure_does_not_block(client_and_ids, monkeypatch) -> None:
    """A launch error never fails the promote — row is created, launched=False."""
    client, ids, db_path = client_and_ids
    import jobsmith.api.postings_routes as pr_mod

    async def _boom(request, *, slug, url, jd_text):  # noqa: ARG001
        raise RuntimeError("supervisor unavailable")

    monkeypatch.setattr(pr_mod, "_launch_apply_run", _boom)

    resp = client.post(f"/api/postings/{ids[0]}/promote")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["launched"] is False
    assert "run_id" in data

    conn = open_pipeline_db(db_path)
    row = conn.execute(
        "SELECT * FROM apply_runs WHERE run_id = ?", (data["run_id"],)
    ).fetchone()
    conn.close()
    assert row is not None


# ---------------------------------------------------------------------------
# Branch-review finding #2 — GET /postings limit/offset + no jd_text
# ---------------------------------------------------------------------------


def test_list_postings_excludes_jd_text(client_and_ids) -> None:
    """jd_text must not appear in GET /postings list response."""
    client, _, _ = client_and_ids
    resp = client.get("/api/postings")
    assert resp.status_code == 200, resp.text
    for row in resp.json():
        assert "jd_text" not in row, f"jd_text leaked into list response for id={row['id']}"


def test_list_postings_limit(client_and_ids) -> None:
    """limit=1 returns exactly 1 row."""
    client, _, _ = client_and_ids
    resp = client.get("/api/postings?limit=1")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


def test_list_postings_offset(client_and_ids) -> None:
    """offset skips rows; limit+offset together page through results."""
    client, _, _ = client_and_ids
    # 3 rows total; limit=2 offset=0 → 2 rows; limit=2 offset=2 → 1 row
    r1 = client.get("/api/postings?limit=2&offset=0")
    r2 = client.get("/api/postings?limit=2&offset=2")
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(r1.json()) == 2
    assert len(r2.json()) == 1
    # Ensure all IDs are distinct across pages
    ids_page1 = {row["id"] for row in r1.json()}
    ids_page2 = {row["id"] for row in r2.json()}
    assert ids_page1.isdisjoint(ids_page2)


def test_list_postings_limit_exceeds_max_returns_422(client_and_ids) -> None:
    """limit > 1000 (the max) must be rejected with 422."""
    client, _, _ = client_and_ids
    resp = client.get("/api/postings?limit=9999")
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Branch-review finding #3 — URL safety check before JD fetch
# ---------------------------------------------------------------------------


def test_fetch_jd_text_skips_file_scheme(tmp_path: Path) -> None:
    """file:// URLs must not be fetched."""
    import asyncio

    import jobsmith.api.postings_routes as pr_mod

    result = asyncio.get_event_loop().run_until_complete(
        pr_mod._fetch_jd_text(f"file://{tmp_path}/secret.txt")
    )
    assert result is None


def test_fetch_jd_text_skips_localhost(tmp_path: Path) -> None:  # noqa: ARG001
    """http://localhost URLs must not be fetched."""
    import asyncio

    import jobsmith.api.postings_routes as pr_mod

    result = asyncio.get_event_loop().run_until_complete(
        pr_mod._fetch_jd_text("http://localhost/admin")
    )
    assert result is None


def test_fetch_jd_text_skips_127(tmp_path: Path) -> None:  # noqa: ARG001
    """http://127.0.0.1 URLs must not be fetched."""
    import asyncio

    import jobsmith.api.postings_routes as pr_mod

    result = asyncio.get_event_loop().run_until_complete(
        pr_mod._fetch_jd_text("http://127.0.0.1:8080/internal")
    )
    assert result is None


def test_promote_blocked_url_succeeds_with_warning(client_and_ids, monkeypatch) -> None:
    """Posting whose URL is a loopback address: promote succeeds with jd_fetch_failed=True.

    The safety guard returns None (not a network error) so the promote contract
    is unchanged — no 5xx, just jd_fetch_failed=True in the response.
    """
    client, _, db_path = client_and_ids

    # Insert a posting with a localhost URL and no jd_text
    conn = open_pipeline_db(db_path)
    blocked_id = upsert_posting(
        conn,
        source="email/internal",
        dedup_key="key-blocked",
        title="Internal Role",
        company="Internal",
        specialty="backend",
        url="http://127.0.0.1:9999/jobs/42",
        jd_text=None,
    )
    conn.commit()
    conn.close()

    resp = client.post(f"/api/postings/{blocked_id}/promote")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["jd_fetch_failed"] is True
    assert "run_id" in data


def test_is_safe_jd_url_rejects_private_ranges() -> None:
    """_is_safe_jd_url blocks RFC-1918 and loopback hosts."""
    from jobsmith.api.postings_routes import _is_safe_jd_url

    blocked = [
        "http://10.0.0.1/jobs",
        "http://10.255.255.255/jobs",
        "http://172.16.0.1/jobs",
        "http://172.31.0.1/jobs",
        "http://192.168.1.1/jobs",
        "http://127.0.0.1/jobs",
        "http://localhost/jobs",
        "http://0.0.0.0/jobs",
        "file:///etc/passwd",
        "ftp://files.example.com/jobs",
    ]
    for url in blocked:
        assert not _is_safe_jd_url(url), f"Expected {url!r} to be blocked"

    allowed = [
        "https://boards.greenhouse.io/stripe/jobs/123",
        "http://example.com/careers",
    ]
    for url in allowed:
        assert _is_safe_jd_url(url), f"Expected {url!r} to be allowed"
