"""Tests for jobsmith.config — JobsmithConfig Pydantic loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobsmith.config import (
    AnchorThresholds,
    JobsmithConfig,
    find_config,
    load_config,
)


def test_default_config_loads() -> None:
    """Empty config → all defaults apply."""
    config = JobsmithConfig()
    assert config.master.work_yml == Path("assets/content/work.yml")
    assert config.anchor_thresholds.money_min_usd == 10_000_000
    assert config.cover_letter.framework == "careerfair-io"


def test_load_config_from_file(tmp_path: Path) -> None:
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "user": {"name": "Pat Doe", "email": "pat@example.com"},
                "anchor_thresholds": {"money_min_usd": 5_000_000},
            }
        )
    )
    config = load_config(config_file)
    assert config.user.name == "Pat Doe"
    assert config.user.email == "pat@example.com"
    assert config.anchor_thresholds.money_min_usd == 5_000_000
    # Unset fields fall back to defaults
    assert config.anchor_thresholds.percent_min == 50.0


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nonexistent.yaml")
    assert config == JobsmithConfig()


def test_find_config_walks_up(tmp_path: Path) -> None:
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("user:\n  name: Pat\n")
    nested = tmp_path / "deep" / "nested" / "dir"
    nested.mkdir(parents=True)
    found = find_config(nested)
    assert found == config_file


def test_find_config_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_config(tmp_path) is None


def test_anchor_threshold_percent_validation() -> None:
    """Percent must be 0-100."""
    with pytest.raises(ValueError, match="percent_min must be 0-100"):
        JobsmithConfig.model_validate(
            {"anchor_thresholds": {"percent_min": 150.0}}
        )


def test_voice_settings_default_banned_lists() -> None:
    config = JobsmithConfig()
    assert "Architected" in config.voice.banned_action_verbs
    assert "enterprise" in config.voice.banned_buzzwords
    assert "perfect fit" in config.voice.banned_marketer_phrases


def test_user_identity_optional() -> None:
    """User fields default to empty strings — never None — for safe templating."""
    config = JobsmithConfig()
    assert config.user.name == ""
    assert config.user.email == ""
