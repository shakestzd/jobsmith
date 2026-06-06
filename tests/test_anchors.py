"""Tests for jobsmith.anchors — regex library and anchor extraction."""

from __future__ import annotations

import pytest

from jobsmith.anchors import (
    DEFAULT_ASSET_COUNT_THRESHOLD,
    DEFAULT_MONEY_THRESHOLD_USD,
    DEFAULT_PERCENT_THRESHOLD,
    extract_anchors,
    is_anchor,
    parse_asset_count,
    parse_money_to_usd,
    parse_percent,
)
from jobsmith.guard import Bullet

# ---------- parse_money_to_usd ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$250M", 250_000_000),
        ("$1B", 1_000_000_000),
        ("$50.5K", 50_500),
        ("$120,000", 120_000),
        ("$132", 132),
        ("$4.25B", 4_250_000_000),
    ],
)
def test_parse_money_to_usd_examples(raw: str, expected: float) -> None:
    assert parse_money_to_usd(raw) == expected


def test_parse_money_to_usd_invalid_returns_none() -> None:
    assert parse_money_to_usd("not money") is None


# ---------- parse_percent ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("75%", 75.0),
        ("99.9%", 99.9),
        ("100%", 100.0),
        ("0.5%", 0.5),
    ],
)
def test_parse_percent_examples(raw: str, expected: float) -> None:
    assert parse_percent(raw) == expected


def test_parse_percent_invalid_returns_none() -> None:
    assert parse_percent("not a percent") is None


# ---------- parse_asset_count ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("200K", 200_000),
        ("1.5M", 1_500_000),
        ("500K", 500_000),
        ("3", 3),
    ],
)
def test_parse_asset_count_examples(raw: str, expected: int) -> None:
    assert parse_asset_count(raw) == expected


# ---------- extract_anchors ----------


def test_extract_anchors_money_above_threshold() -> None:
    text = "Unlocked $250M in additional Investment Tax Credits"
    anchors = extract_anchors(text)
    assert len(anchors) == 1
    assert anchors[0].kind == "money"
    assert anchors[0].raw == "$250M"
    assert anchors[0].value == 250_000_000


def test_extract_anchors_money_below_threshold_excluded() -> None:
    text = "Recovered $5K in dealer fees"  # below $10M default
    anchors = extract_anchors(text)
    assert anchors == []


def test_extract_anchors_percent_above_threshold() -> None:
    text = "Reduced accounts payable processing time by 75%"
    anchors = extract_anchors(text)
    assert len(anchors) == 1
    assert anchors[0].kind == "percent"
    assert anchors[0].value == 75.0


def test_extract_anchors_percent_below_threshold_excluded() -> None:
    text = "Achieved 12% cost reduction"  # below 50% default
    anchors = extract_anchors(text)
    assert anchors == []


def test_extract_anchors_asset_count_above_threshold() -> None:
    text = "Built data infrastructure for 500K solar assets"
    anchors = extract_anchors(text)
    assert len(anchors) == 1
    assert anchors[0].kind == "asset_count"
    assert anchors[0].value == 500_000.0


def test_extract_anchors_money_count_distinguished() -> None:
    """Asset-count regex must NOT match dollar-count phrases like $230K project."""
    text = "Awarded $230K project funding"
    anchors = extract_anchors(text)
    # $230K is below money threshold AND should not match asset_count regex.
    assert anchors == []


def test_extract_anchors_multiple_in_one_bullet() -> None:
    text = "Recovered $95M in dealer revenue: $91M via dashboards, $4M via reconciliation"
    anchors = extract_anchors(text)
    money_anchors = [a for a in anchors if a.kind == "money"]
    # $95M and $91M are above $10M; $4M is below.
    assert len(money_anchors) == 2
    assert {a.raw for a in money_anchors} == {"$95M", "$91M"}


def test_extract_anchors_custom_thresholds() -> None:
    text = "Saved $5M in operating costs and improved efficiency by 25%"
    anchors = extract_anchors(text, money_threshold=1_000_000, percent_threshold=20.0)
    # With lower thresholds both should anchor.
    kinds = {a.kind for a in anchors}
    assert "money" in kinds
    assert "percent" in kinds


def test_default_thresholds() -> None:
    """The default thresholds should match the published constants."""
    assert DEFAULT_MONEY_THRESHOLD_USD == 10_000_000
    assert DEFAULT_PERCENT_THRESHOLD == 50.0
    assert DEFAULT_ASSET_COUNT_THRESHOLD == 100_000


# ---------- is_anchor ----------


def _make_bullet(text: str, anchor_explicit: bool | None = None) -> Bullet:
    """Helper: create a minimal Bullet for is_anchor tests."""
    return Bullet(
        bullet_id="test000000",
        text=text,
        company="TestCo",
        position_title="Test Title",
        position_index=0,
        bullet_index=0,
        anchor_explicit=anchor_explicit,
    )


def test_is_anchor_explicit_true_overrides_no_metric() -> None:
    """anchor_explicit=True wins even when text has no regex-detectable metric."""
    bullet = _make_bullet("Improved team dynamics and communication", anchor_explicit=True)
    assert is_anchor(bullet) is True


def test_is_anchor_explicit_false_overrides_regex_match() -> None:
    """anchor_explicit=False wins even when text would match the regex."""
    bullet = _make_bullet("Cut waste by $50M", anchor_explicit=False)
    assert is_anchor(bullet) is False


def test_is_anchor_string_form_falls_through_to_regex() -> None:
    """anchor_explicit=None (string-form bullet) falls through to regex detection."""
    bullet = _make_bullet("Cut by 75%", anchor_explicit=None)
    assert is_anchor(bullet) is True


# Roborev fix (job 918): Bullet.is_anchor PROPERTY must also honor anchor_explicit
# (the standalone is_anchor function did, but the property was still bool(self.anchors))


def test_bullet_is_anchor_property_explicit_true_overrides_no_metric() -> None:
    """Bullet.is_anchor property: anchor_explicit=True wins even with no metric."""
    bullet = _make_bullet("Improved team dynamics", anchor_explicit=True)
    assert bullet.is_anchor is True


def test_bullet_is_anchor_property_explicit_false_overrides_regex_match() -> None:
    """Bullet.is_anchor property: anchor_explicit=False wins even when text matches regex."""
    from jobsmith.anchors import Anchor
    bullet = _make_bullet("Cut waste by $50M", anchor_explicit=False)
    bullet.anchors = [Anchor(kind="money", raw="$50M", value=50_000_000.0)]
    assert bullet.is_anchor is False, (
        "Property must honor anchor_explicit=False even when bullet.anchors is non-empty"
    )
