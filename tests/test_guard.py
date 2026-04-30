"""Tests for jobsmith.guard — anchor-bullet preservation guardrail."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from jobsmith.guard import (
    Bullet,
    GuardResult,
    _bullet_id,
    check_anchors,
    find_anchors_in_text,
    parse_master_bullets,
)


def _write_master(path: Path, positions: list[dict]) -> None:
    path.write_text(yaml.safe_dump(positions))


def _write_selection(path: Path, positions: list[dict]) -> None:
    path.write_text(json.dumps({"positions": positions}))


# ---------- find_anchors_in_text ----------


def test_find_anchors_picks_up_money_above_threshold() -> None:
    anchors = find_anchors_in_text("Unlocked $250M in tax credits")
    assert len(anchors) == 1
    assert anchors[0].kind == "money"


def test_find_anchors_skips_money_below_threshold() -> None:
    assert find_anchors_in_text("Saved $5K on cloud costs") == []


def test_find_anchors_picks_up_percent_above_threshold() -> None:
    anchors = find_anchors_in_text("Reduced AP processing time by 75%")
    assert len(anchors) == 1
    assert anchors[0].kind == "percent"


def test_find_anchors_picks_up_asset_count_above_threshold() -> None:
    anchors = find_anchors_in_text("Built infrastructure for 500K solar assets")
    assert len(anchors) == 1
    assert anchors[0].kind == "asset_count"


def test_find_anchors_multiple_in_one_bullet() -> None:
    text = "Recovered $95M and $91M; reduced AP by 75% across 200K assets"
    anchors = find_anchors_in_text(text)
    kinds = [a.kind for a in anchors]
    assert kinds.count("money") == 2
    assert kinds.count("percent") == 1
    assert kinds.count("asset_count") == 1


# ---------- parse_master_bullets ----------


def test_parse_master_bullets_returns_typed_bullets(tmp_path: Path) -> None:
    master = tmp_path / "work.yml"
    _write_master(
        master,
        [
            {
                "title": "Senior Data Engineer",
                "location": "Helios",
                "details": [
                    "Unlocked $250M in tax credits",
                    "Reduced AP by 75%",
                    "Wrote a small Python utility",  # not an anchor
                ],
            }
        ],
    )
    bullets = parse_master_bullets(master)
    assert len(bullets) == 3
    assert all(isinstance(b, Bullet) for b in bullets)
    anchor_bullets = [b for b in bullets if b.is_anchor]
    assert len(anchor_bullets) == 2


def test_parse_master_bullets_rejects_non_list_root(tmp_path: Path) -> None:
    master = tmp_path / "work.yml"
    master.write_text("not_a_list: true")
    with pytest.raises(ValueError, match="must be a list"):
        parse_master_bullets(master)


def test_bullet_id_is_stable() -> None:
    text = "Unlocked $250M in tax credits"
    assert _bullet_id(text) == _bullet_id(text)
    assert len(_bullet_id(text)) == 12


# ---------- check_anchors ----------


def test_check_anchors_returns_kept_when_no_selection_yet(tmp_path: Path) -> None:
    """No selection file = selector hasn't run; all anchors preserved by default."""
    master = tmp_path / "work.yml"
    _write_master(
        master,
        [
            {
                "title": "Engineer",
                "location": "Co",
                "details": ["Unlocked $250M in tax credits"],
            }
        ],
    )
    result = check_anchors(
        master_path=master,
        selection_path=tmp_path / "nonexistent.json",
    )
    assert result.exit_code == 0
    assert len(result.kept) == 1
    assert result.dropped_without_reason == []


def test_check_anchors_anchor_dropped_without_reason_returns_exit_1(tmp_path: Path) -> None:
    master = tmp_path / "work.yml"
    text = "Unlocked $250M in tax credits"
    _write_master(
        master,
        [{"title": "Eng", "location": "Co", "details": [text]}],
    )
    selection = tmp_path / "selection.json"
    _write_selection(
        selection,
        [
            {
                "company": "Co",
                "title": "Eng",
                "bullets": [
                    {
                        "master_bullet_id": _bullet_id(text),
                        "included": False,
                        "rephrased": None,
                        "reason_if_dropped": None,
                    }
                ],
            }
        ],
    )
    result = check_anchors(master_path=master, selection_path=selection)
    assert result.exit_code == 1
    assert len(result.dropped_without_reason) == 1


def test_check_anchors_anchor_dropped_with_reason_returns_exit_0(tmp_path: Path) -> None:
    master = tmp_path / "work.yml"
    text = "Unlocked $250M in tax credits"
    _write_master(
        master,
        [{"title": "Eng", "location": "Co", "details": [text]}],
    )
    selection = tmp_path / "selection.json"
    _write_selection(
        selection,
        [
            {
                "company": "Co",
                "title": "Eng",
                "bullets": [
                    {
                        "master_bullet_id": _bullet_id(text),
                        "included": False,
                        "rephrased": None,
                        "reason_if_dropped": "Off-thesis for this JD",
                    }
                ],
            }
        ],
    )
    result = check_anchors(master_path=master, selection_path=selection)
    assert result.exit_code == 0
    assert len(result.dropped_with_reason) == 1
    assert result.dropped_with_reason[0][1] == "Off-thesis for this JD"


def test_check_anchors_pending_inquiry_is_not_a_valid_reason(tmp_path: Path) -> None:
    """A 'pending-inquiry' marker means the inquirer is mid-flight, not resolved."""
    master = tmp_path / "work.yml"
    text = "Unlocked $250M in tax credits"
    _write_master(
        master,
        [{"title": "Eng", "location": "Co", "details": [text]}],
    )
    selection = tmp_path / "selection.json"
    _write_selection(
        selection,
        [
            {
                "company": "Co",
                "title": "Eng",
                "bullets": [
                    {
                        "master_bullet_id": _bullet_id(text),
                        "included": False,
                        "rephrased": None,
                        "reason_if_dropped": "pending-inquiry",
                    }
                ],
            }
        ],
    )
    result = check_anchors(master_path=master, selection_path=selection)
    assert result.exit_code == 1


def test_check_anchors_kept_when_included_true(tmp_path: Path) -> None:
    master = tmp_path / "work.yml"
    text = "Unlocked $250M in tax credits"
    _write_master(
        master,
        [{"title": "Eng", "location": "Co", "details": [text]}],
    )
    selection = tmp_path / "selection.json"
    _write_selection(
        selection,
        [
            {
                "company": "Co",
                "title": "Eng",
                "bullets": [
                    {
                        "master_bullet_id": _bullet_id(text),
                        "included": True,
                        "rephrased": None,
                        "reason_if_dropped": None,
                    }
                ],
            }
        ],
    )
    result = check_anchors(master_path=master, selection_path=selection)
    assert result.exit_code == 0
    assert len(result.kept) == 1
    assert isinstance(result, GuardResult)
