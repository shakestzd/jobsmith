"""jobsmith.reuse.warmstart — warm-start delta engine for the draft phase.

When the reuse planner returns ``plan.draft.decision == "warm-start"``, this
module computes what MUST change vs what can be carried forward, producing a
:class:`WarmStartResult` that the pipeline uses to short-circuit the draft
phase to modify only the delta bullets.

Design decisions
----------------
- **Anchors are sacred**: anchor bullets from the base are copied VERBATIM
  into the output and EXCLUDED from the diff/regenerate set, regardless of
  JD conflict.  A JD conflict is surfaced in ``anchor_conflicts`` for the
  report (slice 9) but NEVER causes an anchor to be dropped or rewritten.
- **Delta = new requirements only**: any requirement in the new JD whose
  ``requirement_hash`` is already covered by a fresh ``bullet_map`` entry
  from the evidence map is NOT in the delta.  Only truly new requirements
  drive regeneration.
- **Escalation over fabrication**: if a requirement is in the delta (i.e.,
  no prior bullet covers it) AND the base resume has no bullet that plausibly
  covers it, we escalate that requirement to full generation via the agent.
  We NEVER fabricate.  Escalation is surface-level — we list the requirement
  hash in ``escalated_requirement_hashes`` and let the agent handle it.
- Slice 8 (backstop) re-gates the final output.  We don't add the final gate
  here; we just don't fabricate.

Public API
----------
``WarmStartResult`` — dataclass: base, anchors_carried, delta_requirements,
                      reused_bullets, escalated_requirement_hashes,
                      anchor_conflicts
``compute_warm_start(plan, *, prior_state_dir, current_requirement_hashes,
                     master_bullets) -> WarmStartResult``
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class WarmStartResult:
    """Result produced by :func:`compute_warm_start`.

    Attributes
    ----------
    base_bullet_selection:
        The bullet-selection.json loaded from the prior application's
        .apply-state/ directory.  Empty dict if unavailable.
    base_prose_draft:
        The prose-draft.md text from the prior application, or None.
    anchors_carried:
        List of bullet records from ``base_bullet_selection`` that are
        anchor bullets.  These are copied verbatim and excluded from the
        regenerate set.
    delta_requirement_hashes:
        Requirement hashes from the new JD that are NOT covered by a
        fresh ``bullet_map`` entry.  These drive regeneration.
    reused_bullet_ids:
        master_bullet_id values whose evidence-map entry covers a
        new-JD requirement (fresh; no rewrite needed).
    escalated_requirement_hashes:
        Requirement hashes that are in the delta AND have no plausible
        coverage from the base selection — full agent generation required.
    anchor_conflicts:
        List of ``(anchor_bullet_id, conflicting_requirement_hash)`` pairs
        where a JD requirement semantically overlaps an anchor bullet
        but the anchor is still KEPT (non-negotiable).  Surfaced for
        the report only.
    """

    base_bullet_selection: dict[str, Any] = field(default_factory=dict)
    base_prose_draft: str | None = None
    anchors_carried: list[dict[str, Any]] = field(default_factory=list)
    delta_requirement_hashes: list[str] = field(default_factory=list)
    reused_bullet_ids: list[str] = field(default_factory=list)
    escalated_requirement_hashes: list[str] = field(default_factory=list)
    anchor_conflicts: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_bullet_selection(state_dir: Path) -> dict[str, Any]:
    """Load bullet-selection.json from *state_dir*; returns {} on missing/error."""
    path = state_dir / "bullet-selection.json"
    if not path.exists():
        logger.debug("warmstart: bullet-selection.json not found at %s", path)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("warmstart: could not load bullet-selection.json: %s", exc)
        return {}


def _load_prose_draft(state_dir: Path) -> str | None:
    """Load prose-draft.md text from *state_dir*; returns None on missing/error."""
    path = state_dir / "prose-draft.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("warmstart: could not load prose-draft.md: %s", exc)
        return None


def _extract_all_bullets(
    selection: dict[str, Any],
) -> list[dict[str, Any]]:
    """Flatten all bullet records from a bullet-selection.json dict."""
    bullets: list[dict[str, Any]] = []
    for position in selection.get("positions", []):
        for bullet in position.get("bullets", []):
            bullets.append(bullet)
    return bullets


def _is_anchor_bullet(bullet: dict[str, Any]) -> bool:
    """Return True when *bullet* is an anchor bullet.

    Checks ``anchor_explicit`` first (user override), then falls back to the
    ``anchors`` list populated by the prose-writer/bullet-selector.  A bullet
    with ``anchor_explicit: true`` is an anchor even without regex metrics;
    ``anchor_explicit: false`` is never an anchor even with metrics.

    When neither field is present, we delegate to the regex detection via
    ``jobsmith.anchors.is_anchor`` against the bullet text.
    """
    # Explicit user flag wins
    anchor_explicit = bullet.get("anchor_explicit")
    if anchor_explicit is not None:
        return bool(anchor_explicit)

    # Selection may carry a pre-computed anchors list from the guard
    anchors_list = bullet.get("anchors")
    if anchors_list is not None:
        return bool(anchors_list)

    # Fall back to regex detection on bullet text
    text = bullet.get("rephrased") or bullet.get("text") or ""
    if not text:
        return False

    try:
        from jobsmith.anchors import extract_anchors

        return bool(extract_anchors(text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("warmstart: anchor detection failed for bullet: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_warm_start(
    *,
    prior_state_dir: Path,
    current_requirement_hashes: list[str],
    bullet_map: dict[str, str],
) -> WarmStartResult:
    """Compute a :class:`WarmStartResult` for a warm-start draft.

    Parameters
    ----------
    prior_state_dir:
        Path to ``<prior-app-dir>/.apply-state/``.  Must contain
        ``bullet-selection.json`` and optionally ``prose-draft.md``.
    current_requirement_hashes:
        ``content_hash`` values for all canonical requirements from the NEW
        JD.  These drive the delta computation.
    bullet_map:
        ``{requirement_hash: master_bullet_id}`` from ``ReusePlan.bullet_map``.
        Requirements whose hash appears here already have a fresh evidence-map
        entry and do NOT need regeneration.

    Returns
    -------
    WarmStartResult
        Fully populated result.  The ``delta_requirement_hashes`` set is the
        core output — it drives what the agent must regenerate.
    """
    result = WarmStartResult()

    # Load prior artifacts
    selection = _load_bullet_selection(prior_state_dir)
    result.base_bullet_selection = selection
    result.base_prose_draft = _load_prose_draft(prior_state_dir)

    # Build the set of requirement hashes that have fresh bullet-map coverage
    covered_hashes: set[str] = set(bullet_map.keys())
    result.reused_bullet_ids = list(dict.fromkeys(bullet_map.values()))

    # Identify delta: new-JD requirements NOT in the bullet_map
    delta: list[str] = []
    for req_hash in current_requirement_hashes:
        if req_hash not in covered_hashes:
            delta.append(req_hash)
    result.delta_requirement_hashes = delta

    # Identify anchor bullets from the base selection
    all_bullets = _extract_all_bullets(selection)
    anchor_bullet_ids: set[str] = set()
    for bullet in all_bullets:
        if not bullet.get("included", True):
            continue  # dropped bullet — not in output
        if _is_anchor_bullet(bullet):
            result.anchors_carried.append(bullet)
            bid = bullet.get("master_bullet_id", "")
            if bid:
                anchor_bullet_ids.add(bid)

    # Anchor carry-forward: anchors are EXCLUDED from delta regardless of JD conflict.
    # Any delta requirement whose only "coverage" was via an anchor bullet is
    # flagged as both a conflict (the anchor wins) and escalated (needs fresh coverage).
    if anchor_bullet_ids:
        # Map anchor bullet IDs to the requirement hashes they supposedly cover
        # (reverse lookup from bullet_map: {bullet_id: [req_hash, ...]})
        bullet_to_reqs: dict[str, list[str]] = {}
        for req_hash, bid in bullet_map.items():
            bullet_to_reqs.setdefault(bid, []).append(req_hash)

        for bid in anchor_bullet_ids:
            covered_by_anchor = bullet_to_reqs.get(bid, [])
            for req_hash in covered_by_anchor:
                # Anchor "covers" a requirement, but the anchor is verbatim — surface
                # as a conflict for the report. If the requirement is still in delta
                # (it won't be, since bullet_map covers it), we'd escalate.
                result.anchor_conflicts.append((bid, req_hash))

    # Escalation: delta requirements with no base-selection coverage
    # These must be fully generated by the agent (never fabricated here).
    # A requirement is escalatable when it's in the delta (no bullet_map entry).
    # ALL delta requirements escalate by default — the agent handles each one.
    result.escalated_requirement_hashes = list(delta)

    logger.info(
        "warmstart: base=%s bullets, anchors=%d, delta=%d reqs, escalated=%d, reused=%d",
        len(all_bullets),
        len(result.anchors_carried),
        len(delta),
        len(result.escalated_requirement_hashes),
        len(result.reused_bullet_ids),
    )

    return result


__all__ = [
    "WarmStartResult",
    "compute_warm_start",
]
