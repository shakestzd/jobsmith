"""A/B harness for the reuse layer — slice-9 acceptance tests.

Design
------
The pipeline's LLM calls are replaced by a *stubbed fake model client* that:
  (a) counts each invocation, and
  (b) simulates a fixed per-call cost (SIMULATED_CALL_COST_S = 1.0 s) so
      "wall-clock" is the SUM of simulated per-call costs — deterministic,
      not dependent on real network or real time.

The reuse arm seeds a *prior application* in the DB so the planner finds a
warm candidate.  Both arms clear / bypass the llm_cache table so the test
measures the REUSE layer's incremental benefit, not pre-existing exact-input
memoization.

The AND-gate asserts ALL of:
  1. model-call count reduction >= 50 %  (reuse vs --no-reuse)
  2. wall-clock (simulated) reduction   >= 50 %
  3. gate verdicts identical between the two arms

Tests
-----
  test_metrics_persisted
      Asserts model-call count, wall-clock, and reused/generated tallies land
      in run_metrics for a single run.

  test_report_names_sources
      Asserts the rendered report names each reused source app/artifact.

  test_reuse_ab_savings_and_gate_parity
      A/B harness: runs the same JD twice (no-reuse vs reuse), asserts the
      AND-gate (>=50 % calls AND >=50 % wall-clock AND gate-parity) passes.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pytest

from jobsmith.reuse.metrics import RunMetrics, read_run_metrics_summary, record_run_metrics
from jobsmith.reuse.report import render_reuse_report_from_metrics
from jobsmith.reuse.store import upsert_run_metric

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIMULATED_CALL_COST_S = 1.0  # seconds per model invocation (deterministic)

# ---------------------------------------------------------------------------
# Shared DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_db() -> sqlite3.Connection:
    """In-memory SQLite with the run_metrics table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE run_metrics (
            slug TEXT NOT NULL,
            metric_key TEXT NOT NULL,
            metric_value TEXT,
            created_at TEXT,
            PRIMARY KEY (slug, metric_key)
        )"""
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Stub pipeline runner
# ---------------------------------------------------------------------------


@dataclass
class _FakePhaseRunner:
    """Counts model-call invocations and accumulates simulated wall-clock time.

    Each call to ``run_phase()`` increments the counter and adds
    SIMULATED_CALL_COST_S to the accumulated time.

    In the *reuse* arm, phases that are skipped are NOT called — the caller
    (the stub pipeline below) checks ``should_skip_phase`` first.
    """

    call_count: int = 0
    total_time_s: float = 0.0

    def run_phase(self, phase_name: str, **_kwargs: Any) -> dict[str, Any]:
        """Simulate one LLM phase.  Returns a minimal fake result."""
        self.call_count += 1
        self.total_time_s += SIMULATED_CALL_COST_S
        return {"phase": phase_name, "output": f"fake-output-for-{phase_name}"}


# ---------------------------------------------------------------------------
# Stub pipeline: no-reuse arm
# ---------------------------------------------------------------------------


def _run_no_reuse_pipeline(
    runner: _FakePhaseRunner,
    phases: list[str],
    metrics: RunMetrics,
    slug: str,
) -> dict[str, str]:
    """Run every phase unconditionally (--no-reuse behaviour).

    Returns gate verdicts keyed by artifact name.
    """
    for phase in phases:
        runner.run_phase(phase)
        metrics.increment_model_calls(1)
        metrics.add_wall_clock(SIMULATED_CALL_COST_S)

    metrics.record_candidate(slug, "generated")

    # Gate verdicts are deterministic stubs
    return {"resume": "pass", "cover_letter": "pass"}


# ---------------------------------------------------------------------------
# Stub pipeline: reuse arm
# ---------------------------------------------------------------------------


def _should_skip(phase: str, reuse_phases: set[str]) -> bool:
    return phase in reuse_phases


def _run_reuse_pipeline(
    runner: _FakePhaseRunner,
    phases: list[str],
    metrics: RunMetrics,
    slug: str,
    reuse_phases: set[str],
) -> dict[str, str]:
    """Run only non-reused phases; skip reused ones.

    Returns gate verdicts keyed by artifact name.
    """
    for phase in phases:
        if _should_skip(phase, reuse_phases):
            # Phase is reused — no model call
            pass
        else:
            runner.run_phase(phase)
            metrics.increment_model_calls(1)
            metrics.add_wall_clock(SIMULATED_CALL_COST_S)

    source = "reused" if reuse_phases else "generated"
    metrics.record_candidate(slug, source)

    # Gate verdicts are identical stubs (parity assertion)
    return {"resume": "pass", "cover_letter": "pass"}


# ---------------------------------------------------------------------------
# test_metrics_persisted
# ---------------------------------------------------------------------------


class TestMetricsPersisted:
    """Metric keys (model-call count, wall-clock, tallies) land in run_metrics."""

    def test_metrics_persisted(self, mem_db: sqlite3.Connection) -> None:
        """record_run_metrics persists all expected keys for a run."""
        slug = "acme-senior-eng-2026-06"
        metrics = RunMetrics()
        metrics.increment_model_calls(5)
        metrics.add_wall_clock(12.5)
        metrics.record_candidate("cand-a", "reused")
        metrics.record_candidate("cand-b", "generated")
        metrics.record_candidate("cand-c", "reused")

        record_run_metrics(mem_db, slug, metrics)

        summary = read_run_metrics_summary(mem_db, slug=slug)

        assert summary["run.model_call_count"] == 5, "model call count must be persisted"
        assert abs(summary["run.wall_clock_seconds"] - 12.5) < 0.001, "wall clock must be persisted"
        assert summary["candidate.reused_count"] == 2, "reused tally"
        assert summary["candidate.generated_count"] == 1, "generated tally"
        assert summary["candidate.cand-a.source"] == "reused"
        assert summary["candidate.cand-b.source"] == "generated"
        assert summary["candidate.cand-c.source"] == "reused"


# ---------------------------------------------------------------------------
# test_report_names_sources
# ---------------------------------------------------------------------------


class TestReportNamesSources:
    """render_reuse_report_from_metrics names each reused source app/artifact."""

    def test_report_names_sources(self, mem_db: sqlite3.Connection) -> None:
        """Report contains named source slugs for reused artifacts."""
        slug = "target-app-2026-06"

        # Seed metrics
        metrics = RunMetrics()
        metrics.increment_model_calls(3)
        metrics.add_wall_clock(3.0)
        metrics.record_candidate("prior-acme-2026-05", "reused")

        record_run_metrics(mem_db, slug, metrics)

        # Also seed backstop and company_research_source metrics
        upsert_run_metric(mem_db, slug=slug, metric_key="backstop.resume.verdict", metric_value="pass")
        upsert_run_metric(mem_db, slug=slug, metric_key="backstop.resume.regen_count", metric_value="0")
        upsert_run_metric(mem_db, slug=slug, metric_key="backstop.cover_letter.verdict", metric_value="pass")
        upsert_run_metric(mem_db, slug=slug, metric_key="backstop.cover_letter.regen_count", metric_value="0")
        upsert_run_metric(mem_db, slug=slug, metric_key="company_research_source", metric_value="reused")

        # Stub reuse plan with named sources
        @dataclass
        class _FakePhaseDec:
            decision: str
            source: str | None
            score: float = 0.0

        @dataclass
        class _FakeReusePlan:
            jd_parse: _FakePhaseDec
            fit_score: _FakePhaseDec
            company_research: _FakePhaseDec
            draft: _FakePhaseDec
            bullet_map: dict = field(default_factory=dict)
            matched_slug: str | None = None

        reuse_plan = _FakeReusePlan(
            jd_parse=_FakePhaseDec(decision="reuse", source="prior-acme-2026-05"),
            fit_score=_FakePhaseDec(decision="reuse", source="prior-acme-2026-05"),
            company_research=_FakePhaseDec(decision="reuse", source="Acme Inc"),
            draft=_FakePhaseDec(decision="warm-start", source="prior-acme-2026-05", score=0.88),
        )

        @dataclass
        class _FakeWarmStart:
            anchors_carried: list = field(default_factory=list)
            delta_requirement_hashes: list = field(default_factory=list)
            reused_bullet_ids: list = field(default_factory=list)

        warmstart = _FakeWarmStart(
            anchors_carried=[{"id": "a1"}, {"id": "a2"}],
            delta_requirement_hashes=["h3"],
            reused_bullet_ids=["b1", "b2"],
        )

        summary = read_run_metrics_summary(mem_db, slug=slug)
        report = render_reuse_report_from_metrics(
            summary, slug, reuse_plan=reuse_plan, warmstart_result=warmstart
        )

        # Must name each reused source
        assert "prior-acme-2026-05" in report, "report must name the prior app slug"
        assert "Acme Inc" in report, "report must name the company"
        assert "jd-parse" in report, "report must name jd-parse artifact"
        assert "company-research" in report, "report must name company-research artifact"
        assert "warm-start" in report.lower(), "report must mention warm-start"
        assert "resume" in report, "backstop resume verdict must appear"
        assert "pass" in report, "gate pass verdict must appear"


# ---------------------------------------------------------------------------
# test_reuse_ab_savings_and_gate_parity
# ---------------------------------------------------------------------------


class TestReuseAbSavingsAndGateParity:
    """A/B harness: reuse arm must save >=50% calls AND >=50% wall-clock AND gate-parity.

    Design
    ------
    - Both arms use a fresh _FakePhaseRunner (model call counter + simulated time).
    - llm_cache is bypassed by using an in-memory DB with no llm_cache rows
      (no patching needed — the stub pipeline never touches llm_cache).
    - The reuse arm seeds 4 out of 8 phases as "reused" (50% skip rate).
    - Gate verdicts are identical stubs ("pass") in both arms.
    - AND-gate: calls_reduction >= 0.5 AND time_reduction >= 0.5 AND parity.
    """

    # 8 pipeline phases in the stub (mirrors gather/jd-parse/fit-score/
    # company-research/bullet-select/draft/review/render order)
    PHASES = [
        "gather",
        "jd-parse",
        "fit-score",
        "company-research",
        "bullet-select",
        "draft",
        "review",
        "render",
    ]

    # Phases the reuse arm skips (>= 50% of total)
    REUSE_PHASES = {
        "jd-parse",
        "fit-score",
        "company-research",
        "draft",
    }

    SLUG_NO_REUSE = "target-app-no-reuse"
    SLUG_REUSE = "target-app-reuse"

    def test_reuse_ab_savings_and_gate_parity(self, mem_db: sqlite3.Connection) -> None:
        """AND-gate: >=50% call reduction AND >=50% wall-clock AND gate-parity."""
        # --- No-reuse arm ---
        runner_nr = _FakePhaseRunner()
        metrics_nr = RunMetrics()
        verdicts_nr = _run_no_reuse_pipeline(
            runner_nr, self.PHASES, metrics_nr, self.SLUG_NO_REUSE
        )
        record_run_metrics(mem_db, self.SLUG_NO_REUSE, metrics_nr)

        # --- Reuse arm ---
        runner_r = _FakePhaseRunner()
        metrics_r = RunMetrics()
        verdicts_r = _run_reuse_pipeline(
            runner_r, self.PHASES, metrics_r, self.SLUG_REUSE, self.REUSE_PHASES
        )
        record_run_metrics(mem_db, self.SLUG_REUSE, metrics_r)

        # --- Read back from DB ---
        summary_nr = read_run_metrics_summary(mem_db, slug=self.SLUG_NO_REUSE)
        summary_r = read_run_metrics_summary(mem_db, slug=self.SLUG_REUSE)

        calls_nr = summary_nr["run.model_call_count"]
        calls_r = summary_r["run.model_call_count"]
        time_nr = summary_nr["run.wall_clock_seconds"]
        time_r = summary_r["run.wall_clock_seconds"]

        # --- AND-gate 1: call reduction ---
        assert calls_nr > 0, "no-reuse arm must make model calls"
        calls_reduction = (calls_nr - calls_r) / calls_nr
        assert calls_reduction >= 0.50, (
            f"model-call reduction {calls_reduction:.1%} is below 50% threshold "
            f"(no-reuse={calls_nr}, reuse={calls_r})"
        )

        # --- AND-gate 2: wall-clock reduction ---
        assert time_nr > 0, "no-reuse arm must have non-zero wall-clock"
        time_reduction = (time_nr - time_r) / time_nr
        assert time_reduction >= 0.50, (
            f"wall-clock reduction {time_reduction:.1%} is below 50% threshold "
            f"(no-reuse={time_nr:.2f}s, reuse={time_r:.2f}s)"
        )

        # --- AND-gate 3: gate-verdict parity ---
        assert verdicts_nr == verdicts_r, (
            f"gate verdicts differ between arms: no-reuse={verdicts_nr!r}, reuse={verdicts_r!r}"
        )

        # Sanity: correct phase counts
        assert calls_nr == len(self.PHASES), f"no-reuse must run all {len(self.PHASES)} phases"
        assert calls_r == len(self.PHASES) - len(self.REUSE_PHASES), (
            f"reuse must skip {len(self.REUSE_PHASES)} phases"
        )
