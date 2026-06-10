"""Tests for jobsmith.sourcing.llm_rescore (feat-1602d64c).

TDD: tests written before implementation.

Covers:
  - budget_cap: when total_cost_usd exceeds budget, stop issuing LLM calls
  - n_cap: only top-N postings by fast_score are rescored
  - fallback_on_failure: SDK errors fall back to fast_score with rationale marker
  - no_llm_skip: when no_llm=True, rescore_postings is a no-op
  - scores_land_on_rows: after rescore, postings rows carry llm_score/specialty/
    rationale/evidence_json
  - run_crawl_no_llm: run_crawl with no_llm=True skips rescoring entirely
  - run_crawl_with_llm: run_crawl without no_llm wires LLM rescore on new postings
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from jobsmith import db as jobsmith_db
from jobsmith.sourcing.adapters.base import Role
from jobsmith.sourcing.llm_rescore import (
    RescoreResult,
    rescore_postings,
    update_posting_llm_score,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Create and return a pipeline DB path with schema applied."""
    path = tmp_path / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(path)
    conn.close()
    return path


def _insert_posting(
    conn,
    *,
    dedup_key: str,
    title: str = "Data Engineer",
    company: str = "TestCo",
    fast_score: float = 0.5,
    jd_text: str = "Build data pipelines.",
) -> int:
    """Insert a posting row directly and return its id."""
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO postings
            (source, url, title, company, location, status, dedup_key,
             fast_score, jd_text, first_seen_at, last_seen_at)
        VALUES
            ('greenhouse/test', 'https://test.com/job', ?, ?, 'Remote',
             'sourced', ?, ?, ?, ?, ?)
        """,
        (title, company, dedup_key, fast_score, jd_text, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM postings WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return int(row["id"])


# ---------------------------------------------------------------------------
# Fake SDK query function
# ---------------------------------------------------------------------------


def _make_fake_query(
    *,
    score: int = 75,
    specialty: str = "tax_equity",
    rationale: str = "Strong tax equity fit.",
    evidence: list[str] | None = None,
    raise_exc: Exception | None = None,
    cost_per_call: float = 0.001,
):
    """Return a fake async generator query function for testing.

    If raise_exc is provided, it is raised instead of yielding a result.
    """
    from claude_agent_sdk import ResultMessage  # type: ignore[import]  # noqa: F401

    call_count = [0]
    total_cost = [0.0]

    async def _fake_query(
        prompt: str, options: Any
    ) -> AsyncGenerator[Any, None]:
        if raise_exc is not None:
            raise raise_exc

        call_count[0] += 1
        cost = cost_per_call
        total_cost[0] += cost

        # Build a fake ResultMessage-like object
        class _FakeResult:
            subtype = "success"
            session_id = f"session-{call_count[0]:03d}"
            total_cost_usd = cost
            structured_output = {
                "specialty": specialty,
                "score": score,
                "rationale": rationale,
                "matched_evidence": evidence or ["profile.stack.dagster"],
                "concerns": [],
                "confidence": "high",
            }

        yield _FakeResult()

    return _fake_query


# ---------------------------------------------------------------------------
# RescoreResult — data class smoke test
# ---------------------------------------------------------------------------


def test_rescore_result_is_fallback_when_marked() -> None:
    r = RescoreResult(
        posting_id=1,
        llm_score=0.42,
        specialty="tax_equity",
        rationale="(LLM unavailable: timeout) — fast-path fallback",
        evidence_json="[]",
        is_fallback=True,
        cost_usd=0.0,
    )
    assert r.is_fallback is True
    assert "fallback" in r.rationale


# ---------------------------------------------------------------------------
# update_posting_llm_score
# ---------------------------------------------------------------------------


def test_update_posting_llm_score_writes_columns(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(conn, dedup_key="upd-001")
        evidence = json.dumps(["profile.stack.dagster", "profile.domains.itc"])

        update_posting_llm_score(
            conn,
            posting_id=pid,
            llm_score=0.78,
            specialty="tax_equity",
            rationale="Clear tax equity fit via ITC.",
            evidence_json=evidence,
        )

        row = conn.execute(
            "SELECT llm_score, specialty, rationale, evidence_json FROM postings WHERE id = ?",
            (pid,),
        ).fetchone()
        assert abs(row["llm_score"] - 0.78) < 0.0001
        assert row["specialty"] == "tax_equity"
        assert "ITC" in row["rationale"]
        assert "dagster" in row["evidence_json"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rescore_postings — no_llm skip
# ---------------------------------------------------------------------------


def test_rescore_postings_no_llm_is_noop(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(conn, dedup_key="nollm-001", fast_score=0.8)

        # Should do nothing — no query_fn should be called
        called = [False]

        async def _spy_query(prompt: str, options: Any) -> AsyncGenerator[Any, None]:
            called[0] = True
            return
            yield  # make it a generator

        results = rescore_postings(
            conn,
            posting_ids=[pid],
            no_llm=True,
            query_fn=_spy_query,
        )

        assert results == []
        assert called[0] is False

        # Row should be unchanged
        row = conn.execute(
            "SELECT llm_score FROM postings WHERE id = ?", (pid,)
        ).fetchone()
        assert row["llm_score"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rescore_postings — N cap
# ---------------------------------------------------------------------------


def test_rescore_postings_n_cap_limits_calls(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        # Insert 5 postings with varying fast_scores
        pids = []
        for i in range(5):
            pid = _insert_posting(
                conn,
                dedup_key=f"ncap-{i:03d}",
                fast_score=float(i) * 0.1 + 0.1,  # 0.1, 0.2, ..., 0.5
            )
            pids.append(pid)

        query_fn = _make_fake_query(score=70)
        # Only rescore top-2 by fast_score (n_cap=2)
        results = rescore_postings(
            conn,
            posting_ids=pids,
            n_cap=2,
            query_fn=query_fn,
            no_llm=False,
        )

        # Only 2 results should be returned
        assert len(results) == 2
        # All 2 should be rescored (not fallback)
        assert all(not r.is_fallback for r in results)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rescore_postings — budget cap
# ---------------------------------------------------------------------------


def test_rescore_postings_budget_cap_stops_when_exceeded(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        # Insert 5 postings
        pids = []
        for i in range(5):
            pid = _insert_posting(
                conn,
                dedup_key=f"budget-{i:03d}",
                fast_score=0.9 - float(i) * 0.1,
            )
            pids.append(pid)

        # Budget cap = $0.003, cost per call = $0.002 → should stop after 1 call
        # (first call costs $0.002, second would exceed $0.003 total)
        query_fn = _make_fake_query(score=70, cost_per_call=0.002)
        results = rescore_postings(
            conn,
            posting_ids=pids,
            n_cap=5,
            budget_usd=0.003,
            query_fn=query_fn,
            no_llm=False,
        )

        # Only 1 call should complete before budget is exceeded
        rescored = [r for r in results if not r.is_fallback]
        assert len(rescored) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rescore_postings — fallback on failure
# ---------------------------------------------------------------------------


def test_rescore_postings_fallback_on_sdk_exception(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(
            conn,
            dedup_key="fail-001",
            fast_score=0.7,
            jd_text="Tax equity solar finance ITC.",
        )

        query_fn = _make_fake_query(raise_exc=RuntimeError("SDK offline"))
        results = rescore_postings(
            conn,
            posting_ids=[pid],
            query_fn=query_fn,
            no_llm=False,
        )

        assert len(results) == 1
        result = results[0]
        assert result.is_fallback is True
        assert "fallback" in result.rationale.lower() or "unavailable" in result.rationale.lower()

        # Fallback score should be written to row
        row = conn.execute(
            "SELECT llm_score, rationale FROM postings WHERE id = ?", (pid,)
        ).fetchone()
        assert row["llm_score"] is not None
        assert row["rationale"] is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rescore_postings — scores land on rows
# ---------------------------------------------------------------------------


def test_rescore_postings_writes_all_columns(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(
            conn,
            dedup_key="land-001",
            fast_score=0.6,
            jd_text="Tax equity solar finance ITC Dagster.",
        )

        query_fn = _make_fake_query(
            score=82,
            specialty="tax_equity",
            rationale="Strong tax equity fit via ITC work.",
            evidence=["profile.stack.dagster", "profile.domains.itc_tax_credits"],
        )
        results = rescore_postings(
            conn,
            posting_ids=[pid],
            query_fn=query_fn,
            no_llm=False,
        )

        assert len(results) == 1
        assert not results[0].is_fallback

        row = conn.execute(
            "SELECT llm_score, specialty, rationale, evidence_json FROM postings WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row["llm_score"] is not None
        assert abs(row["llm_score"] - 0.82) < 0.01
        assert row["specialty"] == "tax_equity"
        assert row["rationale"] is not None
        evidence = json.loads(row["evidence_json"] or "[]")
        assert isinstance(evidence, list)
        assert len(evidence) > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run_crawl integration — no_llm skips rescoring
# ---------------------------------------------------------------------------


def test_run_crawl_no_llm_skips_rescore(db_path: Path) -> None:
    from jobsmith.sourcing.runner import run_crawl

    def _factory(spec: dict):
        from collections.abc import Iterable

        from jobsmith.sourcing.adapters.base import ATSSourceAdapter

        class _FakeAdapter(ATSSourceAdapter):
            name = "fake"

            def fetch(self, slug: str) -> Iterable[Role]:
                return iter([
                    Role(
                        id="fake:001",
                        source="greenhouse",
                        source_slug="testco",
                        company="TestCo",
                        title="Data Engineer",
                        location="Remote",
                        url="https://testco.com/jobs/1",
                        jd_text="Tax equity ITC solar.",
                        posted_date="2026-06-01",
                    )
                ])

        return _FakeAdapter()

    called = [False]

    async def _spy(prompt: str, options: Any) -> AsyncGenerator[Any, None]:
        called[0] = True
        return
        yield

    sources = [{"type": "greenhouse", "slug": "testco"}]
    run_crawl(db_path, sources, adapter_factory=_factory, no_llm=True, _query_fn=_spy)

    assert called[0] is False

    # Posting should exist but llm_score should be NULL
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        row = conn.execute("SELECT llm_score FROM postings").fetchone()
        assert row is not None
        assert row["llm_score"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run_crawl integration — LLM rescore fires on new postings
# ---------------------------------------------------------------------------


def test_run_crawl_with_llm_rescores_new_postings(db_path: Path) -> None:
    from jobsmith.sourcing.runner import run_crawl

    def _factory(spec: dict):
        from collections.abc import Iterable

        from jobsmith.sourcing.adapters.base import ATSSourceAdapter

        class _FakeAdapter(ATSSourceAdapter):
            name = "fake"

            def fetch(self, slug: str) -> Iterable[Role]:
                return iter([
                    Role(
                        id="fake:001",
                        source="greenhouse",
                        source_slug="testco",
                        company="TestCo",
                        title="Data Engineer",
                        location="Remote",
                        url="https://testco.com/jobs/1",
                        jd_text="Tax equity ITC solar Dagster.",
                        posted_date="2026-06-01",
                    )
                ])

        return _FakeAdapter()

    query_fn = _make_fake_query(score=80, specialty="tax_equity", rationale="Good ITC fit.")
    sources = [{"type": "greenhouse", "slug": "testco"}]
    run_crawl(
        db_path,
        sources,
        adapter_factory=_factory,
        no_llm=False,
        _query_fn=query_fn,
    )

    # Posting should have llm_score set
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        row = conn.execute("SELECT llm_score, specialty, rationale FROM postings").fetchone()
        assert row is not None
        assert row["llm_score"] is not None
        assert abs(row["llm_score"] - 0.80) < 0.01
        assert row["specialty"] == "tax_equity"
    finally:
        conn.close()
