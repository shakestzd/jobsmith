"""Voice rubric: framework owns structure, users own content.

Precedence chain (high to low):
  1. user config (.apply-config.yaml voice section) — result_verbs / action_verbs
  2. benchmark-derived (benchmarks.resume_qmd first-word frequency)
  3. GENERIC seed defaults — grounded in published authority (Harvard FAS,
     MIT CAPD, Yale OCS, The Muse, Resume Worded, novoresume, Interview Guys)

Feedback (feedback.py) is a SEPARATE PARALLEL pathway — soft lessons read
by specialists, NOT structured verb lists. Do not merge into VoiceProfile.

Cache: .apply-state/voice-profile.json — keyed by mtime + content_hash.
Recomputes on hash change OR schema validation failure (robust to clock skew
and file corruption). Corrupt cache → live recompute with warning.
"""

from __future__ import annotations

import hashlib
import json
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Generic seed lists (authoritative, role-neutral)
# Sources: Harvard FAS, MIT CAPD, Yale OCS, The Muse, Resume Worded, novoresume
# ---------------------------------------------------------------------------

GENERIC_RESULT_VERBS: frozenset[str] = frozenset({
    "Accelerated",
    "Boosted",
    "Exceeded",
    "Expanded",
    "Generated",
    "Grew",
    "Improved",
    "Increased",
    "Launched",
    "Reduced",
    "Saved",
    "Streamlined",
})

GENERIC_ACTION_VERBS: frozenset[str] = frozenset({
    "Analyzed",
    "Built",
    "Collaborated",
    "Deployed",
    "Designed",
    "Developed",
    "Engineered",
    "Implemented",
    "Integrated",
    "Negotiated",
    "Optimized",
    "Trained",
})

# Q6 option-c: tier-1 puffery defaults (hard-ban)
GENERIC_BANNED_ADJECTIVES: tuple[str, ...] = (
    "innovative",
    "passionate",
    "dynamic",
    "results-driven",
    "self-starter",
)

# Metric patterns: $30, 30%, $1.2M, 200K, etc. in first 5 words
_METRIC_RE = re.compile(
    r"""
    (?:
        \$[\d.,]+\s*[KMBkmb]?  # dollar amounts: $30, $1.2M, $500K
        |
        [\d.,]+\s*[KMBkmb]\b  # asset counts: 200K, 1.5M
        |
        [\d]+\s*%              # percentages: 30%, 75%
        |
        \b\d+(?:\.\d+)?\b      # bare numbers: 12, 3.5
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# VoiceProfile dataclass
# ---------------------------------------------------------------------------


@dataclass
class VoiceProfile:
    """A resolved, validated voice rubric for the current user.

    Built by :func:`load_voice_profile`; consumed by :func:`is_result_first`
    and by specialist prompts (via Slice B.1).
    """

    result_verbs: frozenset[str]
    action_verbs: frozenset[str]
    banned_verbs: frozenset[str]
    banned_adjectives: tuple[str, ...]
    avg_bullet_words: float
    bullets_per_position_avg: float
    source: str  # "generic" | "benchmark" | "config-override"
    benchmark_path: str | None = None
    benchmark_mtime: float | None = None
    benchmark_hash: str | None = None


# ---------------------------------------------------------------------------
# Pydantic schema for on-disk cache (enables schema validation on load)
# ---------------------------------------------------------------------------


class _CachedProfile(BaseModel):
    """Cache schema — validation failure triggers live recompute with warning."""

    result_verbs: list[str]
    action_verbs: list[str]
    banned_verbs: list[str]
    banned_adjectives: list[str]
    avg_bullet_words: float
    bullets_per_position_avg: float
    source: str
    benchmark_path: str | None = None
    benchmark_mtime: float | None = None
    benchmark_hash: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _content_hash(text: str) -> str:
    """16-char SHA-256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _extract_bullets_from_qmd(qmd_path: Path) -> list[str]:
    """Extract bullet lines (markdown '- ' prefix) from a .qmd file."""
    bullets: list[str] = []
    for line in qmd_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return bullets


def _count_h3_sections(qmd_text: str) -> int:
    """Count level-3 headings as a proxy for position count."""
    return sum(1 for line in qmd_text.splitlines() if line.startswith("### "))


def _first_word(text: str) -> str:
    """Return the first whitespace-delimited token from text."""
    parts = text.split()
    return parts[0] if parts else ""


def _from_cached(cached: _CachedProfile) -> VoiceProfile:
    return VoiceProfile(
        result_verbs=frozenset(cached.result_verbs),
        action_verbs=frozenset(cached.action_verbs),
        banned_verbs=frozenset(cached.banned_verbs),
        banned_adjectives=tuple(cached.banned_adjectives),
        avg_bullet_words=cached.avg_bullet_words,
        bullets_per_position_avg=cached.bullets_per_position_avg,
        source=cached.source,
        benchmark_path=cached.benchmark_path,
        benchmark_mtime=cached.benchmark_mtime,
        benchmark_hash=cached.benchmark_hash,
    )


def _write_cache(profile: VoiceProfile, cache_path: Path) -> None:
    data = {
        "result_verbs": sorted(profile.result_verbs),
        "action_verbs": sorted(profile.action_verbs),
        "banned_verbs": sorted(profile.banned_verbs),
        "banned_adjectives": list(profile.banned_adjectives),
        "avg_bullet_words": profile.avg_bullet_words,
        "bullets_per_position_avg": profile.bullets_per_position_avg,
        "source": profile.source,
        "benchmark_path": profile.benchmark_path,
        "benchmark_mtime": profile.benchmark_mtime,
        "benchmark_hash": profile.benchmark_hash,
    }
    cache_path.write_text(json.dumps(data, indent=2))


def _compute_profile(config, benchmark_path: Path | None) -> VoiceProfile:
    """Derive a VoiceProfile from config + optional benchmark file.

    Layer order (each layer overrides below):
      1. config.voice.result_verbs / action_verbs (non-empty → config-override)
      2. benchmark first-word frequency (benchmark present → benchmark)
      3. GENERIC seeds (fallback)
    """
    voice_cfg = getattr(config, "voice", None)
    config_result = list(getattr(voice_cfg, "result_verbs", None) or [])
    config_action = list(getattr(voice_cfg, "action_verbs", None) or [])
    config_banned_verbs = list(getattr(voice_cfg, "banned_action_verbs", None) or [])
    config_banned_adj = tuple(
        getattr(voice_cfg, "banned_adjectives", None) or GENERIC_BANNED_ADJECTIVES
    )

    avg_words = 12.0
    bullets_per_pos = 4.0
    benchmark_mtime: float | None = None
    benchmark_hash_val: str | None = None

    benchmark_result_verbs: frozenset[str] = frozenset()
    benchmark_action_verbs: frozenset[str] = frozenset()

    if benchmark_path and benchmark_path.exists():
        qmd_text = benchmark_path.read_text(encoding="utf-8")
        bullets = _extract_bullets_from_qmd(benchmark_path)

        benchmark_mtime = benchmark_path.stat().st_mtime
        benchmark_hash_val = _content_hash(qmd_text)

        if bullets:
            # Compute avg words per bullet
            total_words = sum(len(b.split()) for b in bullets)
            avg_words = total_words / len(bullets)

            # Estimate bullets per position
            n_positions = max(_count_h3_sections(qmd_text), 1)
            bullets_per_pos = len(bullets) / n_positions

            # Extract first-word frequency to classify result vs action verbs
            # A "result verb" leads a bullet that contains a metric; others are action verbs
            result_candidates: set[str] = set()
            action_candidates: set[str] = set()
            for bullet in bullets:
                fw = _first_word(bullet)
                if not fw or not fw[0].isupper():
                    continue
                # Strip trailing punctuation
                fw = fw.rstrip(".,;:")
                if _METRIC_RE.search(" ".join(bullet.split()[:6])):
                    result_candidates.add(fw)
                else:
                    action_candidates.add(fw)
            benchmark_result_verbs = frozenset(result_candidates)
            benchmark_action_verbs = frozenset(action_candidates - result_candidates)

    # Determine final verb sets and source
    if config_result or config_action:
        result_verbs = frozenset(config_result) if config_result else (
            benchmark_result_verbs | GENERIC_RESULT_VERBS
        )
        action_verbs = frozenset(config_action) if config_action else (
            benchmark_action_verbs | GENERIC_ACTION_VERBS
        )
        source = "config-override"
    elif benchmark_path and benchmark_path.exists() and (
        benchmark_result_verbs or benchmark_action_verbs
    ):
        # Augment benchmark verbs with generic seeds
        result_verbs = benchmark_result_verbs | GENERIC_RESULT_VERBS
        action_verbs = benchmark_action_verbs | GENERIC_ACTION_VERBS
        source = "benchmark"
    else:
        result_verbs = GENERIC_RESULT_VERBS
        action_verbs = GENERIC_ACTION_VERBS
        source = "generic"

    return VoiceProfile(
        result_verbs=result_verbs,
        action_verbs=action_verbs,
        banned_verbs=frozenset(config_banned_verbs),
        banned_adjectives=config_banned_adj,
        avg_bullet_words=avg_words,
        bullets_per_position_avg=bullets_per_pos,
        source=source,
        benchmark_path=str(benchmark_path) if benchmark_path else None,
        benchmark_mtime=benchmark_mtime,
        benchmark_hash=benchmark_hash_val,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_voice_profile(config, cache_dir: Path | None = None) -> VoiceProfile:
    """Read benchmark + config + GENERIC seeds → VoiceProfile.

    Parameters
    ----------
    config:
        Jobsmith config object. Uses:
          - config.benchmarks.resume_qmd (optional Path)
          - config.voice.result_verbs, action_verbs, banned_action_verbs,
            banned_adjectives
    cache_dir:
        Where to read/write voice-profile.json.
        Defaults to .apply-state/ relative to cwd.

    Cache keying: mtime + content_hash composite (Q4 option-a).
    Recomputes on: hash change, mtime change, or schema validation failure.
    Corrupt cache → warning + live recompute.
    """
    cache_dir = cache_dir or Path(".apply-state")
    cache_path = cache_dir / "voice-profile.json"

    bm_cfg = getattr(config, "benchmarks", None)
    raw_bm_path = getattr(bm_cfg, "resume_qmd", None)
    benchmark_path: Path | None = Path(raw_bm_path) if raw_bm_path else None

    # Cache hit check: mtime + content_hash both match
    if benchmark_path and benchmark_path.exists() and cache_path.exists():
        try:
            cached = _CachedProfile.model_validate_json(cache_path.read_text())
            mtime = benchmark_path.stat().st_mtime
            file_text = benchmark_path.read_text(encoding="utf-8")
            if (cached.benchmark_mtime == mtime
                    and cached.benchmark_hash == _content_hash(file_text)):
                return _from_cached(cached)
        except (ValidationError, Exception):
            warnings.warn(
                f"voice-profile.json cache invalid; recomputing from {cache_path}",
                stacklevel=2,
            )

    # Live recompute
    profile = _compute_profile(config, benchmark_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _write_cache(profile, cache_path)
    return profile


def is_result_first(bullet_text: str, profile: VoiceProfile) -> tuple[bool, str]:
    """Validate that a bullet leads with a result signal.

    Precedence (evaluated in order):
      1. Banned verb in first word → fail immediately with "banned verb" message
      2. Metric in first 5 words → pass (strong result signal)
      3. profile.result_verbs first-word match → pass
      4. profile.action_verbs first-word match → fail with restructure hint
      5. Generic action verb match → fail with restructure hint
      6. Other → soft-warn pass (unknown first word, treat charitably)

    Returns
    -------
    (bool, str)
        True + empty string on pass.
        False + explanation string on fail.
    """
    words = bullet_text.split()
    if not words:
        return False, "Empty bullet"

    first = words[0].rstrip(".,;:")

    # 1. Banned verb check (hard fail)
    if first in profile.banned_verbs or any(
        bullet_text.lower().startswith(bv.lower()) for bv in profile.banned_verbs
    ):
        return False, f"Banned verb '{first}' — replace with a result or quantified outcome"

    # 2. Metric in first 5 words (hard pass)
    first_five = " ".join(words[:5])
    if _METRIC_RE.search(first_five):
        return True, ""

    # 3. Result verb first word (pass)
    if first in profile.result_verbs:
        return True, ""

    # 4. Action verb first word → fail with restructure hint
    if first in profile.action_verbs or first in GENERIC_ACTION_VERBS:
        return (
            False,
            f"Action verb '{first}' leads — restructure to put the result/metric first, "
            f"e.g. 'Reduced X by Y% by {first.lower()}ing ...'",
        )

    # 5. Other → soft pass (unknown verb, not banned, not generic action)
    return True, ""


__all__ = [
    "GENERIC_ACTION_VERBS",
    "GENERIC_BANNED_ADJECTIVES",
    "GENERIC_RESULT_VERBS",
    "VoiceProfile",
    "_content_hash",
    "_extract_bullets_from_qmd",
    "is_result_first",
    "load_voice_profile",
]
