"""Tests for GET /api/funnel endpoint.

TDD: tests written before implementation (feat-28a41d1c).

Coverage:
- funnel_counts: sourced, queued, promoted, interview, offer with window filter
- conversion rates between adjacent stages
- cohort = posting first_seen_at in window
- per-source yield (source -> postings -> applied -> interview)
- window=7 / 30 / 90 / all
- empty DB returns zero counts
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.funnel_routes import router as funnel_router
from jobsmith.db import open_pipeline_db
from jobsmith.sourcing.store import upsert_posting

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr("jobsmith.api.funnel_routes._get_db_path", lambda: db_path)
    app = FastAPI()
    app.include_router(funnel_router, prefix="/api")
    return app


def _iso(delta_days: int = 0) -> str:
    """ISO-8601 timestamp relative to now."""
    dt = datetime.now(tz=timezone.utc) - timedelta(days=delta_days)
    return dt.isoformat()


def _seed(db_path: Path) -> None:
    """Insert a set of postings with various statuses and sources.

    Timeline:
      - 3 sourced (from greenhouse/stripe, greenhouse/acme, email/linkedin)
        all seen within last 7 days
      - 1 queued (greenhouse/stripe)  — within 7 days
      - 2 promoted (greenhouse/stripe, email/linkedin) — within 7 days
      - postings promoted to apply_runs; one apply_run has status='interview',
        one has status='offer' (custom free-text statuses, valid in backend)
    """
    conn = open_pipeline_db(db_path)

    # sourced — 3 postings
    for i, src in enumerate(["greenhouse/stripe", "greenhouse/acme", "email/linkedin"]):
        upsert_posting(
            conn,
            source=src,
            dedup_key=f"sourced-{i}",
            title=f"Role {i}",
            company="Co",
            specialty="backend",
            url=f"https://example.com/{i}",
            jd_text="some jd",
        )

    # queued — 1 from greenhouse/stripe
    upsert_posting(
        conn,
        source="greenhouse/stripe",
        dedup_key="queued-0",
        title="Queued Role",
        company="Stripe",
        specialty="backend",
        url="https://stripe.com/q",
        jd_text="jd",
    )
    q_id = conn.execute(
        "SELECT id FROM postings WHERE dedup_key = 'queued-0'"
    ).fetchone()["id"]
    conn.execute("UPDATE postings SET status = 'queued' WHERE id = ?", (q_id,))

    # promoted — 2 postings become apply_runs
    for i, src in enumerate(["greenhouse/stripe", "email/linkedin"]):
        upsert_posting(
            conn,
            source=src,
            dedup_key=f"promoted-{i}",
            title=f"Promoted Role {i}",
            company="Co",
            specialty="backend",
            url=f"https://example.com/promo/{i}",
            jd_text="jd",
        )
        p_id = conn.execute(
            f"SELECT id FROM postings WHERE dedup_key = 'promoted-{i}'"
        ).fetchone()["id"]
        run_id = f"run-promo-{i}"
        conn.execute(
            "INSERT INTO apply_runs (run_id, slug, phase, started_at, status) "
            "VALUES (?, ?, 'gather', ?, 'done')",
            (run_id, f"promo-slug-{i}", _iso()),
        )
        conn.execute(
            "UPDATE postings SET status = 'promoted', promoted_application_id = ? "
            "WHERE id = ?",
            (run_id, p_id),
        )

    # Set one apply_run to interview, one to offer (free-text statuses)
    conn.execute(
        "UPDATE apply_runs SET status = 'interview' WHERE run_id = 'run-promo-0'"
    )
    conn.execute(
        "UPDATE apply_runs SET status = 'offer' WHERE run_id = 'run-promo-1'"
    )

    conn.commit()
    conn.close()


def _seed_old(db_path: Path) -> None:
    """Seed postings with first_seen_at older than 7 days (for window tests)."""
    conn = open_pipeline_db(db_path)
    upsert_posting(
        conn,
        source="greenhouse/old",
        dedup_key="old-0",
        title="Old Role",
        company="Old Co",
        specialty="backend",
        url="https://old.com/1",
        jd_text="jd",
    )
    # Force first_seen_at to 60 days ago
    conn.execute(
        "UPDATE postings SET first_seen_at = ?, last_seen_at = ? WHERE dedup_key = 'old-0'",
        (_iso(60), _iso(60)),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "jobsmith.db"
    _seed(db_path)
    app = _make_app(db_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def empty_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "jobsmith.db"
    open_pipeline_db(db_path).close()  # just create tables
    app = _make_app(db_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/funnel
# ---------------------------------------------------------------------------


def test_funnel_empty_db_returns_zeros(empty_client) -> None:
    resp = empty_client.get("/api/funnel")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stages"]["sourced"] == 0
    assert data["stages"]["queued"] == 0
    assert data["stages"]["promoted"] == 0
    assert data["stages"]["interview"] == 0
    assert data["stages"]["offer"] == 0


def test_funnel_stage_counts(client) -> None:
    resp = client.get("/api/funnel")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    stages = data["stages"]
    # 3 sourced, 1 queued, 2 promoted (+ 1 interview, 1 offer from promoted)
    assert stages["sourced"] == 3
    assert stages["queued"] == 1
    # promoted = postings with status 'promoted'
    assert stages["promoted"] == 2
    # interview and offer come from apply_runs.status
    assert stages["interview"] == 1
    assert stages["offer"] == 1


def test_funnel_conversions_present(client) -> None:
    resp = client.get("/api/funnel")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    conv = data["conversions"]
    # Expected keys: sourced_to_queued, queued_to_promoted, promoted_to_interview, interview_to_offer
    assert "sourced_to_queued" in conv
    assert "queued_to_promoted" in conv
    assert "promoted_to_interview" in conv
    assert "interview_to_offer" in conv


def test_funnel_conversion_promoted_to_interview(client) -> None:
    resp = client.get("/api/funnel")
    data = resp.json()
    conv = data["conversions"]
    # 1 interview out of 2 promoted = 50%
    assert conv["promoted_to_interview"] == pytest.approx(0.5)


def test_funnel_conversion_interview_to_offer(client) -> None:
    resp = client.get("/api/funnel")
    data = resp.json()
    conv = data["conversions"]
    # 1 offer out of 1 interview = 100%
    assert conv["interview_to_offer"] == pytest.approx(1.0)


def test_funnel_per_source_table(client) -> None:
    resp = client.get("/api/funnel")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    sources = {row["source"]: row for row in data["per_source"]}
    # greenhouse/stripe should appear
    assert "greenhouse/stripe" in sources
    stripe = sources["greenhouse/stripe"]
    assert stripe["postings"] >= 1
    assert "applied" in stripe
    assert "interview" in stripe


def test_funnel_window_all_includes_old(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "jobsmith.db"
    _seed(db_path)
    _seed_old(db_path)
    app = _make_app(db_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as c:
        resp_all = c.get("/api/funnel?window=all")
        resp_7 = c.get("/api/funnel?window=7")
    assert resp_all.status_code == 200
    assert resp_7.status_code == 200
    all_sourced = resp_all.json()["stages"]["sourced"]
    week_sourced = resp_7.json()["stages"]["sourced"]
    # old posting (60 days ago) should appear in 'all' but not '7'
    assert all_sourced > week_sourced


def test_funnel_window_30_excludes_60day(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "jobsmith.db"
    _seed_old(db_path)
    app = _make_app(db_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as c:
        resp = c.get("/api/funnel?window=30")
    assert resp.status_code == 200
    # The only seeded posting is 60 days old, so it's outside the 30-day window
    assert resp.json()["stages"]["sourced"] == 0


def test_funnel_default_window_is_30(client) -> None:
    resp_default = client.get("/api/funnel")
    resp_30 = client.get("/api/funnel?window=30")
    assert resp_default.json()["stages"] == resp_30.json()["stages"]


def test_funnel_invalid_window_returns_422(client) -> None:
    resp = client.get("/api/funnel?window=99")
    assert resp.status_code == 422


def test_funnel_response_has_window_field(client) -> None:
    resp = client.get("/api/funnel?window=7")
    data = resp.json()
    assert data["window"] == 7
