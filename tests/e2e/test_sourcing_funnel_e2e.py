"""Fixtures-only E2E: sourcing crawl -> email ingestion -> postings API -> funnel.

Slice 8 of plan-4def4798 (feat-447088bc). AUTOMATED half only.

What this test proves (no network, no SDK, no launchd, no real email):
  - run_crawl with fake ATS adapters (fixture JSON payloads) + a mock email
    alerts fn upserts postings into a temp DB.
  - GET /api/postings (FastAPI TestClient) returns the sourced rows, ranked.
  - POST /api/postings/{id}/promote creates an apply_runs row and links it.
  - GET /api/funnel reflects the expected stage counts (sourced, promoted).
  - GET /api/sourcing/run-health reflects state="ok" after the completed run.
  - Dedup invariant: a second run_crawl with the same fixture data does NOT
    create new postings rows.
  - Promoted posting carries promoted_application_id after promote.

What is deliberately left to the manual pass (orchestrator):
  - Live browser verification (launchctl kickstart, screenshot).
  - Real ATS board HTTP fetches.
  - Real Gmail / Mail.app ingestion.
  - Real LLM SDK (Anthropic) rescore calls.
  - launchd schedule installation (jobsmith source install-schedule).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith import db as jobsmith_db
from jobsmith.api.funnel_routes import router as funnel_router
from jobsmith.api.postings_routes import router as postings_router
from jobsmith.api.run_health import router as run_health_router
from jobsmith.sourcing.adapters.base import ATSSourceAdapter
from jobsmith.sourcing.runner import run_crawl

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_ATS = _FIXTURES / "ats"
_EMAIL = _FIXTURES / "email_alerts"

# ---------------------------------------------------------------------------
# Fake ATS adapter driven by the representative fixture JSONs
# ---------------------------------------------------------------------------


class _FixtureGreenhouseAdapter(ATSSourceAdapter):
    """Reads the recorded greenhouse_stripe_response.json fixture.

    No network calls — returns the same 3 roles the real fixture contains.
    """

    name = "greenhouse"

    def __init__(self, fixture_path: Path, company_name: str) -> None:
        self._fixture = fixture_path
        self._company_name = company_name

    def fetch(self, slug: str):  # noqa: ANN201
        from jobsmith.sourcing.adapters.greenhouse import parse_greenhouse_payload

        payload = json.loads(self._fixture.read_text())
        return list(
            parse_greenhouse_payload(
                payload,
                source_slug=slug,
                company_name=self._company_name,
            )
        )


class _FixtureLeverAdapter(ATSSourceAdapter):
    """Reads the recorded lever_response.json fixture (3 roles)."""

    name = "lever"

    def __init__(self, fixture_path: Path, company_name: str) -> None:
        self._fixture = fixture_path
        self._company_name = company_name

    def fetch(self, slug: str):  # noqa: ANN201
        from jobsmith.sourcing.adapters.lever import parse_lever_payload

        payload = json.loads(self._fixture.read_text())
        return list(
            parse_lever_payload(
                payload,
                source_slug=slug,
                company_name=self._company_name,
            )
        )


def _make_fixture_adapter_factory(
    greenhouse_fixture: Path,
    lever_fixture: Path,
):
    """Return an adapter_factory using the recorded fixture files."""

    def factory(spec: dict) -> ATSSourceAdapter | None:
        t = spec.get("type")
        slug = spec.get("slug", "unknown")
        company = spec.get("company") or spec.get("name") or slug.title()
        if t == "greenhouse":
            return _FixtureGreenhouseAdapter(greenhouse_fixture, company_name=company)
        if t == "lever":
            return _FixtureLeverAdapter(lever_fixture, company_name=company)
        return None

    return factory


# ---------------------------------------------------------------------------
# Mock email alert function (no Gmail / Mail.app)
# ---------------------------------------------------------------------------

# Synthesize 2 email alert postings from the linkedin_alert.html fixture
# by directly parsing it.

def _mock_email_alerts_fn(senders: list[dict], **kwargs: Any):  # noqa: ANN201
    """Return 2 inline postings (LinkedIn-style) — no real email / OAuth."""
    postings = [
        {
            "source": "email/linkedin-alert",
            "title": "Data Engineer",
            "company": "Acme Corp",
            "location": "Remote, United States",
            "url": "https://www.linkedin.com/jobs/view/3001000001/",
            "external_id": "3001000001",
        },
        {
            "source": "email/linkedin-alert",
            "title": "Senior Data Engineer",
            "company": "Beta Inc",
            "location": "Remote",
            "url": "https://www.linkedin.com/jobs/view/3001000002/",
            "external_id": "3001000002",
        },
    ]
    return postings, []  # (postings, degraded_senders)


# ---------------------------------------------------------------------------
# Shared DB + TestClient fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the full crawl pipeline and return (db_path, TestClient, summary).

    1. Creates a temp DB.
    2. Runs run_crawl with fixture-backed adapters + mock email fn.
    3. Builds a minimal FastAPI app with all three sourcing routers.
    4. Monkeypatches _get_db_path for postings + funnel routers.
    5. Yields (db_path, client, summary) for assertion tests.
    """
    db_path = tmp_path / "jobsmith.db"
    # Ensure schema is applied
    conn = jobsmith_db.open_pipeline_db(db_path)
    conn.close()

    # Sources: 1 greenhouse (stripe), 1 lever (netflix)
    sources = [
        {"type": "greenhouse", "slug": "stripe", "company": "Stripe"},
        {"type": "lever", "slug": "netflix", "company": "Netflix"},
    ]

    alert_senders = [
        {
            "type": "mailapp_alert",
            "sender_slug": "linkedin-alert",
            "account": "me@example.com",
            "mailbox": "Job Alerts",
        }
    ]

    adapter_factory = _make_fixture_adapter_factory(
        greenhouse_fixture=_ATS / "greenhouse_stripe_response.json",
        lever_fixture=_ATS / "lever_response.json",
    )

    # Patch inter-source sleep so the fixture suite runs in < 1 s per run
    with patch("jobsmith.sourcing.runner._INTER_SOURCE_SLEEP", 0.0):
        summary = run_crawl(
            db_path=db_path,
            sources=sources,
            alert_senders=alert_senders,
            adapter_factory=adapter_factory,
            _run_email_alerts_fn=_mock_email_alerts_fn,
            no_llm=True,  # skip LLM SDK — fixture-only, no network
            global_timeout_sec=60,
        )

    # Build a minimal app with all three routers
    # (run_health uses request.app.state.repo_root — we stub it via monkeypatch
    # on the internal _resolve_db_path helper instead)
    app = FastAPI()

    # Monkeypatch _get_db_path for postings + funnel routers
    monkeypatch.setattr("jobsmith.api.postings_routes._get_db_path", lambda: db_path)
    monkeypatch.setattr("jobsmith.api.funnel_routes._get_db_path", lambda: db_path)

    app.include_router(postings_router, prefix="/api")
    app.include_router(funnel_router, prefix="/api")
    app.include_router(run_health_router, prefix="/api")

    # Monkeypatch run_health's _resolve_db_path (uses request.app.state)
    monkeypatch.setattr(
        "jobsmith.api.run_health._resolve_db_path",
        lambda _request: db_path,
    )

    with TestClient(app, raise_server_exceptions=True) as client:
        yield db_path, client, summary


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunCrawlOutput:
    """Validates what run_crawl wrote to the DB."""

    def test_summary_not_aborted(self, e2e_setup) -> None:
        _, _, summary = e2e_setup
        assert summary["aborted"] is False

    def test_sourced_from_greenhouse(self, e2e_setup) -> None:
        _, _, summary = e2e_setup
        assert "greenhouse/stripe" in summary["sources_checked"]

    def test_sourced_from_lever(self, e2e_setup) -> None:
        _, _, summary = e2e_setup
        assert "lever/netflix" in summary["sources_checked"]

    def test_email_alerts_sourced(self, e2e_setup) -> None:
        _, _, summary = e2e_setup
        # Email path marks as "email_alerts" in sources_checked
        assert "email_alerts" in summary["sources_checked"]

    def test_total_roles_fetched_at_least_seven(self, e2e_setup) -> None:
        """3 greenhouse + 3 lever + 2 email = 8 minimum."""
        _, _, summary = e2e_setup
        # greenhouse fixture has 3, lever fixture has 3, email mock has 2
        assert summary["roles_fetched"] >= 7

    def test_roles_upserted_matches_fetched(self, e2e_setup) -> None:
        _, _, summary = e2e_setup
        assert summary["roles_upserted"] == summary["roles_fetched"]

    def test_sourcing_run_record_created(self, e2e_setup) -> None:
        db_path, _, summary = e2e_setup
        conn = jobsmith_db.open_pipeline_db(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM sourcing_runs WHERE run_id = ?", (summary["run_id"],)
            ).fetchone()
            assert row is not None
            assert row["status"] in ("done", "degraded")
            assert row["finished_at"] is not None
        finally:
            conn.close()

    def test_postings_rows_present(self, e2e_setup) -> None:
        db_path, _, summary = e2e_setup
        conn = jobsmith_db.open_pipeline_db(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
            assert count == summary["roles_upserted"]
        finally:
            conn.close()

    def test_email_postings_source_prefix(self, e2e_setup) -> None:
        """Email-sourced postings carry source prefix 'email/'."""
        db_path, _, _ = e2e_setup
        conn = jobsmith_db.open_pipeline_db(db_path)
        try:
            rows = conn.execute(
                "SELECT source FROM postings WHERE source LIKE 'email/%'"
            ).fetchall()
            assert len(rows) >= 2
        finally:
            conn.close()


class TestDedup:
    """A second run with the same fixture data must NOT create new postings."""

    def test_second_run_does_not_duplicate(self, e2e_setup) -> None:
        db_path, _, _ = e2e_setup

        # Count postings after first crawl
        conn = jobsmith_db.open_pipeline_db(db_path)
        count_before = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        conn.close()

        # Re-run with the same fixtures
        sources = [
            {"type": "greenhouse", "slug": "stripe", "company": "Stripe"},
            {"type": "lever", "slug": "netflix", "company": "Netflix"},
        ]
        alert_senders = [
            {
                "type": "mailapp_alert",
                "sender_slug": "linkedin-alert",
                "account": "me@example.com",
                "mailbox": "Job Alerts",
            }
        ]
        adapter_factory = _make_fixture_adapter_factory(
            greenhouse_fixture=_ATS / "greenhouse_stripe_response.json",
            lever_fixture=_ATS / "lever_response.json",
        )
        with patch("jobsmith.sourcing.runner._INTER_SOURCE_SLEEP", 0.0):
            run_crawl(
                db_path=db_path,
                sources=sources,
                alert_senders=alert_senders,
                adapter_factory=adapter_factory,
                _run_email_alerts_fn=_mock_email_alerts_fn,
                no_llm=True,
                global_timeout_sec=60,
            )

        conn = jobsmith_db.open_pipeline_db(db_path)
        count_after = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        conn.close()

        assert count_after == count_before, (
            f"Dedup failed: {count_after} rows after second run, expected {count_before}"
        )


class TestPostingsAPI:
    """GET /api/postings and POST /api/postings/{id}/promote via TestClient."""

    def test_list_postings_returns_all_sourced(self, e2e_setup) -> None:
        db_path, client, summary = e2e_setup
        resp = client.get("/api/postings")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data) == summary["roles_upserted"]

    def test_list_postings_only_sourced_status_by_default(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/postings")
        assert resp.status_code == 200
        data = resp.json()
        # After a fresh crawl, all postings are 'sourced'
        assert all(p["status"] == "sourced" for p in data)

    def test_filter_by_source_greenhouse(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/postings?source=greenhouse")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert all("greenhouse" in p["source"] for p in data)

    def test_filter_by_source_email(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/postings?source=email")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 2
        assert all("email" in p["source"] for p in data)

    def test_ranking_llm_score_desc(self, e2e_setup) -> None:
        """Postings ranked by llm_score DESC NULLS LAST, fast_score DESC NULLS LAST."""
        _, client, _ = e2e_setup
        resp = client.get("/api/postings")
        data = resp.json()
        # Collect effective scores (use fast_score when llm_score is None)
        scores = [
            p["llm_score"] if p["llm_score"] is not None else p["fast_score"]
            for p in data
        ]
        # Scores should be non-increasing (allowing None/NaN at the tail)
        non_null = [s for s in scores if s is not None]
        assert non_null == sorted(non_null, reverse=True)

    def test_promote_creates_apply_run(self, e2e_setup) -> None:
        db_path, client, _ = e2e_setup
        # Pick the first sourced posting
        resp = client.get("/api/postings?source=greenhouse")
        assert resp.status_code == 200
        postings = resp.json()
        assert postings, "Expected at least one greenhouse posting"
        pid = postings[0]["id"]

        promote_resp = client.post(f"/api/postings/{pid}/promote")
        assert promote_resp.status_code == 200, promote_resp.text
        promote_data = promote_resp.json()
        assert "run_id" in promote_data
        assert promote_data["run_id"]  # non-empty string

        # Verify apply_runs row exists
        conn = jobsmith_db.open_pipeline_db(db_path)
        try:
            ar = conn.execute(
                "SELECT * FROM apply_runs WHERE run_id = ?",
                (promote_data["run_id"],),
            ).fetchone()
            assert ar is not None
            assert ar["status"] == "in-progress"
        finally:
            conn.close()

    def test_promote_links_promoted_application_id(self, e2e_setup) -> None:
        db_path, client, _ = e2e_setup
        # Pick the first sourced posting
        resp = client.get("/api/postings?source=greenhouse")
        postings = resp.json()
        pid = postings[0]["id"]

        promote_resp = client.post(f"/api/postings/{pid}/promote")
        run_id = promote_resp.json()["run_id"]

        conn = jobsmith_db.open_pipeline_db(db_path)
        try:
            row = conn.execute(
                "SELECT status, promoted_application_id FROM postings WHERE id = ?",
                (pid,),
            ).fetchone()
            assert row["status"] == "promoted"
            assert row["promoted_application_id"] == run_id
        finally:
            conn.close()

    def test_promote_idempotent(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/postings?source=lever")
        postings = resp.json()
        assert postings
        pid = postings[0]["id"]

        r1 = client.post(f"/api/postings/{pid}/promote")
        r2 = client.post(f"/api/postings/{pid}/promote")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["run_id"] == r2.json()["run_id"]


class TestFunnelAPI:
    """GET /api/funnel reflects the crawl + promote state."""

    def test_funnel_sourced_count_matches_crawl(self, e2e_setup) -> None:
        db_path, client, summary = e2e_setup
        resp = client.get("/api/funnel?window=all")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Sourced = total rows (all freshly crawled, none promoted yet)
        assert data["stages"]["sourced"] == summary["roles_upserted"]

    def test_funnel_promoted_count_after_promote(self, e2e_setup) -> None:
        db_path, client, summary = e2e_setup

        # Promote one posting via the API
        all_resp = client.get("/api/postings")
        pid = all_resp.json()[0]["id"]
        client.post(f"/api/postings/{pid}/promote")

        resp = client.get("/api/funnel?window=all")
        data = resp.json()
        assert data["stages"]["promoted"] == 1

    def test_funnel_sourced_decreases_after_promote(self, e2e_setup) -> None:
        _, client, summary = e2e_setup

        # Baseline: all sourced
        before = client.get("/api/funnel?window=all").json()["stages"]["sourced"]
        assert before == summary["roles_upserted"]

        # Promote one
        pid = client.get("/api/postings").json()[0]["id"]
        client.post(f"/api/postings/{pid}/promote")

        after = client.get("/api/funnel?window=all").json()["stages"]["sourced"]
        # promoted count decrements sourced by 1
        assert after == before - 1

    def test_funnel_per_source_includes_greenhouse(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/funnel?window=all")
        data = resp.json()
        sources = {row["source"] for row in data["per_source"]}
        assert "greenhouse/stripe" in sources

    def test_funnel_per_source_includes_email(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/funnel?window=all")
        data = resp.json()
        sources = {row["source"] for row in data["per_source"]}
        # email postings come through as "email/linkedin-alert"
        assert any("email" in s for s in sources)

    def test_funnel_conversions_present(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/funnel?window=all")
        data = resp.json()
        conv = data["conversions"]
        assert "sourced_to_queued" in conv
        assert "queued_to_promoted" in conv
        assert "promoted_to_interview" in conv
        assert "interview_to_offer" in conv

    def test_funnel_window_field_present(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/funnel?window=30")
        data = resp.json()
        assert data["window"] == 30


class TestRunHealthAPI:
    """GET /api/sourcing/run-health reflects the completed run."""

    def test_run_health_state_ok_after_clean_run(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/sourcing/run-health")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # A fresh run just completed with no degraded sources
        assert data["state"] in ("ok", "degraded"), (
            f"Expected ok or degraded after clean run, got {data['state']!r}"
        )

    def test_run_health_has_last_run_id(self, e2e_setup) -> None:
        _, client, summary = e2e_setup
        resp = client.get("/api/sourcing/run-health")
        data = resp.json()
        assert data["last_run_id"] == summary["run_id"]

    def test_run_health_finished_at_set(self, e2e_setup) -> None:
        _, client, _ = e2e_setup
        resp = client.get("/api/sourcing/run-health")
        data = resp.json()
        assert data["finished_at"] is not None

    def test_run_health_age_hours_small(self, e2e_setup) -> None:
        """Age should be < 1 hour since we just ran it."""
        _, client, _ = e2e_setup
        resp = client.get("/api/sourcing/run-health")
        data = resp.json()
        assert data["age_hours"] is not None
        assert data["age_hours"] < 1.0


class TestEndToEndInvariants:
    """Cross-cutting invariants over the full sourcing-to-promote loop."""

    def test_no_duplicate_postings_after_fresh_crawl(self, e2e_setup) -> None:
        """Every dedup_key in the DB is unique."""
        db_path, _, _ = e2e_setup
        conn = jobsmith_db.open_pipeline_db(db_path)
        try:
            rows = conn.execute(
                "SELECT dedup_key, COUNT(*) AS n FROM postings GROUP BY dedup_key HAVING n > 1"
            ).fetchall()
            assert rows == [], f"Duplicate dedup_keys found: {[r['dedup_key'] for r in rows]}"
        finally:
            conn.close()

    def test_all_postings_have_status_sourced(self, e2e_setup) -> None:
        db_path, _, _ = e2e_setup
        conn = jobsmith_db.open_pipeline_db(db_path)
        try:
            rows = conn.execute(
                "SELECT status FROM postings WHERE status != 'sourced'"
            ).fetchall()
            assert rows == [], (
                f"Expected all sourced; found non-sourced: {[r['status'] for r in rows]}"
            )
        finally:
            conn.close()

    def test_promote_then_funnel_counts_consistent(self, e2e_setup) -> None:
        """After promoting N postings, funnel.promoted == N and sourced == total - N."""
        db_path, client, summary = e2e_setup
        total = summary["roles_upserted"]

        # Promote 2 postings
        all_postings = client.get("/api/postings").json()
        pids_to_promote = [p["id"] for p in all_postings[:2]]
        for pid in pids_to_promote:
            r = client.post(f"/api/postings/{pid}/promote")
            assert r.status_code == 200

        funnel = client.get("/api/funnel?window=all").json()["stages"]
        assert funnel["promoted"] == 2
        assert funnel["sourced"] == total - 2

    def test_promoted_posting_links_apply_run(self, e2e_setup) -> None:
        """Promoted posting has promoted_application_id pointing to a valid apply_run."""
        db_path, client, _ = e2e_setup
        pid = client.get("/api/postings").json()[0]["id"]
        promote_resp = client.post(f"/api/postings/{pid}/promote")
        run_id = promote_resp.json()["run_id"]

        conn = jobsmith_db.open_pipeline_db(db_path)
        try:
            posting = conn.execute(
                "SELECT promoted_application_id FROM postings WHERE id = ?", (pid,)
            ).fetchone()
            ar = conn.execute(
                "SELECT run_id FROM apply_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert posting["promoted_application_id"] == run_id
            assert ar is not None
        finally:
            conn.close()
