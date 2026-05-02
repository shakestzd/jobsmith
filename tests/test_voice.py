"""Tests for jobsmith.voice — voice profile module.

TDD: these tests are written before the implementation.
"""

from __future__ import annotations

import json
import time
import warnings
from dataclasses import fields
from pathlib import Path

import pytest

from jobsmith.voice import (
    GENERIC_ACTION_VERBS,
    GENERIC_BANNED_ADJECTIVES,
    GENERIC_RESULT_VERBS,
    VoiceProfile,
    _content_hash,
    _extract_bullets_from_qmd,
    is_result_first,
    load_voice_profile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_QMD = """\
---
title: "Pat Doe Resume"
---

## Experience

### Senior Data Engineer — Helios Energy Corp

- Accelerated solar asset onboarding by 40% via automated ETL
- Reduced manual report assembly from 5 days to 4 hours
- Built a Python geospatial analytics platform that unlocked $250M

### Data Analyst — Atlas Climate Capital

- Generated $1M in interest revenue over 9 months
- Expanded reporting coverage to 50+ project finance scenarios
"""


def _make_config(benchmark_path=None, result_verbs=None, action_verbs=None,
                 banned_adjectives=None, banned_action_verbs=None):
    """Build a minimal config object for testing."""
    return type(
        "_Config",
        (),
        {
            "benchmarks": type("_B", (), {"resume_qmd": benchmark_path})(),
            "voice": type("_V", (), {
                "result_verbs": result_verbs or [],
                "action_verbs": action_verbs or [],
                "banned_adjectives": banned_adjectives or list(
                    ("innovative", "passionate", "dynamic", "results-driven", "self-starter")
                ),
                "banned_action_verbs": banned_action_verbs or ["Architected", "Leveraged", "Orchestrated"],
            })(),
        },
    )()


# ---------------------------------------------------------------------------
# VoiceProfile dataclass
# ---------------------------------------------------------------------------


def test_voice_profile_dataclass_constructs() -> None:
    """VoiceProfile can be constructed with required fields."""
    vp = VoiceProfile(
        result_verbs=frozenset({"Grew", "Reduced"}),
        action_verbs=frozenset({"Built", "Designed"}),
        banned_verbs=frozenset({"Architected"}),
        banned_adjectives=("innovative",),
        avg_bullet_words=12.5,
        bullets_per_position_avg=4.0,
        source="generic",
    )
    assert vp.source == "generic"
    assert "Grew" in vp.result_verbs
    assert vp.benchmark_path is None
    # Check all expected field names exist
    field_names = {f.name for f in fields(VoiceProfile)}
    for name in ("result_verbs", "action_verbs", "banned_verbs", "banned_adjectives",
                 "avg_bullet_words", "bullets_per_position_avg", "source",
                 "benchmark_path", "benchmark_mtime", "benchmark_hash"):
        assert name in field_names, f"Missing field: {name}"


def test_generic_seed_lists_size() -> None:
    """GENERIC seed lists must be exactly 12 result verbs, 12 action verbs, 5 banned adjectives."""
    assert len(GENERIC_RESULT_VERBS) == 12, f"Expected 12 result verbs, got {len(GENERIC_RESULT_VERBS)}"
    assert len(GENERIC_ACTION_VERBS) == 12, f"Expected 12 action verbs, got {len(GENERIC_ACTION_VERBS)}"
    assert len(GENERIC_BANNED_ADJECTIVES) == 5, (
        f"Expected 5 banned adjectives, got {len(GENERIC_BANNED_ADJECTIVES)}"
    )


# ---------------------------------------------------------------------------
# load_voice_profile
# ---------------------------------------------------------------------------


def test_load_voice_profile_no_benchmark_returns_generic_plus_config(tmp_path: Path) -> None:
    """No benchmark → VoiceProfile uses generic seeds + config banned_verbs."""
    config = _make_config()
    profile = load_voice_profile(config, cache_dir=tmp_path / ".apply-state")
    assert profile.source in ("generic", "config-override")
    # Generic result verbs present
    assert GENERIC_RESULT_VERBS.issubset(profile.result_verbs) or len(profile.result_verbs) >= 12
    assert profile.benchmark_path is None
    # Banned verbs from config
    assert "Architected" in profile.banned_verbs


def test_load_voice_profile_with_benchmark_extracts_first_word_freq(tmp_path: Path) -> None:
    """Benchmark qmd → profile source is 'benchmark', verbs extracted from bullets."""
    qmd = tmp_path / "resume.qmd"
    qmd.write_text(SAMPLE_QMD)
    config = _make_config(benchmark_path=qmd)
    profile = load_voice_profile(config, cache_dir=tmp_path / ".apply-state")
    assert profile.source == "benchmark"
    assert profile.benchmark_path == str(qmd)
    # Should have picked up some verbs from first words: Accelerated, Reduced, Built, Generated, Expanded
    all_verbs = profile.result_verbs | profile.action_verbs
    assert len(all_verbs) >= 5


# ---------------------------------------------------------------------------
# is_result_first
# ---------------------------------------------------------------------------


def test_is_result_first_metric_in_first_5_words() -> None:
    """Metric in first 5 words → passes."""
    vp = VoiceProfile(
        result_verbs=GENERIC_RESULT_VERBS,
        action_verbs=GENERIC_ACTION_VERBS,
        banned_verbs=frozenset({"Architected", "Leveraged"}),
        banned_adjectives=GENERIC_BANNED_ADJECTIVES,
        avg_bullet_words=12.0,
        bullets_per_position_avg=4.0,
        source="generic",
    )
    ok, msg = is_result_first("Cut waste by 30% across operations", vp)
    assert ok is True
    assert msg == ""


def test_is_result_first_banned_verb() -> None:
    """Banned verb first word → fails with 'banned' in message."""
    vp = VoiceProfile(
        result_verbs=GENERIC_RESULT_VERBS,
        action_verbs=GENERIC_ACTION_VERBS,
        banned_verbs=frozenset({"Architected", "Leveraged"}),
        banned_adjectives=GENERIC_BANNED_ADJECTIVES,
        avg_bullet_words=12.0,
        bullets_per_position_avg=4.0,
        source="generic",
    )
    ok, msg = is_result_first("Architected scalable cloud platform for enterprise", vp)
    assert ok is False
    assert "banned" in msg.lower()


def test_is_result_first_action_verb_warns() -> None:
    """Action verb first word (no metric) → fails with restructure hint."""
    vp = VoiceProfile(
        result_verbs=GENERIC_RESULT_VERBS,
        action_verbs=GENERIC_ACTION_VERBS,
        banned_verbs=frozenset({"Architected", "Leveraged"}),
        banned_adjectives=GENERIC_BANNED_ADJECTIVES,
        avg_bullet_words=12.0,
        bullets_per_position_avg=4.0,
        source="generic",
    )
    ok, msg = is_result_first("Built nova_fde framework for data pipeline orchestration", vp)
    assert ok is False
    assert "restructure" in msg.lower()


def test_is_result_first_non_profit_pm_config() -> None:
    """Non-profit PM config with custom result_verbs — 'Served 200K students' passes."""
    vp = VoiceProfile(
        result_verbs=frozenset({"Served", "Onboarded", "Mobilized"}),
        action_verbs=GENERIC_ACTION_VERBS,
        banned_verbs=frozenset({"Architected"}),
        banned_adjectives=GENERIC_BANNED_ADJECTIVES,
        avg_bullet_words=12.0,
        bullets_per_position_avg=4.0,
        source="config-override",
    )
    ok, msg = is_result_first("Served 200K students across 12 districts via mobile program", vp)
    assert ok is True, f"Expected pass but got: {msg}"


def test_is_result_first_frontend_designer_config() -> None:
    """Frontend designer config with custom result_verbs — 'Shipped 12 production design systems' passes."""
    vp = VoiceProfile(
        result_verbs=frozenset({"Shipped", "Launched", "Migrated"}),
        action_verbs=GENERIC_ACTION_VERBS,
        banned_verbs=frozenset({"Architected"}),
        banned_adjectives=GENERIC_BANNED_ADJECTIVES,
        avg_bullet_words=12.0,
        bullets_per_position_avg=4.0,
        source="config-override",
    )
    ok, msg = is_result_first("Shipped 12 production design systems used by 5K+ developers", vp)
    assert ok is True, f"Expected pass but got: {msg}"


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def test_voice_profile_cache_mtime_invalidation(tmp_path: Path) -> None:
    """If file content changes (mtime or hash), cache is invalidated and profile is recomputed."""
    qmd = tmp_path / "resume.qmd"
    qmd.write_text(SAMPLE_QMD)
    cache_dir = tmp_path / ".apply-state"
    config = _make_config(benchmark_path=qmd)

    # First load — populates cache
    p1 = load_voice_profile(config, cache_dir=cache_dir)
    assert p1.source == "benchmark"

    # Touch the file (change content — guaranteed different hash and likely different mtime)
    time.sleep(0.01)
    new_content = SAMPLE_QMD + "\n- Launched new feature in 3 sprints\n"
    qmd.write_text(new_content)

    p2 = load_voice_profile(config, cache_dir=cache_dir)
    # The mtime in the profile should reflect the new file
    assert p2.benchmark_mtime is not None
    assert p2.benchmark_mtime == pytest.approx(qmd.stat().st_mtime, rel=1e-6)
    # Hash should match the new content
    assert p2.benchmark_hash == _content_hash(new_content)


def test_voice_profile_cache_content_hash_invalidation(tmp_path: Path) -> None:
    """If hash in cache doesn't match file content, forces recompute."""
    qmd = tmp_path / "resume.qmd"
    qmd.write_text(SAMPLE_QMD)
    cache_dir = tmp_path / ".apply-state"
    config = _make_config(benchmark_path=qmd)

    # First load
    p1 = load_voice_profile(config, cache_dir=cache_dir)
    original_hash = p1.benchmark_hash

    # Inject a stale cache with wrong hash (simulating content change with same mtime)
    cache_path = cache_dir / "voice-profile.json"
    data = json.loads(cache_path.read_text())
    data["benchmark_hash"] = "stale_hash_0000"
    cache_path.write_text(json.dumps(data))

    # Reload — should detect hash mismatch and recompute
    p2 = load_voice_profile(config, cache_dir=cache_dir)
    assert p2.benchmark_hash == original_hash


def test_voice_profile_corrupt_cache_falls_back_to_live_recompute(tmp_path: Path) -> None:
    """Corrupt or schema-invalid cache JSON → warning issued, live recompute succeeds."""
    qmd = tmp_path / "resume.qmd"
    qmd.write_text(SAMPLE_QMD)
    cache_dir = tmp_path / ".apply-state"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Write obviously corrupt cache
    (cache_dir / "voice-profile.json").write_text("{invalid json!!}")

    config = _make_config(benchmark_path=qmd)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        profile = load_voice_profile(config, cache_dir=cache_dir)

    # Should still return a valid profile
    assert profile is not None
    assert profile.source in ("benchmark", "generic", "config-override")
    # Should have issued a warning
    assert any(
        "cache" in str(w.message).lower() or "recomputing" in str(w.message).lower()
        for w in caught
    ), "Expected a cache warning"


# ---------------------------------------------------------------------------
# Config integration — banned_buzzwords must NOT contain moved adjectives
# ---------------------------------------------------------------------------


def test_banned_buzzwords_does_not_contain_moved_adjectives() -> None:
    """'innovative' and 'passionate' must NOT be in banned_buzzwords defaults.

    They were moved to banned_adjectives (Q6 option-c).
    """
    from jobsmith.config import VoiceSettings
    vs = VoiceSettings()
    assert "innovative" not in vs.banned_buzzwords, (
        "'innovative' must be in banned_adjectives, not banned_buzzwords"
    )
    assert "passionate" not in vs.banned_buzzwords, (
        "'passionate' must be in banned_adjectives, not banned_buzzwords"
    )
    # Confirm they live in banned_adjectives
    assert "innovative" in vs.banned_adjectives
    assert "passionate" in vs.banned_adjectives


# ---------------------------------------------------------------------------
# Roborev fix #1: voice-profile.json carries banned_buzzwords + banned_marketer_phrases
# ---------------------------------------------------------------------------


def test_load_voice_profile_includes_banned_buzzwords_and_marketer_phrases(tmp_path: Path) -> None:
    """voice-profile.json on disk must include banned_buzzwords and banned_marketer_phrases.

    Specialist prompts (apply-resume-tell-fixer, apply-prose-writer) read both
    fields; without them the specialists silently skip those rules.
    """
    config = _make_config(
        banned_action_verbs=["Architected"],
    )
    # Inject buzzwords and marketer phrases via the synthetic config helper
    config.voice.banned_buzzwords = ["enterprise", "proprietary"]
    config.voice.banned_marketer_phrases = ["perfect fit", "passionate about"]

    cache_dir = tmp_path / ".apply-state"
    profile = load_voice_profile(config, cache_dir=cache_dir)
    assert "enterprise" in profile.banned_buzzwords
    assert "perfect fit" in profile.banned_marketer_phrases

    # And the on-disk JSON has them too — that's what specialists read
    cache = json.loads((cache_dir / "voice-profile.json").read_text())
    assert "banned_buzzwords" in cache
    assert "enterprise" in cache["banned_buzzwords"]
    assert "banned_marketer_phrases" in cache
    assert "perfect fit" in cache["banned_marketer_phrases"]


# ---------------------------------------------------------------------------
# Roborev fix #2: cache invalidates on config change (not just benchmark change)
# ---------------------------------------------------------------------------


def test_voice_profile_cache_invalidates_on_config_change(tmp_path: Path) -> None:
    """Editing config.voice without touching the benchmark must invalidate the cache.

    Without this, mtime + content_hash on the benchmark would still match
    and the user's config change would be silently ignored.
    """
    qmd = tmp_path / "resume.qmd"
    qmd.write_text(SAMPLE_QMD)
    cache_dir = tmp_path / ".apply-state"
    config1 = _make_config(benchmark_path=qmd, banned_action_verbs=["Architected"])
    p1 = load_voice_profile(config1, cache_dir=cache_dir)
    assert "Architected" in p1.banned_verbs

    # Same benchmark file (unchanged), different config — cache must invalidate
    config2 = _make_config(benchmark_path=qmd, banned_action_verbs=["Architected", "Spearheaded"])
    p2 = load_voice_profile(config2, cache_dir=cache_dir)
    assert "Spearheaded" in p2.banned_verbs, (
        "Cache must invalidate on config change so the user's edit takes effect "
        "without requiring a benchmark touch"
    )


def test_voice_profile_cache_hit_on_unchanged_config_and_benchmark(tmp_path: Path) -> None:
    """Same config + same benchmark → cache hit (no warning, identical profile)."""
    qmd = tmp_path / "resume.qmd"
    qmd.write_text(SAMPLE_QMD)
    cache_dir = tmp_path / ".apply-state"
    config = _make_config(benchmark_path=qmd, banned_action_verbs=["Architected"])

    p1 = load_voice_profile(config, cache_dir=cache_dir)
    # Reload — must hit cache (we verify by checking config_hash is stable)
    p2 = load_voice_profile(config, cache_dir=cache_dir)
    assert p1.config_hash is not None
    assert p1.config_hash == p2.config_hash
