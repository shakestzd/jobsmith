"""Tests for jobsmith.reuse.warmstart (feat-13a76665 / slice-7).

TDD: tests written to specify behavior.

Covers:
  Unit:
    - test_requirement_delta_computation
    - test_anchors_carried_forward

  Integration:
    - test_warmstart_regenerates_only_delta
    - test_uncovered_requirement_escalates
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bullet_selection(bullets: list[dict]) -> dict:
    """Wrap a flat bullet list in the canonical bullet-selection.json shape."""
    return {
        "positions": [
            {
                "company": "Acme",
                "title": "Engineer",
                "bullets": bullets,
            }
        ]
    }


def _make_state_dir(
    tmp_path: Path,
    slug: str = "prior-slug",
    selection: dict | None = None,
    prose_draft: str | None = None,
) -> Path:
    """Create a minimal .apply-state/ directory under tmp_path/<slug>/."""
    state_dir = tmp_path / slug / ".apply-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    if selection is not None:
        (state_dir / "bullet-selection.json").write_text(
            json.dumps(selection), encoding="utf-8"
        )
    if prose_draft is not None:
        (state_dir / "prose-draft.md").write_text(prose_draft, encoding="utf-8")
    return state_dir


# ---------------------------------------------------------------------------
# Unit — test_requirement_delta_computation
# ---------------------------------------------------------------------------


class TestRequirementDeltaComputation:
    """compute_warm_start correctly partitions new requirements into
    covered (via bullet_map) vs delta (must regenerate)."""

    def test_requirement_delta_computation(self, tmp_path: Path) -> None:
        """Requirements NOT in bullet_map end up in delta_requirement_hashes."""
        from jobsmith.reuse.warmstart import compute_warm_start

        prior_state = _make_state_dir(
            tmp_path,
            selection=_make_bullet_selection(
                [
                    {
                        "master_bullet_id": "aabbcc112233",
                        "text": "Built ETL pipeline processing 1M records/day",
                        "included": True,
                        "rephrased": None,
                    }
                ]
            ),
        )

        # Two requirements: one covered, one new
        covered_hash = "hash_covered_001"
        new_hash = "hash_new_002"
        bullet_map = {covered_hash: "aabbcc112233"}

        result = compute_warm_start(
            prior_state_dir=prior_state,
            current_requirement_hashes=[covered_hash, new_hash],
            bullet_map=bullet_map,
        )

        assert covered_hash not in result.delta_requirement_hashes, (
            "Covered requirement should NOT be in delta"
        )
        assert new_hash in result.delta_requirement_hashes, (
            "New requirement MUST be in delta"
        )
        assert "aabbcc112233" in result.reused_bullet_ids, (
            "Covered bullet_id should be in reused_bullet_ids"
        )

    def test_all_covered_means_empty_delta(self, tmp_path: Path) -> None:
        """When all requirements are in bullet_map, delta is empty."""
        from jobsmith.reuse.warmstart import compute_warm_start

        prior_state = _make_state_dir(tmp_path)
        bullet_map = {"hash_a": "bid_a", "hash_b": "bid_b"}

        result = compute_warm_start(
            prior_state_dir=prior_state,
            current_requirement_hashes=["hash_a", "hash_b"],
            bullet_map=bullet_map,
        )

        assert result.delta_requirement_hashes == []

    def test_empty_bullet_map_all_delta(self, tmp_path: Path) -> None:
        """When bullet_map is empty, all requirements are in delta."""
        from jobsmith.reuse.warmstart import compute_warm_start

        prior_state = _make_state_dir(tmp_path)

        result = compute_warm_start(
            prior_state_dir=prior_state,
            current_requirement_hashes=["h1", "h2", "h3"],
            bullet_map={},
        )

        assert set(result.delta_requirement_hashes) == {"h1", "h2", "h3"}


# ---------------------------------------------------------------------------
# Unit — test_anchors_carried_forward
# ---------------------------------------------------------------------------


class TestAnchorsCarriedForward:
    """Anchor bullets from the base are ALWAYS carried forward verbatim
    and excluded from the delta."""

    def test_anchors_carried_forward(self, tmp_path: Path) -> None:
        """Anchor bullet (via regex) appears in anchors_carried, NOT in delta."""
        from jobsmith.reuse.warmstart import compute_warm_start

        # Anchor bullet: has a $10M+ metric
        anchor_bullet = {
            "master_bullet_id": "anchor_id_001",
            "text": "Led $250M portfolio restructuring across 12 regions",
            "included": True,
            "rephrased": "Led $250M portfolio restructuring across 12 regions",
        }
        non_anchor_bullet = {
            "master_bullet_id": "regular_id_002",
            "text": "Wrote documentation for internal wiki",
            "included": True,
            "rephrased": None,
        }

        prior_state = _make_state_dir(
            tmp_path,
            selection=_make_bullet_selection([anchor_bullet, non_anchor_bullet]),
        )

        result = compute_warm_start(
            prior_state_dir=prior_state,
            current_requirement_hashes=["h1"],
            bullet_map={},
        )

        anchor_ids = [b.get("master_bullet_id") for b in result.anchors_carried]
        assert "anchor_id_001" in anchor_ids, "Anchor bullet must be in anchors_carried"
        assert "regular_id_002" not in anchor_ids, "Non-anchor should not be in anchors_carried"

    def test_explicit_anchor_flag_wins(self, tmp_path: Path) -> None:
        """Bullet with anchor_explicit=True is an anchor even without metrics."""
        from jobsmith.reuse.warmstart import compute_warm_start

        bullet = {
            "master_bullet_id": "explicit_anchor",
            "text": "Wrote unit tests for payment module",
            "included": True,
            "anchor_explicit": True,
        }

        prior_state = _make_state_dir(
            tmp_path,
            selection=_make_bullet_selection([bullet]),
        )

        result = compute_warm_start(
            prior_state_dir=prior_state,
            current_requirement_hashes=[],
            bullet_map={},
        )

        anchor_ids = [b.get("master_bullet_id") for b in result.anchors_carried]
        assert "explicit_anchor" in anchor_ids

    def test_explicit_anchor_false_excludes_metric_bullet(self, tmp_path: Path) -> None:
        """Bullet with anchor_explicit=False is NOT an anchor even with $10M+ metric."""
        from jobsmith.reuse.warmstart import compute_warm_start

        bullet = {
            "master_bullet_id": "suppressed_anchor",
            "text": "Managed $50M budget for cloud infrastructure",
            "included": True,
            "anchor_explicit": False,
        }

        prior_state = _make_state_dir(
            tmp_path,
            selection=_make_bullet_selection([bullet]),
        )

        result = compute_warm_start(
            prior_state_dir=prior_state,
            current_requirement_hashes=[],
            bullet_map={},
        )

        anchor_ids = [b.get("master_bullet_id") for b in result.anchors_carried]
        assert "suppressed_anchor" not in anchor_ids

    def test_dropped_bullets_not_in_anchors(self, tmp_path: Path) -> None:
        """Bullets with included=False are not carried forward as anchors."""
        from jobsmith.reuse.warmstart import compute_warm_start

        bullet = {
            "master_bullet_id": "dropped_anchor",
            "text": "Reduced costs by $15M via vendor renegotiation",
            "included": False,  # dropped
        }

        prior_state = _make_state_dir(
            tmp_path,
            selection=_make_bullet_selection([bullet]),
        )

        result = compute_warm_start(
            prior_state_dir=prior_state,
            current_requirement_hashes=[],
            bullet_map={},
        )

        anchor_ids = [b.get("master_bullet_id") for b in result.anchors_carried]
        assert "dropped_anchor" not in anchor_ids


# ---------------------------------------------------------------------------
# Integration — test_uncovered_requirement_escalates
# ---------------------------------------------------------------------------


class TestUncoveredRequirementEscalates:
    """Requirements in the delta (no bullet_map entry) are escalated for
    full agent generation rather than fabrication."""

    def test_uncovered_requirement_escalates(self, tmp_path: Path) -> None:
        """Delta requirements (uncovered) appear in escalated_requirement_hashes."""
        from jobsmith.reuse.warmstart import compute_warm_start

        prior_state = _make_state_dir(tmp_path)

        # No bullet_map entry for these — both are uncovered
        result = compute_warm_start(
            prior_state_dir=prior_state,
            current_requirement_hashes=["uncovered_h1", "uncovered_h2"],
            bullet_map={},
        )

        assert "uncovered_h1" in result.escalated_requirement_hashes
        assert "uncovered_h2" in result.escalated_requirement_hashes

    def test_covered_requirements_not_escalated(self, tmp_path: Path) -> None:
        """Requirements with bullet_map entries are NOT escalated."""
        from jobsmith.reuse.warmstart import compute_warm_start

        prior_state = _make_state_dir(tmp_path)
        bullet_map = {"covered_h1": "some_bullet_id"}

        result = compute_warm_start(
            prior_state_dir=prior_state,
            current_requirement_hashes=["covered_h1"],
            bullet_map=bullet_map,
        )

        assert "covered_h1" not in result.escalated_requirement_hashes


# ---------------------------------------------------------------------------
# Integration — test_warmstart_regenerates_only_delta
# ---------------------------------------------------------------------------


class TestWarmstartRegeneratesOnlyDelta:
    """Pipeline in warm-start mode appends delta context to the draft prompt,
    dispatched through the existing agent path (run_phase_iter)."""

    def test_warmstart_regenerates_only_delta(self, tmp_path: Path) -> None:
        """When reuse-plan.json says warm-start, the draft prompt includes
        the warm-start delta context (not present in regenerate mode)."""
        from jobsmith.core.pipeline import (
            _build_warmstart_prompt_suffix,
            _load_reuse_plan_from_state,
        )
        from jobsmith.reuse.store import content_hash

        # Set up a prior app with bullet-selection
        prior_slug = "prior-app-2024-01"
        prior_state = _make_state_dir(
            tmp_path,
            slug=prior_slug,
            selection=_make_bullet_selection(
                [
                    {
                        "master_bullet_id": "bid_existing",
                        "text": "Optimized SQL queries reducing latency by 60%",
                        "included": True,
                    }
                ]
            ),
            prose_draft="# Prior draft\n- bullet one",
        )

        # Set up current app with reuse-plan.json
        current_slug = "current-app-2024-02"
        current_state = tmp_path / current_slug / ".apply-state"
        current_state.mkdir(parents=True, exist_ok=True)

        covered_req = "Python proficiency"
        covered_hash = content_hash(covered_req)
        new_req = "Kubernetes experience"
        new_hash = content_hash(new_req)

        jd_parsed = {
            "company": "TechCo",
            "must_haves": [{"raw": covered_req}, {"raw": new_req}],
            "nice_to_haves": [],
        }
        (current_state / "jd-parsed.json").write_text(json.dumps(jd_parsed))

        reuse_plan = {
            "draft": {"decision": "warm-start", "source": prior_slug, "score": 0.85},
            "matched_slug": prior_slug,
            "bullet_map": {covered_hash: "bid_existing"},
        }
        (current_state / "reuse-plan.json").write_text(json.dumps(reuse_plan))

        # Stub the applications_dir to return tmp_path
        with patch(
            "jobsmith.core.pipeline.applications_dir",
            return_value=tmp_path,
        ):
            loaded = _load_reuse_plan_from_state(current_state)
            assert loaded is not None
            assert loaded["draft"]["decision"] == "warm-start"

            suffix = _build_warmstart_prompt_suffix(
                current_slug, tmp_path, loaded
            )

        assert "Warm-start mode" in suffix, "Warm-start suffix must include header"
        assert prior_slug in suffix, "Prior slug must appear in suffix"
        # New (uncovered) requirement should be in delta
        assert new_hash in suffix or "Delta requirement hashes" in suffix, (
            "Delta requirements must appear in warm-start suffix"
        )

    def test_no_warmstart_for_regenerate_plan(self, tmp_path: Path) -> None:
        """When plan says regenerate, no warm-start suffix is produced."""
        from jobsmith.core.pipeline import _load_reuse_plan_from_state

        state_dir = tmp_path / "some-app" / ".apply-state"
        state_dir.mkdir(parents=True, exist_ok=True)

        reuse_plan = {
            "draft": {"decision": "regenerate", "source": None, "score": 0.0},
            "matched_slug": None,
            "bullet_map": {},
        }
        (state_dir / "reuse-plan.json").write_text(json.dumps(reuse_plan))

        loaded = _load_reuse_plan_from_state(state_dir)
        assert loaded is not None
        assert loaded["draft"]["decision"] == "regenerate"
        # The pipeline should NOT apply warm-start for regenerate decisions
        decision = (loaded.get("draft") or {}).get("decision")
        assert decision != "warm-start"

    def test_missing_reuse_plan_returns_none(self, tmp_path: Path) -> None:
        """_load_reuse_plan_from_state returns None when file is absent."""
        from jobsmith.core.pipeline import _load_reuse_plan_from_state

        state_dir = tmp_path / "no-plan" / ".apply-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        # No reuse-plan.json written

        result = _load_reuse_plan_from_state(state_dir)
        assert result is None
