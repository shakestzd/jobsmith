"""jobsmith.reuse.backstop — correctness backstop gate (slice-8).

POST-assembly, PRE-acceptance: unconditionally runs guard.py ``check_anchors``
and factcheck.py ``check_draft`` on every assembled resume + cover letter.

Failure policy: on gate failure, regenerate via regen_fn up to
``config.reuse.regen_retry_bound`` times, then fall back to fallback_fn,
then raise ``BackstopError`` — NEVER ship ungated output.

Metric keys: ``backstop.{resume|cover_letter}.{verdict|regen_count}``
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BackstopError(Exception):
    """All retries + fallback exhausted — gate still failing."""


@dataclass
class GateVerdict:
    artifact: str  # "resume" | "cover_letter"
    passed: bool
    anchor_exit_code: int
    factcheck_passed: bool
    failed_claims: list[str] = field(default_factory=list)
    regen_count: int = 0
    outcome: str = "pass"  # "pass" | "fail_regen" | "fail_fallback" | "error"


@dataclass
class BackstopResult:
    resume: GateVerdict
    cover_letter: GateVerdict

    @property
    def passed(self) -> bool:
        return self.resume.passed and self.cover_letter.passed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_anchor_gate(
    master_path: Path,
    selection_path: Path,
    decisions_path: Path | None = None,
) -> int:
    """Run check_anchors; return exit_code (0=pass, 1=fail, 2=error)."""
    try:
        from jobsmith.guard import check_anchors

        return check_anchors(master_path, selection_path, decisions_path).exit_code
    except Exception as exc:  # noqa: BLE001
        logger.warning("backstop: anchor gate error: %s", exc)
        return 2


def _run_factcheck_gate(
    draft_text: str,
    content_dir: Path,
    extra_sources: dict[str, str] | None = None,
) -> tuple[bool, list[str]]:
    """Run check_draft; return (passed, failed_claims)."""
    try:
        from jobsmith.factcheck import check_draft

        result = check_draft(draft_text, content_dir, extra_sources=extra_sources)
        return result.passed, result.failed_claims
    except Exception as exc:  # noqa: BLE001
        logger.warning("backstop: factcheck gate error: %s", exc)
        return False, [f"gate-error: {exc}"]


def _gate_draft_text(
    draft_text: str,
    content_dir: Path,
    master_path: Path,
    selection_path: Path,
    decisions_path: Path | None = None,
    extra_sources: dict[str, str] | None = None,
) -> tuple[bool, int, bool, list[str]]:
    """Run both gates; return (overall_pass, anchor_rc, fc_passed, failed_claims)."""
    anchor_rc = _run_anchor_gate(master_path, selection_path, decisions_path)
    fc_passed, failed_claims = _run_factcheck_gate(draft_text, content_dir, extra_sources)
    return (anchor_rc == 0) and fc_passed, anchor_rc, fc_passed, failed_claims


def _record_metrics(conn: sqlite3.Connection | None, slug: str, verdict: GateVerdict) -> None:
    if conn is None:
        return
    try:
        from jobsmith.reuse.store import upsert_run_metric

        prefix = f"backstop.{verdict.artifact}"
        upsert_run_metric(conn, slug=slug, metric_key=f"{prefix}.verdict", metric_value=verdict.outcome)
        upsert_run_metric(conn, slug=slug, metric_key=f"{prefix}.regen_count", metric_value=str(verdict.regen_count))
    except Exception as exc:  # noqa: BLE001
        logger.warning("backstop: could not record metrics: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_backstop(
    *,
    slug: str,
    resume_text: str,
    cover_letter_text: str,
    master_path: Path,
    content_dir: Path,
    selection_path: Path,
    decisions_path: Path | None = None,
    extra_sources: dict[str, str] | None = None,
    regen_retry_bound: int = 3,
    resume_regen_fn: Callable[[], str] | None = None,
    resume_fallback_fn: Callable[[], str] | None = None,
    cover_letter_regen_fn: Callable[[], str] | None = None,
    cover_letter_fallback_fn: Callable[[], str] | None = None,
    db_conn: sqlite3.Connection | None = None,
) -> BackstopResult:
    """Run anchor + factcheck gates on the final resume and cover letter.

    UNCONDITIONAL — runs whether or not reuse/warm-start was used.
    Raises BackstopError only when all retries + fallback are exhausted.

    ``extra_sources`` is forwarded to ``check_draft`` so JD-domain terms
    (IRA, Energy Community) can be verified against the job description.
    """
    resume_verdict = _gate_artifact(
        artifact="resume",
        draft_text=resume_text,
        master_path=master_path,
        content_dir=content_dir,
        selection_path=selection_path,
        decisions_path=decisions_path,
        extra_sources=extra_sources,
        regen_retry_bound=regen_retry_bound,
        regen_fn=resume_regen_fn,
        fallback_fn=resume_fallback_fn,
    )
    _record_metrics(db_conn, slug, resume_verdict)

    cover_letter_verdict = _gate_artifact(
        artifact="cover_letter",
        draft_text=cover_letter_text,
        master_path=master_path,
        content_dir=content_dir,
        selection_path=selection_path,
        decisions_path=decisions_path,
        extra_sources=extra_sources,
        regen_retry_bound=regen_retry_bound,
        regen_fn=cover_letter_regen_fn,
        fallback_fn=cover_letter_fallback_fn,
    )
    _record_metrics(db_conn, slug, cover_letter_verdict)

    logger.info("backstop: slug=%s resume=%s cover_letter=%s", slug, resume_verdict.outcome, cover_letter_verdict.outcome)
    return BackstopResult(resume=resume_verdict, cover_letter=cover_letter_verdict)


def _gate_artifact(
    *,
    artifact: str,
    draft_text: str,
    master_path: Path,
    content_dir: Path,
    selection_path: Path,
    decisions_path: Path | None,
    extra_sources: dict[str, str] | None = None,
    regen_retry_bound: int,
    regen_fn: Callable[[], str] | None,
    fallback_fn: Callable[[], str] | None,
) -> GateVerdict:
    """Gate one artifact with bounded retry + fallback. Raises BackstopError on exhaustion."""
    current_text = draft_text
    regen_count = 0

    overall, anchor_rc, fc_passed, failed_claims = _gate_draft_text(
        current_text, content_dir, master_path, selection_path, decisions_path, extra_sources
    )
    if overall:
        return GateVerdict(artifact=artifact, passed=True, anchor_exit_code=anchor_rc,
                           factcheck_passed=fc_passed, failed_claims=failed_claims,
                           regen_count=0, outcome="pass")

    # Retry loop
    if regen_fn is not None:
        for attempt in range(regen_retry_bound):
            logger.warning("backstop: %s gate failed (attempt %d/%d), regenerating...",
                           artifact, attempt + 1, regen_retry_bound)
            try:
                current_text = regen_fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("backstop: regen_fn raised: %s", exc)
                break
            regen_count += 1
            overall, anchor_rc, fc_passed, failed_claims = _gate_draft_text(
                current_text, content_dir, master_path, selection_path, decisions_path, extra_sources
            )
            if overall:
                return GateVerdict(artifact=artifact, passed=True, anchor_exit_code=anchor_rc,
                                   factcheck_passed=fc_passed, failed_claims=failed_claims,
                                   regen_count=regen_count, outcome="fail_regen")

    # Fallback
    if fallback_fn is not None:
        logger.warning("backstop: %s retries exhausted, trying fallback...", artifact)
        try:
            current_text = fallback_fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("backstop: fallback_fn raised: %s", exc)
        else:
            overall, anchor_rc, fc_passed, failed_claims = _gate_draft_text(
                current_text, content_dir, master_path, selection_path, decisions_path, extra_sources
            )
            if overall:
                return GateVerdict(artifact=artifact, passed=True, anchor_exit_code=anchor_rc,
                                   factcheck_passed=fc_passed, failed_claims=failed_claims,
                                   regen_count=regen_count, outcome="fail_fallback")

    raise BackstopError(
        f"backstop: {artifact} gate still failing after {regen_count} regen(s) + fallback; "
        f"anchor_rc={anchor_rc}, failed_claims={failed_claims}. Cannot ship ungated output."
    )


def gate_verdict_for_text(
    draft_text: str,
    *,
    master_path: Path,
    content_dir: Path,
    selection_path: Path,
    decisions_path: Path | None = None,
    extra_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Lightweight gate check returning a plain dict verdict for parity comparison."""
    anchor_rc = _run_anchor_gate(master_path, selection_path, decisions_path)
    fc_passed, failed_claims = _run_factcheck_gate(draft_text, content_dir, extra_sources)
    return {
        "anchor_passed": anchor_rc == 0,
        "factcheck_passed": fc_passed,
        "passed": (anchor_rc == 0) and fc_passed,
        "failed_claims": failed_claims,
    }


__all__ = [
    "BackstopError",
    "BackstopResult",
    "GateVerdict",
    "gate_verdict_for_text",
    "run_backstop",
]
