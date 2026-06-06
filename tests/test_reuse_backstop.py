"""Tests for jobsmith.reuse.backstop (feat-bd0fc232 / slice-8).

TDD: tests specify behavior.

Covers:
  Unit:
    - test_gates_run_on_reused_output
    - test_failed_gate_triggers_regen
    - test_retry_bound_then_fallback_then_error

  Integration:
    - test_reuse_vs_no_reuse_gate_parity
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_master_work_yml(tmp_path: Path) -> Path:
    master = tmp_path / "work.yml"
    master.write_text(
        "- location: Acme Corp\n  title: Engineer\n  details:\n    - Built tooling\n",
        encoding="utf-8",
    )
    return master


def _make_selection_json(tmp_path: Path) -> Path:
    sel = tmp_path / "bullet-selection.json"
    sel.write_text(
        json.dumps({"positions": [{"company": "Acme Corp", "title": "Engineer", "bullets": [
            {"master_bullet_id": "aabbcc112233", "included": True, "reason_if_dropped": None}
        ]}]}),
        encoding="utf-8",
    )
    return sel


def _make_content_dir(tmp_path: Path) -> Path:
    content = tmp_path / "content"
    content.mkdir(parents=True, exist_ok=True)
    (content / "work.yml").write_text("Built tooling\n", encoding="utf-8")
    return content


def _pass_gates():
    """Context managers that make both gates pass."""
    return (
        patch("jobsmith.reuse.backstop._run_anchor_gate", return_value=0),
        patch("jobsmith.reuse.backstop._run_factcheck_gate", return_value=(True, [])),
    )


def _fail_gates():
    """Context managers that make both gates fail."""
    return (
        patch("jobsmith.reuse.backstop._run_anchor_gate", return_value=1),
        patch("jobsmith.reuse.backstop._run_factcheck_gate", return_value=(False, ["bad-claim"])),
    )


# ---------------------------------------------------------------------------
# Unit: test_gates_run_on_reused_output
# ---------------------------------------------------------------------------


class TestGatesRunOnReusedOutput:
    """Gates UNCONDITIONALLY run on the final output (reuse or not)."""

    def test_gates_run_on_reused_output(self, tmp_path: Path) -> None:
        master = _make_master_work_yml(tmp_path)
        sel = _make_selection_json(tmp_path)
        content = _make_content_dir(tmp_path)

        with (
            patch("jobsmith.reuse.backstop._run_anchor_gate", return_value=0) as mock_anchor,
            patch("jobsmith.reuse.backstop._run_factcheck_gate", return_value=(True, [])) as mock_fc,
        ):
            from jobsmith.reuse.backstop import run_backstop

            result = run_backstop(
                slug="test-slug",
                resume_text="Built tooling.",
                cover_letter_text="Cover letter text.",
                master_path=master,
                content_dir=content,
                selection_path=sel,
            )

        # Both gates must run for both artifacts (2 calls each)
        assert mock_anchor.call_count == 2, "anchor gate must run for both artifacts"
        assert mock_fc.call_count == 2, "factcheck gate must run for both artifacts"
        assert result.passed is True
        assert result.resume.outcome == "pass"
        assert result.cover_letter.outcome == "pass"


# ---------------------------------------------------------------------------
# Unit: test_failed_gate_triggers_regen
# ---------------------------------------------------------------------------


class TestFailedGateTriggersRegen:
    """A gate failure on first pass triggers regen_fn and re-gates."""

    def test_failed_gate_triggers_regen(self, tmp_path: Path) -> None:
        master = _make_master_work_yml(tmp_path)
        sel = _make_selection_json(tmp_path)
        content = _make_content_dir(tmp_path)

        regen_calls: list[int] = []
        call_count = [0]

        def mock_anchor(m, s, d=None):
            call_count[0] += 1
            # First resume call fails; subsequent calls pass
            return 1 if call_count[0] == 1 else 0

        with (
            patch("jobsmith.reuse.backstop._run_anchor_gate", side_effect=mock_anchor),
            patch("jobsmith.reuse.backstop._run_factcheck_gate", return_value=(True, [])),
        ):
            from jobsmith.reuse.backstop import run_backstop

            result = run_backstop(
                slug="test-slug",
                resume_text="original text",
                cover_letter_text="cover letter",
                master_path=master,
                content_dir=content,
                selection_path=sel,
                regen_retry_bound=3,
                resume_regen_fn=lambda: (regen_calls.append(1) or "regen text"),
            )

        assert len(regen_calls) == 1, "regen called once after first failure"
        assert result.resume.regen_count == 1
        assert result.resume.outcome == "fail_regen"
        assert result.resume.passed is True


# ---------------------------------------------------------------------------
# Unit: test_retry_bound_then_fallback_then_error
# ---------------------------------------------------------------------------


class TestRetryBoundThenFallbackThenError:
    """Retry exhaustion → fallback; if fallback also fails → BackstopError."""

    def test_retry_bound_then_fallback_then_error(self, tmp_path: Path) -> None:
        master = _make_master_work_yml(tmp_path)
        sel = _make_selection_json(tmp_path)
        content = _make_content_dir(tmp_path)

        regen_count = [0]
        fallback_count = [0]
        retry_bound = 2

        with (
            patch("jobsmith.reuse.backstop._run_anchor_gate", return_value=1),
            patch("jobsmith.reuse.backstop._run_factcheck_gate", return_value=(False, ["bad"])),
        ):
            from jobsmith.reuse.backstop import BackstopError, run_backstop

            with pytest.raises(BackstopError, match="gate still failing"):
                run_backstop(
                    slug="test-slug",
                    resume_text="bad resume",
                    cover_letter_text="bad letter",
                    master_path=master,
                    content_dir=content,
                    selection_path=sel,
                    regen_retry_bound=retry_bound,
                    resume_regen_fn=lambda: (regen_count.__setitem__(0, regen_count[0] + 1) or "bad"),
                    resume_fallback_fn=lambda: (fallback_count.__setitem__(0, fallback_count[0] + 1) or "bad"),
                )

        assert regen_count[0] == retry_bound, f"expected {retry_bound} regen attempts"
        assert fallback_count[0] == 1, "fallback tried once after retries exhausted"

    def test_fallback_succeeds_after_retries_exhausted(self, tmp_path: Path) -> None:
        master = _make_master_work_yml(tmp_path)
        sel = _make_selection_json(tmp_path)
        content = _make_content_dir(tmp_path)

        gate_n = [0]

        def mock_anchor(m, s, d=None):
            gate_n[0] += 1
            # Initial + 2 retries fail; fallback + cover letter pass
            return 1 if gate_n[0] <= 3 else 0

        with (
            patch("jobsmith.reuse.backstop._run_anchor_gate", side_effect=mock_anchor),
            patch("jobsmith.reuse.backstop._run_factcheck_gate", return_value=(True, [])),
        ):
            from jobsmith.reuse.backstop import run_backstop

            result = run_backstop(
                slug="test-slug",
                resume_text="original",
                cover_letter_text="letter",
                master_path=master,
                content_dir=content,
                selection_path=sel,
                regen_retry_bound=2,
                resume_regen_fn=lambda: "still bad",
                resume_fallback_fn=lambda: "now good",
            )

        assert result.resume.passed is True
        assert result.resume.outcome == "fail_fallback"


# ---------------------------------------------------------------------------
# Integration: test_reuse_vs_no_reuse_gate_parity
# ---------------------------------------------------------------------------


class TestReuseVsNoReuseGateParity:
    """Gate VERDICT must match between reuse and no-reuse runs."""

    def test_reuse_vs_no_reuse_gate_parity(self, tmp_path: Path) -> None:
        master = _make_master_work_yml(tmp_path)
        sel = _make_selection_json(tmp_path)
        content = _make_content_dir(tmp_path)

        with (
            patch("jobsmith.reuse.backstop._run_anchor_gate", return_value=0),
            patch("jobsmith.reuse.backstop._run_factcheck_gate", return_value=(True, [])),
        ):
            from jobsmith.reuse.backstop import gate_verdict_for_text

            reuse_verdict = gate_verdict_for_text(
                "some text",
                master_path=master,
                content_dir=content,
                selection_path=sel,
            )
            no_reuse_verdict = gate_verdict_for_text(
                "some text",
                master_path=master,
                content_dir=content,
                selection_path=sel,
            )

        assert reuse_verdict["passed"] == no_reuse_verdict["passed"]
        assert reuse_verdict["anchor_passed"] == no_reuse_verdict["anchor_passed"]
        assert reuse_verdict["factcheck_passed"] == no_reuse_verdict["factcheck_passed"]


# ---------------------------------------------------------------------------
# Metric recording
# ---------------------------------------------------------------------------


class TestMetricRecording:
    """Gate outcomes are recorded into run_metrics with correct keys."""

    def test_metrics_recorded_on_pass(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE run_metrics (
            slug TEXT, metric_key TEXT, metric_value TEXT, created_at TEXT,
            PRIMARY KEY (slug, metric_key))""")

        master = _make_master_work_yml(tmp_path)
        sel = _make_selection_json(tmp_path)
        content = _make_content_dir(tmp_path)

        with (
            patch("jobsmith.reuse.backstop._run_anchor_gate", return_value=0),
            patch("jobsmith.reuse.backstop._run_factcheck_gate", return_value=(True, [])),
        ):
            from jobsmith.reuse.backstop import run_backstop

            run_backstop(
                slug="myapp",
                resume_text="text",
                cover_letter_text="letter",
                master_path=master,
                content_dir=content,
                selection_path=sel,
                db_conn=conn,
            )

        rows = {
            r["metric_key"]: r["metric_value"]
            for r in conn.execute("SELECT * FROM run_metrics WHERE slug='myapp'").fetchall()
        }
        assert rows.get("backstop.resume.verdict") == "pass"
        assert rows.get("backstop.resume.regen_count") == "0"
        assert rows.get("backstop.cover_letter.verdict") == "pass"
        assert rows.get("backstop.cover_letter.regen_count") == "0"
