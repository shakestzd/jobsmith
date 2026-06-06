"""jobsmith.reuse.report — human-readable end-of-run reuse report (slice-9).

Reads ``run_metrics`` rows for a slug and renders a plain-text summary that
NAMES each reused source application and artifact, e.g.:

    ─── Reuse Report: acme-senior-eng-2026-06 ───
    model calls  : 4  (saved ~8 vs no-reuse baseline)
    wall clock   : 12.3 s
    candidates   : 2 reused, 1 generated

    Reused artifacts:
      • company-research  from app: prior-acme-2026-05
      • jd-parse          from app: prior-acme-2026-05
      • warm-started from resume:   prior-acme-2026-05 (3 bullets carried, 2 regenerated)

    Backstop gates:
      resume      : pass  (0 regens)
      cover_letter: pass  (0 regens)

Public API
----------
``render_reuse_report(conn, slug, *, reuse_plan, warmstart_result) -> str``
``render_reuse_report_from_metrics(metrics_dict, slug) -> str``
"""
from __future__ import annotations

import sqlite3
from typing import Any

from jobsmith.reuse.metrics import read_run_metrics_summary

# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _section(title: str) -> str:
    bar = "─" * (len(title) + 4)
    return f"{bar}\n  {title}\n{bar}"


def _fmt_float(v: Any, dp: int = 1) -> str:
    try:
        return f"{float(v):.{dp}f}"
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_reuse_report(
    conn: sqlite3.Connection,
    slug: str,
    *,
    reuse_plan: Any | None = None,
    warmstart_result: Any | None = None,
) -> str:
    """Render a human-readable reuse report for *slug* from ``run_metrics``.

    Parameters
    ----------
    conn:
        Open SQLite connection to the pipeline DB.
    slug:
        Application slug to report on.
    reuse_plan:
        Optional :class:`~jobsmith.reuse.planner.ReusePlan` — if provided,
        adds named artifact sources from the plan decisions.
    warmstart_result:
        Optional :class:`~jobsmith.reuse.warmstart.WarmStartResult` — if
        provided, adds warm-start bullet statistics.

    Returns
    -------
    str
        Human-readable report string.
    """
    summary = read_run_metrics_summary(conn, slug=slug)
    return render_reuse_report_from_metrics(
        summary,
        slug,
        reuse_plan=reuse_plan,
        warmstart_result=warmstart_result,
    )


def render_reuse_report_from_metrics(
    metrics_dict: dict[str, Any],
    slug: str,
    *,
    reuse_plan: Any | None = None,
    warmstart_result: Any | None = None,
) -> str:
    """Render a reuse report from a pre-read metrics dictionary.

    This variant is useful in tests and offline tooling where the DB
    connection is not available but ``read_run_metrics_summary`` output is.

    Parameters
    ----------
    metrics_dict:
        Output of :func:`~jobsmith.reuse.metrics.read_run_metrics_summary`.
    slug:
        Application slug (used in the header).
    reuse_plan:
        Optional ``ReusePlan`` for named artifact sources.
    warmstart_result:
        Optional ``WarmStartResult`` for warm-start bullet stats.

    Returns
    -------
    str
        Human-readable report string.
    """
    lines: list[str] = []

    header = f"Reuse Report: {slug}"
    lines.append(_section(header))
    lines.append("")

    # --- Run-level numbers ---
    model_calls = metrics_dict.get("run.model_call_count", "n/a")
    wall_clock = metrics_dict.get("run.wall_clock_seconds", "n/a")
    reused_count = metrics_dict.get("candidate.reused_count", "n/a")
    generated_count = metrics_dict.get("candidate.generated_count", "n/a")

    lines.append(f"  model calls  : {model_calls}")
    lines.append(f"  wall clock   : {_fmt_float(wall_clock)} s")
    lines.append(f"  candidates   : {reused_count} reused, {generated_count} generated")
    lines.append("")

    # --- Reused artifacts (named) ---
    reuse_lines: list[str] = []

    if reuse_plan is not None:
        # jd-parse / fit-score
        jd_dec = getattr(reuse_plan, "jd_parse", None)
        if jd_dec is not None and getattr(jd_dec, "decision", None) == "reuse":
            src = getattr(jd_dec, "source", None) or "unknown"
            reuse_lines.append(f"  • jd-parse          from app: {src}")
            reuse_lines.append(f"  • fit-score         from app: {src}")

        # company-research
        cr_dec = getattr(reuse_plan, "company_research", None)
        if cr_dec is not None and getattr(cr_dec, "decision", None) == "reuse":
            src = getattr(cr_dec, "source", None) or "unknown"
            reuse_lines.append(f"  • company-research  from company: {src}")

        # warm-start draft
        draft_dec = getattr(reuse_plan, "draft", None)
        if draft_dec is not None and getattr(draft_dec, "decision", None) == "warm-start":
            src = getattr(draft_dec, "source", None) or "unknown"
            score = getattr(draft_dec, "score", 0.0)
            extra = ""
            if warmstart_result is not None:
                anchors = len(getattr(warmstart_result, "anchors_carried", []))
                delta = len(getattr(warmstart_result, "delta_requirement_hashes", []))
                reused_b = len(getattr(warmstart_result, "reused_bullet_ids", []))
                extra = f" ({anchors} anchors carried, {reused_b} bullets reused, {delta} regenerated)"
            reuse_lines.append(
                f"  • warm-started from resume: {src}  (JD overlap {score:.0%}){extra}"
            )

    # Also pull per-candidate sources from metrics
    candidate_source_lines: list[str] = []
    for key, val in sorted(metrics_dict.items()):
        if key.startswith("candidate.") and key.endswith(".source"):
            # key format: candidate.<candidate_slug>.source
            parts = key.split(".", 2)
            if len(parts) == 3:
                cand_slug = parts[1]
                candidate_source_lines.append(f"  • candidate {cand_slug}: {val}")

    if reuse_lines or candidate_source_lines:
        lines.append("  Reused artifacts:")
        lines.extend(reuse_lines)
        if candidate_source_lines:
            lines.append("  Candidate sources:")
            lines.extend(candidate_source_lines)
        lines.append("")

    # --- Backstop gate results ---
    resume_verdict = metrics_dict.get("backstop.resume.verdict")
    resume_regen = metrics_dict.get("backstop.resume.regen_count")
    cl_verdict = metrics_dict.get("backstop.cover_letter.verdict")
    cl_regen = metrics_dict.get("backstop.cover_letter.regen_count")

    if resume_verdict or cl_verdict:
        lines.append("  Backstop gates:")
        if resume_verdict:
            lines.append(f"    resume      : {resume_verdict}  ({resume_regen or 0} regens)")
        if cl_verdict:
            lines.append(f"    cover_letter: {cl_verdict}  ({cl_regen or 0} regens)")
        lines.append("")

    # --- Company research cache ---
    company_source = metrics_dict.get("company_research_source")
    if company_source:
        lines.append(f"  company research: {company_source}")
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "render_reuse_report",
    "render_reuse_report_from_metrics",
]
