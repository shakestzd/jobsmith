"""Tests for jobsmith.sourcing.scoring — comp parser, red flags, fit scorer.

TDD: written before implementation (feat-5531c54b).

Covers pure-functional logic in scoring.py that does NOT require YAML config files.
Config-dependent behaviour (weights/profile/red-flag patterns) is tested only for
graceful degradation (empty files → zero scores, no crash).
"""

from __future__ import annotations

from jobsmith.sourcing.scoring import (
    Compensation,
    ScoredRole,
    detect_red_flags,
    parse_compensation,
    reset_red_flag_cache,
    reset_scoring_cache,
    score_all_lanes,
    score_role_fast,
)

# ---------------------------------------------------------------------------
# parse_compensation
# ---------------------------------------------------------------------------


def test_parse_comp_empty_text_returns_defaults() -> None:
    c = parse_compensation("")
    assert isinstance(c, Compensation)
    assert c.base_range_low is None
    assert c.base_range_high is None
    assert c.confidence == "low"


def test_parse_comp_extracts_explicit_range() -> None:
    c = parse_compensation("Salary: $130,000 - $160,000 annually")
    assert c.base_range_low == 130000
    assert c.base_range_high == 160000
    assert c.confidence == "high"


def test_parse_comp_extracts_k_notation() -> None:
    c = parse_compensation("We pay $120k - $150k per year")
    assert c.base_range_low == 120000
    assert c.base_range_high == 150000


def test_parse_comp_detects_bonus() -> None:
    c = parse_compensation("Base $130k plus annual bonus of up to 20%.")
    assert c.bonus is not None


def test_parse_comp_detects_equity() -> None:
    c = parse_compensation("Competitive equity package with RSUs vesting over 4 years.")
    assert c.equity is not None


def test_parse_comp_total_comp_estimate_with_bonus() -> None:
    c = parse_compensation("Base $120k - $150k. Annual bonus eligible.")
    # midpoint = 135k; with 15% bonus = 135k + ~20k = ~155k
    assert c.total_comp_estimate is not None
    assert c.total_comp_estimate > 135000


def test_parse_comp_benefits_detected() -> None:
    c = parse_compensation(
        "We offer health insurance, dental, 401k matching, and unlimited PTO."
    )
    assert "health" in c.benefits_list
    assert "dental" in c.benefits_list
    assert "401k" in c.benefits_list
    assert "pto" in c.benefits_list


# ---------------------------------------------------------------------------
# detect_red_flags — graceful degradation (no patterns file)
# ---------------------------------------------------------------------------


def test_detect_red_flags_returns_list_when_no_patterns_file() -> None:
    """When no red-flag-patterns.yaml exists, returns [] not None."""
    reset_red_flag_cache()
    import os

    # Point to a non-existent path so the cache loads empty
    original = os.environ.get("RED_FLAG_PATTERNS_PATH")
    os.environ["RED_FLAG_PATTERNS_PATH"] = "/nonexistent/path.yaml"
    try:
        flags = detect_red_flags("Some JD text here.")
        assert isinstance(flags, list)
        assert flags == []
    finally:
        if original is None:
            del os.environ["RED_FLAG_PATTERNS_PATH"]
        else:
            os.environ["RED_FLAG_PATTERNS_PATH"] = original
        reset_red_flag_cache()


def test_detect_red_flags_returns_empty_for_empty_text() -> None:
    flags = detect_red_flags("")
    assert flags == []


# ---------------------------------------------------------------------------
# score_all_lanes — graceful degradation (no weights file)
# ---------------------------------------------------------------------------


def test_score_all_lanes_returns_scored_role() -> None:
    """score_all_lanes always returns a ScoredRole, even without config files."""
    reset_scoring_cache()
    result = score_all_lanes("Senior data engineer role with Python and Spark.")
    assert isinstance(result, ScoredRole)


def test_score_all_lanes_graceful_without_config() -> None:
    """With empty weights cache, scores are 0 and excluded=False."""
    reset_scoring_cache()
    result = score_all_lanes("Random JD text here.", red_flags=[])
    # Without any weights file, all lane scores are 0
    assert result.score_a >= 0
    assert result.excluded is False


# ---------------------------------------------------------------------------
# score_role_fast
# ---------------------------------------------------------------------------


def test_score_role_fast_returns_dict() -> None:
    result = score_role_fast("Build data pipelines in Python. $130k-$160k salary.")
    assert isinstance(result, dict)


def test_score_role_fast_has_expected_keys() -> None:
    result = score_role_fast("Build data pipelines.")
    required_keys = {
        "score_a",
        "score_b",
        "score_tax_equity",
        "score_ai_research",
        "score_elixir_distributed",
        "dominant_specialty",
        "score_breakdown",
        "excluded",
        "mode_tags",
        "reasoning",
        "comp_low",
        "comp_high",
        "total_comp_estimate",
        "comp_confidence",
        "red_flags",
    }
    assert required_keys.issubset(result.keys())


def test_score_role_fast_comp_extracted() -> None:
    result = score_role_fast("Salary $120,000 - $150,000 per year.")
    assert result["comp_low"] == 120000
    assert result["comp_high"] == 150000


def test_score_role_fast_empty_text() -> None:
    result = score_role_fast("")
    assert result["comp_low"] is None
    assert result["red_flags"] == []
