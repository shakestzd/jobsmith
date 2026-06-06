"""jobsmith.reuse.planner — reuse-planner pre-phase for the apply pipeline.

This module is the integration bottleneck for slices 1-5.  Before the normal
apply phases run, ``compute_reuse_plan`` queries all reuse primitives and
returns a :class:`ReusePlan` describing, per-phase, whether to reuse prior
artifacts or regenerate from scratch.

Design decisions
----------------
- Correctness-first: any uncertainty => regenerate.
- ``no_reuse_plan()`` returns a fully-regenerate plan; the pipeline uses this
  when ``--no-reuse`` is set, producing byte-for-byte legacy behavior.
- ``should_skip_phase(plan, specialist)`` maps specialist names to plan fields
  so the pipeline can gate each specialist.
- The planner is intentionally stateless: it reads only (never writes).
  Writes happen in the pipeline after a phase succeeds.
- Sits ABOVE the exact-input llm_cache (migration 008): this catches
  near-duplicate JDs and evidence-map hits; llm_cache catches exact-input
  repeats independently.

Slices 7/8/9 integration points
---------------------------------
- Slice 7 (warm-start): read ``plan.draft.decision`` ("warm-start"|"regenerate"),
  ``plan.jd_parse.decision``, ``plan.matched_slug``, and ``plan.bullet_map``
  to build a warm-start delta for the draft phase.
  ``jd_overlap_warm_start_threshold`` gates the draft decision.
- Slice 8 (backstop): unconditional final-output re-gate; independent of plan.
- Slice 9 (metrics): reads ``run_metrics`` table (written by pipeline after
  phases complete).

Public API
----------
``PhaseDecision`` — dataclass: decision ("reuse"|"warm-start"|"regenerate"),
                    source (slug/key or None), score (float, 0.0 when N/A)
``ReusePlan``     — dataclass: per-phase PhaseDecision + bullet_map + matched_slug
                    + draft (warm-start decision) + jd_overlap_score
``compute_reuse_plan(conn, *, jd_text, current_slug, cfg, companies_dir,
                     company_name, current_bullet_texts, requirement_hashes)
                   -> ReusePlan``
``no_reuse_plan() -> ReusePlan``  — all-regenerate sentinel
``should_skip_phase(plan, specialist) -> bool``
``write_reuse_plan_artifact(plan, app_state_dir) -> None``  — serialize to JSON
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from jobsmith.config import ReuseSettings
from jobsmith.reuse.company_cache import check_cache
from jobsmith.reuse.dedup import find_duplicate_jd
from jobsmith.reuse.evidence_map import lookup_mapped_bullet

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class PhaseDecision:
    """Decision for a single pipeline phase.

    Attributes
    ----------
    decision:
        ``"reuse"``       — load prior artifact, skip LLM call.
        ``"warm-start"``  — use prior artifact as a base, run delta pass.
        ``"regenerate"``  — run the phase normally.
    source:
        For ``"reuse"`` / ``"warm-start"`` decisions: the slug or key that
        identifies the prior artifact.  ``None`` for ``"regenerate"``.
    score:
        Similarity score used to derive this decision (0.0–1.0).
        0.0 when not applicable (e.g. ``"regenerate"`` with no prior).
    """

    decision: str  # "reuse" | "warm-start" | "regenerate"
    source: str | None
    score: float = 0.0


@dataclass
class ReusePlan:
    """Per-application reuse plan computed before phases run.

    Fields
    ------
    jd_parse:
        Whether to reuse jd-parsed.json from a prior near-duplicate application.
    fit_score:
        Whether to reuse fit-score.json from the same prior application.
        Tied to jd_parse: if jd-parse is regenerated, fit-score must be too.
    company_research:
        Whether to reuse company-research.md from the file cache.
    bullet_map:
        ``{requirement_hash: master_bullet_id}`` for requirements where a
        fresh evidence-map row exists.  Empty dict when none found.
        Slice-7 reads this to build the warm-start delta.
    matched_slug:
        The prior application slug that produced the jd_parse/fit_score reuse
        decision.  ``None`` when jd_parse.decision == ``"regenerate"``.
    draft:
        Warm-start decision for the draft phase.
        ``decision == "warm-start"`` when JD overlap >= jd_overlap_warm_start_threshold;
        ``decision == "regenerate"`` otherwise.
        Slice-7 reads this field exclusively to decide whether to apply delta logic.
    jd_overlap_score:
        The JD-overlap (Jaccard/token-set coverage) score against the matched
        prior application.  0.0 when no match or no-reuse plan.
    """

    jd_parse: PhaseDecision
    fit_score: PhaseDecision
    company_research: PhaseDecision
    bullet_map: dict[str, str]
    matched_slug: str | None
    draft: PhaseDecision = None  # type: ignore[assignment]
    jd_overlap_score: float = 0.0

    def __post_init__(self) -> None:
        # Provide a default regenerate draft decision if not explicitly set.
        if self.draft is None:
            self.draft = PhaseDecision(decision="regenerate", source=None, score=0.0)


# ---------------------------------------------------------------------------
# Specialist name → plan field mapping
# ---------------------------------------------------------------------------

# Maps specialist/phase names (as used in the pipeline and prompts) to the
# corresponding ReusePlan field name.  Names ending in "-parse", "-score",
# "-research" map to the three top-level decisions.
_PHASE_FIELD_MAP: dict[str, str] = {
    "jd-parse": "jd_parse",
    "jd_parse": "jd_parse",
    "fit-score": "fit_score",
    "fit_score": "fit_score",
    "company-research": "company_research",
    "company_research": "company_research",
}


def should_skip_phase(plan: ReusePlan, specialist: str) -> bool:
    """Return True when the plan says to reuse (skip) *specialist*.

    Parameters
    ----------
    plan:
        The :class:`ReusePlan` computed by :func:`compute_reuse_plan`.
    specialist:
        Specialist or phase name, e.g. ``"jd-parse"``, ``"fit-score"``,
        ``"company-research"``.  Unknown names always return ``False``
        (regenerate) — correctness-first.

    Returns
    -------
    bool
        ``True`` when the specialist should be skipped (reuse prior artifact).
        ``False`` when the specialist must run normally.
    """
    field_name = _PHASE_FIELD_MAP.get(specialist)
    if field_name is None:
        return False
    phase_decision: PhaseDecision = getattr(plan, field_name)
    return phase_decision.decision == "reuse"


# ---------------------------------------------------------------------------
# no_reuse_plan — all-regenerate sentinel
# ---------------------------------------------------------------------------


def no_reuse_plan() -> ReusePlan:
    """Return a fully-regenerate :class:`ReusePlan` (``--no-reuse`` sentinel).

    The pipeline uses this when ``--no-reuse`` is passed, ensuring byte-for-byte
    legacy behavior: no prior artifacts are ever loaded, no phases are skipped.
    No warm-start is applied.
    """
    return ReusePlan(
        jd_parse=PhaseDecision(decision="regenerate", source=None, score=0.0),
        fit_score=PhaseDecision(decision="regenerate", source=None, score=0.0),
        company_research=PhaseDecision(decision="regenerate", source=None, score=0.0),
        bullet_map={},
        matched_slug=None,
        draft=PhaseDecision(decision="regenerate", source=None, score=0.0),
        jd_overlap_score=0.0,
    )


def write_reuse_plan_artifact(plan: ReusePlan, app_state_dir: Path) -> None:
    """Serialize *plan* to ``<app_state_dir>/reuse-plan.json``.

    Called by the pipeline BEFORE the phase loop / gather so that specialists
    can consult the plan.  Creates the directory if it does not exist.
    Serializes each :class:`PhaseDecision` as a sub-object with ``decision``,
    ``source``, and ``score`` keys.

    Parameters
    ----------
    plan:
        The :class:`ReusePlan` computed by :func:`compute_reuse_plan` (or
        the no-reuse sentinel).
    app_state_dir:
        Path to ``<app-dir>/.apply-state/``.  Created if missing.
    """
    app_state_dir.mkdir(parents=True, exist_ok=True)
    artifact = asdict(plan)
    artifact_path = app_state_dir / "reuse-plan.json"
    artifact_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    logger.debug("reuse-planner: wrote artifact to %s", artifact_path)


# ---------------------------------------------------------------------------
# compute_reuse_plan — main planner function
# ---------------------------------------------------------------------------


def compute_reuse_plan(
    conn: sqlite3.Connection,
    *,
    jd_text: str,
    current_slug: str,
    cfg: ReuseSettings,
    companies_dir: Path,
    company_name: str,
    current_bullet_texts: dict[str, str],
    requirement_hashes: list[str],
) -> ReusePlan:
    """Compute a :class:`ReusePlan` for *current_slug* before phases run.

    Correctness-first: any uncertainty => regenerate.  This function only
    reads the DB and filesystem; it never writes.

    Parameters
    ----------
    conn:
        Open SQLite connection to the pipeline DB.
    jd_text:
        Raw JD text for the current application (used for dedup fingerprint).
    current_slug:
        Slug of the application being processed (self-excluded from dedup).
    cfg:
        ``ReuseSettings`` from ``JobsmithConfig().reuse``.
    companies_dir:
        Directory containing ``<key>.md`` company research files.
    company_name:
        Company name extracted from the JD (may include legal suffixes).
    current_bullet_texts:
        ``{master_bullet_id: text}`` for all bullets currently in master YAML.
        Used by evidence-map freshness check.
    requirement_hashes:
        List of ``content_hash`` values for the current application's
        canonical requirements.  For each hash that has a fresh evidence-map
        row, ``plan.bullet_map[hash]`` is set.

    Returns
    -------
    ReusePlan
        Per-phase decisions plus bullet map.
    """
    # --- Phase 1/2: JD dedup → jd_parse + fit_score decisions ---
    jd_parse_decision = PhaseDecision(decision="regenerate", source=None)
    fit_score_decision = PhaseDecision(decision="regenerate", source=None)
    matched_slug: str | None = None

    try:
        dedup_result = find_duplicate_jd(
            conn,
            jd_text=jd_text,
            current_slug=current_slug,
            cfg=cfg,
        )
        if dedup_result is not None and dedup_result.decision == "reuse":
            matched_slug = dedup_result.matched_slug
            jd_parse_decision = PhaseDecision(decision="reuse", source=matched_slug)
            fit_score_decision = PhaseDecision(decision="reuse", source=matched_slug)
    except Exception as exc:  # noqa: BLE001 — degrade to regenerate, never abort
        logger.warning("planner: JD dedup check failed — regenerating: %s", exc)

    # --- Phase 3: company research → company_research decision ---
    company_research_decision = PhaseDecision(decision="regenerate", source=None)

    if company_name:
        try:
            cached = check_cache(
                company_name,
                companies_dir=companies_dir,
                ttl_days=cfg.company_ttl_days,
            )
            if cached is not None:
                company_research_decision = PhaseDecision(
                    decision="reuse",
                    source=company_name,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("planner: company cache check failed — regenerating: %s", exc)

    # --- Bullet map: per-requirement evidence lookup ---
    bullet_map: dict[str, str] = {}

    for req_hash in requirement_hashes:
        try:
            bullet_id = lookup_mapped_bullet(
                conn,
                requirement_hash=req_hash,
                current_bullet_texts=current_bullet_texts,
            )
            if bullet_id is not None:
                bullet_map[req_hash] = bullet_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "planner: evidence map lookup failed for %s — skipping: %s",
                req_hash,
                exc,
            )

    # --- Draft warm-start: JD-overlap against prior matched application ---
    draft_decision = PhaseDecision(decision="regenerate", source=None, score=0.0)
    jd_overlap_score = 0.0

    if matched_slug is not None and jd_text:
        try:
            from jobsmith.reuse.match import _token_set_ratio

            norm_current = jd_text.strip().lower()
            text_row = conn.execute(
                "SELECT metric_value FROM run_metrics WHERE slug = ? AND metric_key = ?",
                (matched_slug, "jd_normalized_text"),
            ).fetchone()
            if text_row is not None:
                jd_overlap_score = _token_set_ratio(norm_current, text_row[0])
                if jd_overlap_score >= cfg.jd_overlap_warm_start_threshold:
                    draft_decision = PhaseDecision(
                        decision="warm-start",
                        source=matched_slug,
                        score=jd_overlap_score,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("planner: JD overlap check failed — regenerating draft: %s", exc)

    return ReusePlan(
        jd_parse=jd_parse_decision,
        fit_score=fit_score_decision,
        company_research=company_research_decision,
        bullet_map=bullet_map,
        matched_slug=matched_slug,
        draft=draft_decision,
        jd_overlap_score=jd_overlap_score,
    )


__all__ = [
    "PhaseDecision",
    "ReusePlan",
    "compute_reuse_plan",
    "no_reuse_plan",
    "should_skip_phase",
    "write_reuse_plan_artifact",
]
