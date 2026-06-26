"""Tests for jobsmith.sourcing.llm_rescore (feat-1602d64c / feat-a73173a1).

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
  - coverage_fields_present: LLM returns coverage → written to DB
  - coverage_fields_absent: LLM omits coverage → NULL + rationale marker
  - coverage_fields_malformed: non-int coverage → NULL + rationale marker
  - output_schema_accepts_coverage_fields: schema includes coverage + uncovered_must_haves
  - digest_injected_into_system_prompt: build_master_digest output appears in prompt
  - backtest_gitlab_senior_ai: low coverage, AI/LLM gaps
  - backtest_arcadia_lead_analytics: low coverage, DBT/healthcare gaps
  - mid_run_timeout_leaves_remaining_null: timeout => remaining postings stay NULL
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
    _OUTPUT_SCHEMA,
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


# ---------------------------------------------------------------------------
# Helpers for coverage tests
# ---------------------------------------------------------------------------


def _make_fake_query_with_coverage(
    *,
    score: int = 75,
    specialty: str = "tax_equity",
    rationale: str = "Reasonable fit.",
    evidence: list[str] | None = None,
    coverage: int | None = 40,
    uncovered_must_haves: list[str] | None = None,
    cost_per_call: float = 0.001,
):
    """Fake query returning coverage fields in structured_output."""
    async def _fake_query(prompt: str, options: Any) -> AsyncGenerator[Any, None]:
        class _FakeResult:
            subtype = "success"
            session_id = "session-cov"
            total_cost_usd = cost_per_call
            structured_output: dict = {
                "specialty": specialty,
                "score": score,
                "rationale": rationale,
                "matched_evidence": evidence or ["profile.stack.dagster"],
                "concerns": [],
                "confidence": "medium",
            }
            if coverage is not None:
                structured_output["coverage"] = coverage
            if uncovered_must_haves is not None:
                structured_output["uncovered_must_haves"] = uncovered_must_haves

        yield _FakeResult()

    return _fake_query


def _make_fake_query_with_coverage_v2(
    *,
    score: int = 75,
    specialty: str = "tax_equity",
    rationale: str = "Reasonable fit.",
    evidence: list[str] | None = None,
    coverage: Any = 40,
    uncovered_must_haves: list[str] | None = None,
    include_coverage: bool = True,
    include_uncovered: bool = True,
    cost_per_call: float = 0.001,
):
    """Flexible fake query for coverage field presence/absence/malformation tests."""
    async def _fake_query(prompt: str, options: Any) -> AsyncGenerator[Any, None]:
        output: dict = {
            "specialty": specialty,
            "score": score,
            "rationale": rationale,
            "matched_evidence": evidence or ["profile.stack.dagster"],
            "concerns": [],
            "confidence": "medium",
        }
        if include_coverage:
            output["coverage"] = coverage
        if include_uncovered:
            output["uncovered_must_haves"] = uncovered_must_haves or []

        class _FakeResult:
            subtype = "success"
            session_id = "session-cov"
            total_cost_usd = cost_per_call
            structured_output = output

        yield _FakeResult()

    return _fake_query


def _make_db_with_master_content(tmp_path: Path) -> Path:
    """Create a DB with some master_content rows for digest tests."""
    db_path = tmp_path / "jobsmith_cov.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    # Insert a work section with a representative bullet
    conn.execute(
        """
        INSERT INTO master_content (section, content_blob)
        VALUES ('work', ?)
        """,
        (
            "- title: Senior AI Engineer\n"
            "  location: Johns Hopkins\n"
            "  date: '2024'\n"
            "  details:\n"
            "    - bullet: LangGraph pipeline 99.3% recall on 6673 papers\n"
            "    - bullet: Multi-agent LLM RAG system deployed to production\n",
        ),
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# _OUTPUT_SCHEMA accepts coverage fields
# ---------------------------------------------------------------------------


def test_output_schema_has_coverage_field() -> None:
    """Schema properties must include 'coverage' with int 0-100 range."""
    props = _OUTPUT_SCHEMA["schema"]["properties"]
    assert "coverage" in props, "coverage not in _OUTPUT_SCHEMA properties"
    cov_schema = props["coverage"]
    assert cov_schema.get("type") == "integer"
    assert cov_schema.get("minimum") == 0
    assert cov_schema.get("maximum") == 100


def test_output_schema_has_uncovered_must_haves_field() -> None:
    """Schema properties must include 'uncovered_must_haves' as string array."""
    props = _OUTPUT_SCHEMA["schema"]["properties"]
    assert "uncovered_must_haves" in props, "uncovered_must_haves not in _OUTPUT_SCHEMA properties"
    unc_schema = props["uncovered_must_haves"]
    assert unc_schema.get("type") == "array"
    assert unc_schema["items"]["type"] == "string"


def test_output_schema_additionalproperties_false() -> None:
    """additionalProperties must remain False — malformed-output guard."""
    assert _OUTPUT_SCHEMA["schema"].get("additionalProperties") is False


# ---------------------------------------------------------------------------
# RescoreResult dataclass has coverage fields
# ---------------------------------------------------------------------------


def test_rescore_result_has_coverage_fields() -> None:
    """RescoreResult must carry coverage_score and uncovered_must_haves."""
    r = RescoreResult(
        posting_id=1,
        llm_score=0.42,
        specialty="ai_research",
        rationale="Some fit.",
        evidence_json="[]",
        is_fallback=False,
        cost_usd=0.001,
        coverage_score=35,
        uncovered_must_haves=["LangGraph", "RAG pipelines"],
    )
    assert r.coverage_score == 35
    assert r.uncovered_must_haves == ["LangGraph", "RAG pipelines"]


def test_rescore_result_coverage_defaults_to_none() -> None:
    """RescoreResult coverage fields default to None (backward compat)."""
    r = RescoreResult(
        posting_id=1,
        llm_score=0.5,
        specialty="none",
        rationale="fallback",
        evidence_json="[]",
        is_fallback=True,
        cost_usd=0.0,
    )
    assert r.coverage_score is None
    assert r.uncovered_must_haves is None


# ---------------------------------------------------------------------------
# update_posting_llm_score writes coverage columns
# ---------------------------------------------------------------------------


def test_update_posting_llm_score_writes_coverage_columns(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(conn, dedup_key="cov-upd-001")
        uncovered_json = json.dumps(["LangGraph deployment", "RAG pipelines"])

        update_posting_llm_score(
            conn,
            posting_id=pid,
            llm_score=0.45,
            specialty="ai_research",
            rationale="Partial AI fit.",
            evidence_json="[]",
            coverage_score=35,
            uncovered_json=uncovered_json,
        )

        row = conn.execute(
            "SELECT coverage_score, uncovered_json FROM postings WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row["coverage_score"] == 35
        assert "LangGraph" in row["uncovered_json"]
    finally:
        conn.close()


def test_update_posting_llm_score_writes_null_coverage(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(conn, dedup_key="cov-null-001")
        update_posting_llm_score(
            conn,
            posting_id=pid,
            llm_score=0.5,
            specialty="none",
            rationale="Fallback.",
            evidence_json="[]",
            coverage_score=None,
            uncovered_json=None,
        )

        row = conn.execute(
            "SELECT coverage_score, uncovered_json FROM postings WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row["coverage_score"] is None
        assert row["uncovered_json"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Coverage fields present → written to DB
# ---------------------------------------------------------------------------


def test_coverage_fields_present_written_to_db(db_path: Path) -> None:
    """When LLM returns valid coverage, it lands in coverage_score + uncovered_json."""
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(conn, dedup_key="cov-present-001", fast_score=0.6)

        query_fn = _make_fake_query_with_coverage_v2(
            score=55,
            specialty="ai_research",
            coverage=30,
            uncovered_must_haves=["LangGraph deployment", "clinical LLM experience"],
        )
        results = rescore_postings(
            conn,
            posting_ids=[pid],
            query_fn=query_fn,
            no_llm=False,
        )
        assert len(results) == 1
        assert results[0].coverage_score == 30
        assert results[0].uncovered_must_haves == ["LangGraph deployment", "clinical LLM experience"]

        row = conn.execute(
            "SELECT coverage_score, uncovered_json FROM postings WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row["coverage_score"] == 30
        uncovered = json.loads(row["uncovered_json"])
        assert "LangGraph deployment" in uncovered
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Coverage fields absent → NULL + rationale marker
# ---------------------------------------------------------------------------


def test_coverage_fields_absent_gives_null(db_path: Path) -> None:
    """When LLM omits coverage, coverage_score=NULL, rationale has marker."""
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(conn, dedup_key="cov-absent-001", fast_score=0.5)

        query_fn = _make_fake_query_with_coverage_v2(
            score=70,
            specialty="tax_equity",
            coverage=None,
            include_coverage=False,
            include_uncovered=False,
        )
        results = rescore_postings(
            conn,
            posting_ids=[pid],
            query_fn=query_fn,
            no_llm=False,
        )
        assert len(results) == 1
        assert results[0].coverage_score is None
        # Rationale should contain a coverage-unavailable marker
        assert "coverage-unavailable" in results[0].rationale

        row = conn.execute(
            "SELECT coverage_score, uncovered_json, rationale FROM postings WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row["coverage_score"] is None
        assert row["uncovered_json"] is None
        assert "coverage-unavailable" in row["rationale"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Coverage fields malformed → NULL + rationale marker
# ---------------------------------------------------------------------------


def test_coverage_fields_malformed_gives_null(db_path: Path) -> None:
    """When LLM returns non-int coverage, degrade to NULL + rationale marker."""
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(conn, dedup_key="cov-malformed-001", fast_score=0.5)

        # coverage is a string "high" instead of int
        query_fn = _make_fake_query_with_coverage_v2(
            score=70,
            specialty="tax_equity",
            coverage="high",  # malformed — not an int
        )
        results = rescore_postings(
            conn,
            posting_ids=[pid],
            query_fn=query_fn,
            no_llm=False,
        )
        assert len(results) == 1
        assert results[0].coverage_score is None
        assert "coverage-unavailable" in results[0].rationale

        row = conn.execute(
            "SELECT coverage_score, rationale FROM postings WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row["coverage_score"] is None
        assert "coverage-unavailable" in row["rationale"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Digest injection
# ---------------------------------------------------------------------------


def test_digest_injected_into_system_prompt(tmp_path: Path) -> None:
    """FIT_SCORER_SYSTEM_PROMPT must contain the master digest when DB has content."""
    from jobsmith.sourcing.llm_rescore import build_system_prompt_with_digest

    db_path = _make_db_with_master_content(tmp_path)
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        prompt = build_system_prompt_with_digest(conn)
        assert "LangGraph" in prompt
        assert "master bullet inventory" in prompt.lower() or "bullet inventory" in prompt.lower()
        # Specialty framing must still be present
        assert "tax_equity" in prompt
        assert "ai_research" in prompt
    finally:
        conn.close()


def test_digest_injection_empty_db(tmp_path: Path) -> None:
    """When DB has no master_content, prompt still works (empty marker path)."""
    from jobsmith.sourcing.llm_rescore import build_system_prompt_with_digest

    db_path = tmp_path / "empty.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        prompt = build_system_prompt_with_digest(conn)
        # Should still contain specialty framing
        assert "tax_equity" in prompt
        # Should contain empty marker from coverage.py
        assert "no master content" in prompt.lower()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backtest: GitLab Senior AI Engineer — low coverage, AI/LLM gaps
# ---------------------------------------------------------------------------


GITLAB_AI_JD = """\
GitLab is seeking a Senior AI Engineer to join our AI-powered DevSecOps platform team.
You will design and deploy production LLM inference services, build RAG pipelines
over code repositories, integrate OpenAI/Anthropic APIs, and fine-tune foundation models.
Requirements: 5+ years ML engineering, PyTorch, HuggingFace, LangChain or LangGraph,
experience shipping LLM-powered features at scale, familiarity with MLOps tooling
(MLflow, Kubeflow). Nice to have: clinical or healthcare AI background.
"""

ARCADIA_ANALYTICS_JD = """\
Arcadia is hiring a Lead Analytics Engineer to build our healthcare data platform.
You will design and maintain dbt models over large healthcare claims datasets,
build Snowflake pipelines, ensure CMS regulatory compliance, and mentor junior analysts.
Requirements: 4+ years analytics engineering, dbt Core expertise, Snowflake or BigQuery,
healthcare claims data (FHIR, HL7, EDI 837), payer/provider domain knowledge.
Nice to have: familiarity with value-based care metrics.
"""


def _make_backtest_query(
    *,
    coverage: int,
    uncovered: list[str],
    specialty: str = "ai_research",
    score: int = 45,
):
    """Return a deterministic fake query for backtest scenarios."""
    async def _fake_query(prompt: str, options: Any) -> AsyncGenerator[Any, None]:
        class _FakeResult:
            subtype = "success"
            session_id = "session-backtest"
            total_cost_usd = 0.001
            structured_output = {
                "specialty": specialty,
                "score": score,
                "rationale": "Low coverage: missing core AI/LLM stack.",
                "matched_evidence": ["profile.stack.langchain"],
                "concerns": ["missing LLM deployment experience"],
                "confidence": "medium",
                "coverage": coverage,
                "uncovered_must_haves": uncovered,
            }

        yield _FakeResult()

    return _fake_query


def test_backtest_gitlab_senior_ai_low_coverage(db_path: Path) -> None:
    """GitLab AI Engineer JD: LLM returns low coverage, gaps include AI/LLM bullets."""
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(
            conn,
            dedup_key="backtest-gitlab-ai",
            title="Senior AI Engineer",
            company="GitLab",
            fast_score=0.55,
            jd_text=GITLAB_AI_JD,
        )

        gitlab_uncovered = [
            "LLM inference service deployment",
            "RAG pipelines over code repositories",
            "PyTorch fine-tuning at scale",
        ]
        query_fn = _make_backtest_query(
            coverage=25,
            uncovered=gitlab_uncovered,
            specialty="ai_research",
            score=40,
        )
        results = rescore_postings(
            conn,
            posting_ids=[pid],
            query_fn=query_fn,
            no_llm=False,
        )

        assert len(results) == 1
        r = results[0]
        assert r.coverage_score is not None
        assert r.coverage_score <= 40, f"Expected low coverage, got {r.coverage_score}"
        assert r.uncovered_must_haves is not None
        assert len(r.uncovered_must_haves) > 0
        # At least one gap should reference LLM/AI domain
        gap_text = " ".join(r.uncovered_must_haves).lower()
        assert any(kw in gap_text for kw in ["llm", "rag", "pytorch", "inference"])

        row = conn.execute(
            "SELECT coverage_score, uncovered_json FROM postings WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row["coverage_score"] <= 40
        uncovered_list = json.loads(row["uncovered_json"])
        assert len(uncovered_list) > 0
    finally:
        conn.close()


def test_backtest_arcadia_lead_analytics_low_coverage(db_path: Path) -> None:
    """Arcadia Analytics JD: LLM returns low coverage, gaps include DBT/healthcare claims."""
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(
            conn,
            dedup_key="backtest-arcadia-analytics",
            title="Lead Analytics Engineer",
            company="Arcadia",
            fast_score=0.50,
            jd_text=ARCADIA_ANALYTICS_JD,
        )

        arcadia_uncovered = [
            "dbt Core expertise on claims data",
            "healthcare claims (FHIR, HL7, EDI 837)",
            "Snowflake or BigQuery analytics",
        ]
        query_fn = _make_backtest_query(
            coverage=20,
            uncovered=arcadia_uncovered,
            specialty="none",
            score=35,
        )
        results = rescore_postings(
            conn,
            posting_ids=[pid],
            query_fn=query_fn,
            no_llm=False,
        )

        assert len(results) == 1
        r = results[0]
        assert r.coverage_score is not None
        assert r.coverage_score <= 40, f"Expected low coverage, got {r.coverage_score}"
        gap_text = " ".join(r.uncovered_must_haves or []).lower()
        assert any(kw in gap_text for kw in ["dbt", "healthcare", "claims", "fhir", "snowflake"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mid-run timeout leaves remaining postings NULL
# ---------------------------------------------------------------------------


def test_mid_run_timeout_leaves_remaining_postings_null(db_path: Path) -> None:
    """If a mid-run timeout fires, remaining postings keep NULL coverage_score."""
    import asyncio

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        # Insert 3 postings
        pids = []
        for i in range(3):
            pid = _insert_posting(
                conn,
                dedup_key=f"timeout-mid-{i:03d}",
                fast_score=0.9 - float(i) * 0.1,
            )
            pids.append(pid)

        call_count = [0]

        async def _timeout_on_second(prompt: str, options: Any) -> AsyncGenerator[Any, None]:
            call_count[0] += 1
            if call_count[0] == 2:
                # Simulate timeout by raising asyncio.TimeoutError
                raise asyncio.TimeoutError()

            class _FakeResult:
                subtype = "success"
                session_id = "session-ok"
                total_cost_usd = 0.001
                structured_output = {
                    "specialty": "tax_equity",
                    "score": 70,
                    "rationale": "Good fit.",
                    "matched_evidence": ["profile.stack.dagster"],
                    "concerns": [],
                    "confidence": "high",
                    "coverage": 60,
                    "uncovered_must_haves": [],
                }

            yield _FakeResult()

        results = rescore_postings(
            conn,
            posting_ids=pids,
            query_fn=_timeout_on_second,
            no_llm=False,
            budget_usd=10.0,  # large budget so we don't stop early
        )

        # All 3 postings scored (timeout → fallback, not skipped)
        assert len(results) == 3

        # The timed-out one (index 1) should be fallback with NULL coverage
        fallbacks = [r for r in results if r.is_fallback]
        assert len(fallbacks) >= 1
        for fb in fallbacks:
            assert fb.coverage_score is None

        # First posting (successful) should have coverage set
        success_results = [r for r in results if not r.is_fallback]
        assert len(success_results) >= 1
        for sr in success_results:
            assert sr.coverage_score == 60
    finally:
        conn.close()


# ===========================================================================
# Slice 3 (feat-59d71698): pluggable Scorer Backend providers
# ===========================================================================

from jobsmith.config import LLMSettings  # noqa: E402
from jobsmith.sourcing.llm_rescore import (  # noqa: E402
    AntigravityScorer,
    BaseScorerBackend,
    ClaudeAgentScorer,
    OpenAICompatibleScorer,
    make_scorer,
)

# A well-formed fit-metrics JSON payload (what a cooperative server returns).
_WELL_FORMED = json.dumps(
    {
        "specialty": "tax_equity",
        "score": 82,
        "rationale": "Strong tax equity fit via ITC work.",
        "matched_evidence": ["profile.stack.dagster"],
        "concerns": [],
        "confidence": "high",
        "coverage": 40,
        "uncovered_must_haves": ["LangGraph"],
    }
)


def _fake_row(**overrides) -> dict:
    """A mapping that quacks like a sqlite3.Row for the scorer's row access."""
    base = {
        "id": 1,
        "fast_score": 0.6,
        "jd_text": "Tax equity solar finance ITC.",
        "company": "TestCo",
        "title": "Data Engineer",
        "location": "Remote",
        "url": "https://test.com/job",
    }
    base.update(overrides)
    return base


class _FakeCompatClient:
    """Stand-in for OpenAICompatClient — records calls, returns canned content."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    def complete(
        self,
        messages,
        *,
        response_format=None,
        temperature=None,
        extra=None,
    ) -> str:
        self.calls.append({"messages": messages, "response_format": response_format})
        return self._content


# ---------------------------------------------------------------------------
# Factory resolution
# ---------------------------------------------------------------------------


def test_base_scorer_backend_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseScorerBackend()  # type: ignore[abstract]


def test_make_scorer_default_claude() -> None:
    scorer = make_scorer(LLMSettings(), query_fn=_make_fake_query())
    assert isinstance(scorer, ClaudeAgentScorer)


def test_make_scorer_unknown_provider_falls_back_to_claude() -> None:
    # codex_cli has no dedicated scorer in slice 3 → default Claude path.
    scorer = make_scorer(LLMSettings(provider="codex_cli"), query_fn=_make_fake_query())
    assert isinstance(scorer, ClaudeAgentScorer)


def test_make_scorer_antigravity() -> None:
    scorer = make_scorer(LLMSettings(provider="antigravity_cli"))
    assert isinstance(scorer, AntigravityScorer)


def test_make_scorer_openai_compatible() -> None:
    scorer = make_scorer(
        LLMSettings(provider="openai_compatible", base_url="http://127.0.0.1:8080/v1", model="m")
    )
    assert isinstance(scorer, OpenAICompatibleScorer)


# ---------------------------------------------------------------------------
# OpenAICompatibleScorer — well-formed parse (MLX + Ollama share the path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:8080/v1", "http://localhost:11434/v1"],  # MLX, Ollama
)
def test_openai_scorer_parses_well_formed_json(base_url: str) -> None:
    client = _FakeCompatClient(_WELL_FORMED)
    scorer = OpenAICompatibleScorer(base_url=base_url, model="m", _client=client)

    r = scorer.score(_fake_row(), system_prompt="SYSTEM PROMPT")

    assert r.parse_ok is True
    assert r.is_fallback is False
    assert abs(r.llm_score - 0.82) < 0.01
    assert r.specialty == "tax_equity"
    assert "ITC" in r.rationale
    assert r.coverage_score == 40
    assert r.uncovered_must_haves == ["LangGraph"]

    # json_schema response_format is REQUESTED (preferred where supported)...
    assert client.calls[0]["response_format"]["type"] == "json_schema"
    # ...AND the schema is ALSO embedded in the prompt (servers may ignore it).
    system_content = client.calls[0]["messages"][0]["content"]
    assert "SYSTEM PROMPT" in system_content
    assert "score" in system_content


def test_openai_scorer_markdown_fenced_json_parses() -> None:
    fenced = "```json\n" + _WELL_FORMED + "\n```"
    scorer = OpenAICompatibleScorer(
        base_url="http://localhost:11434/v1", model="m", _client=_FakeCompatClient(fenced)
    )
    r = scorer.score(_fake_row(), system_prompt="SYS")
    assert r.parse_ok is True
    assert r.specialty == "tax_equity"


# ---------------------------------------------------------------------------
# OpenAICompatibleScorer — ROBUSTNESS (done_when #2): malformed output is
# caught and FLAGGED (parse_ok=False), the crawl must NOT crash.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_output",
    [
        "I'm sorry, I cannot comply with that request.",  # plain prose
        '{"foo": "bar"}',  # JSON but schema-violating (no score)
        '{"score": "high"}',  # score present but not an integer
        "here you go: not json at all {{{",  # broken JSON
        "",  # empty body
        '{"score": 80, "rationale":',  # truncated JSON
    ],
)
@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8080/v1", "http://localhost:11434/v1"])
def test_openai_scorer_malformed_output_is_flagged_not_raised(
    bad_output: str, base_url: str
) -> None:
    client = _FakeCompatClient(bad_output)
    scorer = OpenAICompatibleScorer(base_url=base_url, model="m", _client=client)

    # MUST NOT raise — the crawl keeps going.
    r = scorer.score(_fake_row(fast_score=0.6), system_prompt="SYS")

    assert r.parse_ok is False
    assert r.is_fallback is True
    assert r.llm_score == pytest.approx(0.6)  # degrades to fast_score
    assert r.coverage_score is None


def test_openai_scorer_network_error_falls_back_without_parse_flag() -> None:
    class _BoomClient:
        def complete(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("connection refused")

    scorer = OpenAICompatibleScorer(
        base_url="http://localhost:11434/v1", model="m", _client=_BoomClient()
    )
    r = scorer.score(_fake_row(), system_prompt="SYS")

    assert r.is_fallback is True
    # A transport failure is an availability fallback, not a parse failure.
    assert r.parse_ok is True
    assert r.llm_score == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# AntigravityScorer — subprocess path (injected run_fn, no real `agy`)
# ---------------------------------------------------------------------------


def test_antigravity_scorer_parses_well_formed() -> None:
    captured: dict = {}

    def _run(prompt: str, *, timeout_sec: float, project_root=None) -> str:
        captured["prompt"] = prompt
        return _WELL_FORMED

    scorer = AntigravityScorer(run_fn=_run)
    r = scorer.score(_fake_row(), system_prompt="SYS PROMPT")

    assert r.parse_ok is True
    assert r.specialty == "tax_equity"
    # Schema-in-prompt + system prompt rode along in the single-shot prompt.
    assert "SYS PROMPT" in captured["prompt"]
    assert "score" in captured["prompt"]


def test_antigravity_scorer_malformed_flagged() -> None:
    scorer = AntigravityScorer(run_fn=lambda prompt, **k: "garbage not json")
    r = scorer.score(_fake_row(), system_prompt="SYS")
    assert r.parse_ok is False
    assert r.is_fallback is True


def test_antigravity_scorer_subprocess_error_falls_back() -> None:
    def _boom(prompt: str, **k) -> str:
        raise RuntimeError("agy not found on PATH")

    scorer = AntigravityScorer(run_fn=_boom)
    r = scorer.score(_fake_row(), system_prompt="SYS")
    assert r.is_fallback is True


# ---------------------------------------------------------------------------
# ClaudeAgentScorer — wraps the existing SDK path unchanged
# ---------------------------------------------------------------------------


def test_claude_scorer_score_uses_query_fn() -> None:
    scorer = ClaudeAgentScorer(query_fn=_make_fake_query(score=80, specialty="tax_equity"))
    r = scorer.score(_fake_row(), system_prompt="SYS")
    assert r.parse_ok is True
    assert r.is_fallback is False
    assert abs(r.llm_score - 0.80) < 0.01
    assert r.specialty == "tax_equity"


def test_claude_scorer_sdk_error_falls_back() -> None:
    scorer = ClaudeAgentScorer(query_fn=_make_fake_query(raise_exc=RuntimeError("SDK offline")))
    r = scorer.score(_fake_row(), system_prompt="SYS")
    assert r.is_fallback is True


# ---------------------------------------------------------------------------
# rescore_postings honors an injected scorer (provider-agnostic loop)
# ---------------------------------------------------------------------------


def test_rescore_postings_uses_injected_scorer(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(conn, dedup_key="inject-001", fast_score=0.6)

        scorer = OpenAICompatibleScorer(
            base_url="http://localhost:11434/v1",
            model="m",
            _client=_FakeCompatClient(_WELL_FORMED),
        )
        results = rescore_postings(
            conn,
            posting_ids=[pid],
            scorer=scorer,
            no_llm=False,
        )
        assert len(results) == 1
        assert results[0].parse_ok is True
        row = conn.execute(
            "SELECT llm_score, specialty FROM postings WHERE id = ?", (pid,)
        ).fetchone()
        assert abs(row["llm_score"] - 0.82) < 0.01
        assert row["specialty"] == "tax_equity"
    finally:
        conn.close()


def test_rescore_postings_malformed_scorer_does_not_crash_crawl(db_path: Path) -> None:
    """End-to-end: a malformed-JSON server response flags the posting, no crash."""
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        pid = _insert_posting(conn, dedup_key="inject-bad-001", fast_score=0.55)

        scorer = OpenAICompatibleScorer(
            base_url="http://localhost:11434/v1",
            model="m",
            _client=_FakeCompatClient("totally not json"),
        )
        results = rescore_postings(conn, posting_ids=[pid], scorer=scorer, no_llm=False)

        assert len(results) == 1
        assert results[0].parse_ok is False
        assert results[0].is_fallback is True
        # Fallback score still written — posting not lost.
        row = conn.execute(
            "SELECT llm_score FROM postings WHERE id = ?", (pid,)
        ).fetchone()
        assert row["llm_score"] == pytest.approx(0.55)
    finally:
        conn.close()
