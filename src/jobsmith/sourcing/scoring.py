"""Fast-path scoring for sourced job postings (feat-5531c54b).

Bundles the three scoring concerns ported from shakestzd/private/scripts/:
  - comp_parser.py  → parse_compensation / Compensation
  - red_flags.py    → detect_red_flags / RedFlag
  - fit_scoring.py  → score_all_lanes / ScoredRole

All functions are pure (no network, no DB, no I/O) and safe to call in
tight loops during the crawl's post-fetch scoring pass.

The YAML config paths (scoring-weights.yaml, shakes-profile.yaml,
red-flag-patterns.yaml) are resolved from the user's repo root via
JOBSMITH_REPO_ROOT (or the standard walk-up). When the files are absent
the scorer degrades gracefully (returns zero scores, empty red-flag lists).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# ---------------------------------------------------------------------------
# Compensation parser
# ---------------------------------------------------------------------------

Confidence = Literal["low", "med", "high"]


@dataclass
class Compensation:
    base_range_low: int | None = None
    base_range_high: int | None = None
    bonus: str | None = None
    equity: str | None = None
    benefits_list: list[str] = field(default_factory=list)
    total_comp_estimate: int | None = None
    confidence: Confidence = "low"


# Match: $120,000, $120000, $120k, $120K
_DOLLAR_AMOUNT = r"\$\s*(\d{2,3}(?:[,\.]?\d{3})*)\s*(k|K)?"

_RANGE_RE = re.compile(
    rf"{_DOLLAR_AMOUNT}\s*(?:[-–—]|to|—)\s*{_DOLLAR_AMOUNT}",
    re.IGNORECASE,
)
_STARTING_AT_RE = re.compile(
    rf"(?:starting\s+at|from|at\s+least|minimum\s+of)\s*{_DOLLAR_AMOUNT}",
    re.IGNORECASE,
)
_UP_TO_RE = re.compile(
    rf"(?:up\s+to|max(?:imum)?\s+of)\s*{_DOLLAR_AMOUNT}",
    re.IGNORECASE,
)
_COMPETITIVE_RE = re.compile(
    r"(?i)\bcompensation\s+commensurate|\bcompetitive\s+(?:salary|compensation|pay|package)",
)
_EQUITY_ONLY_RE = re.compile(
    r"(?i)equity[- ]only|no\s+(?:cash\s+)?salary",
)
_BONUS_RE = re.compile(
    r"(?i)(?:annual|performance|target|signing|year[- ]end)\s+bonus|"
    r"bonus[- ]eligible|cash\s+bonus|bonus\s+structure",
)
_BENEFIT_PATTERNS = {
    "health": r"(?i)\bhealth\s+(?:insurance|coverage|benefits)\b",
    "dental": r"(?i)\bdental\b",
    "vision": r"(?i)\bvision\b",
    "401k": r"(?i)\b401\s*\(?k\)?(?:\s+(?:match|matching))?\b",
    "equity": r"(?i)\b(?:equity|stock\s+options|rsu|isos?)\b",
    "pto": r"(?i)\b(?:pto|paid\s+time[- ]off|vacation\s+days?)\b",
    "remote": r"(?i)\b(?:fully\s+remote|remote[- ]first|work\s+from\s+(?:home|anywhere))\b",
    "tuition": r"(?i)\btuition\s+(?:reimbursement|assistance)\b",
    "parental": r"(?i)\bparental\s+leave\b",
}


def _to_int(amount: str, k_suffix: str | None) -> int:
    cleaned = amount.replace(",", "").replace(".", "")
    n = int(cleaned)
    if k_suffix:
        n *= 1000
    return n


def _find_range(text: str) -> tuple[int | None, int | None, Confidence]:
    m = _RANGE_RE.search(text)
    if m:
        low = _to_int(m.group(1), m.group(2))
        high = _to_int(m.group(3), m.group(4))
        if low < 1000 or high < 1000:
            return None, None, "low"
        if low > high:
            low, high = high, low
        return low, high, "high"

    m_start = _STARTING_AT_RE.search(text)
    if m_start:
        low = _to_int(m_start.group(1), m_start.group(2))
        if low >= 1000:
            return low, None, "med"

    m_up = _UP_TO_RE.search(text)
    if m_up:
        high = _to_int(m_up.group(1), m_up.group(2))
        if high >= 1000:
            return None, high, "med"

    return None, None, "low"


def _detect_benefits(text: str) -> list[str]:
    return [name for name, pattern in _BENEFIT_PATTERNS.items() if re.search(pattern, text)]


def _detect_bonus(text: str) -> str | None:
    m = _BONUS_RE.search(text)
    return m.group(0) if m else None


def _detect_equity(text: str) -> str | None:
    if _EQUITY_ONLY_RE.search(text):
        return "equity-only"
    if re.search(r"(?i)\b(?:equity|stock\s+options|rsu|isos?)\b", text):
        return "equity grant"
    return None


def parse_compensation(text: str) -> Compensation:
    """Extract structured compensation info from JD body text."""
    if not text or not text.strip():
        return Compensation()

    low, high, conf = _find_range(text)

    if low is None and high is None and _COMPETITIVE_RE.search(text):
        conf = "low"

    bonus = _detect_bonus(text)
    equity = _detect_equity(text)
    benefits = _detect_benefits(text)

    total_estimate: int | None = None
    if low is not None and high is not None:
        midpoint = (low + high) // 2
        total_estimate = midpoint
        if bonus:
            total_estimate += midpoint * 15 // 100
    elif low is not None:
        total_estimate = low
        if bonus:
            total_estimate += low * 15 // 100

    return Compensation(
        base_range_low=low,
        base_range_high=high,
        bonus=bonus,
        equity=equity,
        benefits_list=benefits,
        total_comp_estimate=total_estimate,
        confidence=conf,
    )


# ---------------------------------------------------------------------------
# Red flags
# ---------------------------------------------------------------------------

Severity = Literal["info", "warn", "block"]


@dataclass(frozen=True)
class RedFlag:
    severity: Severity
    pattern: str
    matched_phrase: str
    advice: str


_PATTERN_CACHE: list[tuple[str, Severity, re.Pattern[str], str]] | None = None


def _red_flag_patterns_path() -> Path | None:
    override = os.environ.get("RED_FLAG_PATTERNS_PATH")
    if override:
        return Path(override)
    # Resolve from user's repo root
    from .._sourcing_config import _resolve_repo_root_best_effort

    root = _resolve_repo_root_best_effort()
    if root is None:
        return None
    candidate = root / "private" / "capacity" / "red-flag-patterns.yaml"
    return candidate if candidate.exists() else None


def _load_red_flag_patterns() -> list[tuple[str, Severity, re.Pattern[str], str]]:
    global _PATTERN_CACHE
    if _PATTERN_CACHE is not None:
        return _PATTERN_CACHE
    path = _red_flag_patterns_path()
    if path is None or not path.exists():
        _PATTERN_CACHE = []
        return _PATTERN_CACHE
    raw = yaml.safe_load(path.read_text()) or {}
    patterns: list[tuple[str, Severity, re.Pattern[str], str]] = []
    for entry in raw.get("patterns", []):
        try:
            compiled = re.compile(entry["regex"])
        except re.error:
            continue
        patterns.append(
            (entry["id"], entry["severity"], compiled, entry.get("advice", ""))
        )
    _PATTERN_CACHE = patterns
    return _PATTERN_CACHE


def reset_red_flag_cache() -> None:
    """Test hook — clear cached patterns so a new path is reloaded."""
    global _PATTERN_CACHE
    _PATTERN_CACHE = None


def detect_red_flags(text: str) -> list[RedFlag]:
    """Scan JD text against the pattern library, return all hits."""
    if not text:
        return []
    out: list[RedFlag] = []
    for pid, severity, regex, advice in _load_red_flag_patterns():
        m = regex.search(text)
        if m:
            out.append(
                RedFlag(severity=severity, pattern=pid, matched_phrase=m.group(0), advice=advice)
            )
    return out


# ---------------------------------------------------------------------------
# Fit scoring
# ---------------------------------------------------------------------------


@dataclass
class ScoredRole:
    """Three-lane fit scoring result."""

    # Legacy fields — preserved for backward compatibility during dual-write.
    score_a: int = 0
    score_b: int = 0

    # Three-lane breakdown
    score_tax_equity: int = 0
    score_ai_research: int = 0
    score_elixir_distributed: int = 0

    dominant_specialty: str = ""
    score_breakdown: dict[str, int] = field(default_factory=dict)

    reasoning: dict[str, list[str]] = field(default_factory=dict)
    mode_tags: list[str] = field(default_factory=list)
    excluded: bool = False


_WEIGHTS_CACHE: dict[str, Any] | None = None
_PROFILE_CACHE: dict[str, Any] | None = None


def _scoring_yaml_paths() -> tuple[Path | None, Path | None]:
    """Return (weights_path, profile_path) resolved from repo root."""
    from .._sourcing_config import _resolve_repo_root_best_effort

    root = _resolve_repo_root_best_effort()
    if root is None:
        return None, None
    weights = root / "private" / "capacity" / "scoring-weights.yaml"
    profile = root / "private" / "capacity" / "shakes-profile.yaml"
    return (weights if weights.exists() else None, profile if profile.exists() else None)


def _load_weights() -> dict[str, Any]:
    global _WEIGHTS_CACHE
    if _WEIGHTS_CACHE is not None:
        return _WEIGHTS_CACHE
    weights_path, _ = _scoring_yaml_paths()
    if weights_path is None:
        _WEIGHTS_CACHE = {}
        return _WEIGHTS_CACHE
    _WEIGHTS_CACHE = yaml.safe_load(weights_path.read_text()) or {}
    return _WEIGHTS_CACHE


def _load_profile() -> dict[str, Any]:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is not None:
        return _PROFILE_CACHE
    _, profile_path = _scoring_yaml_paths()
    if profile_path is None:
        _PROFILE_CACHE = {}
        return _PROFILE_CACHE
    _PROFILE_CACHE = yaml.safe_load(profile_path.read_text()) or {}
    return _PROFILE_CACHE


def reset_scoring_cache() -> None:
    """Test hook — clear cached weights AND profile."""
    global _WEIGHTS_CACHE, _PROFILE_CACHE
    _WEIGHTS_CACHE = None
    _PROFILE_CACHE = None


_EVIDENCE_MULT = {"primary": 1.0, "secondary": 0.6, "mentioned": 0.3}
_EVIDENCE_MISSING_DEFAULT = 0.5


def _evidence_multiplier(profile: dict, evidence_key: str | None) -> float:
    if not evidence_key:
        return 1.0
    stack = profile.get("stack") or []
    for entry in stack:
        if (entry or {}).get("name") == evidence_key:
            return _EVIDENCE_MULT.get(entry.get("evidence", "mentioned"), 0.5)
    return _EVIDENCE_MISSING_DEFAULT


def _match_group(
    text: str,
    group: dict[str, dict[str, Any]],
    profile: dict,
) -> tuple[int, list[str]]:
    total = 0.0
    matched: list[str] = []
    for pid, spec in (group or {}).items():
        try:
            if re.search(spec["regex"], text, flags=re.IGNORECASE):
                base = float(spec.get("base_weight", spec.get("weight", 0)))
                mult = _evidence_multiplier(profile, spec.get("evidence_key"))
                total += base * mult
                matched.append(pid)
        except re.error:
            continue
    return int(round(total)), matched


def _excluded_by_block_flag(red_flags: list | None) -> bool:
    if not red_flags:
        return False
    return any(getattr(f, "severity", None) == "block" for f in red_flags)


def _comp_floor_penalty(comp_high: int | None, floor: dict[str, Any]) -> int:
    target = int(floor.get("target_usd", 132000))
    if comp_high is None:
        return -int(floor.get("penalty_unknown", 0))
    if comp_high >= target:
        return 0
    if comp_high >= 120000:
        return -int(floor.get("penalty_below_target", 0))
    if comp_high >= 100000:
        return -int(floor.get("penalty_well_below", 0))
    return -int(floor.get("penalty_far_below", 0))


def _clamp(score: int) -> int:
    return max(0, min(100, score))


def _score_lane(
    lane_name: str,
    lane_weights: dict[str, Any],
    jd_text: str,
    profile: dict,
    comp_high: int | None,
) -> tuple[int, list[str]]:
    score = 0
    matched: list[str] = []
    for group_name in ("keywords", "domain", "arrangement"):
        group = lane_weights.get(group_name, {}) or {}
        group_score, group_matched = _match_group(jd_text, group, profile)
        score += group_score
        matched.extend(group_matched)
    floor = lane_weights.get("comp_floor")
    if floor:
        score += _comp_floor_penalty(comp_high, floor)
    return _clamp(score), matched


def score_all_lanes(
    jd_text: str,
    metadata: dict | None = None,
    red_flags: list | None = None,
) -> ScoredRole:
    """Score a JD across all three Mode A specialty lanes + mode_b.

    Gracefully degrades to zero scores when weights/profile YAML files
    are absent (user hasn't set up private/capacity/ yet).
    """
    weights = _load_weights()
    profile = _load_profile()
    lanes = weights.get("lanes", {}) or {}
    comp = parse_compensation(jd_text)
    comp_high = comp.base_range_high

    tax_score, tax_matched = _score_lane(
        "tax_equity", lanes.get("tax_equity", {}), jd_text, profile, comp_high
    )
    ai_score, ai_matched = _score_lane(
        "ai_research", lanes.get("ai_research", {}), jd_text, profile, comp_high
    )
    elixir_score, elixir_matched = _score_lane(
        "elixir_distributed",
        lanes.get("elixir_distributed", {}),
        jd_text,
        profile,
        comp_high,
    )

    mode_b_weights = weights.get("mode_b", {}) or {}
    mode_b_score = 0
    mode_b_matched: list[str] = []
    arr_score, arr_hits = _match_group(
        jd_text, mode_b_weights.get("arrangement", {}), profile
    )
    mode_b_score += arr_score
    mode_b_matched.extend(arr_hits)
    pen_score, pen_hits = _match_group(
        jd_text, mode_b_weights.get("penalty", {}), profile
    )
    mode_b_score += pen_score
    mode_b_score = _clamp(mode_b_score)

    breakdown = {
        "tax_equity": tax_score,
        "ai_research": ai_score,
        "elixir_distributed": elixir_score,
    }
    dominant, score_a = max(breakdown.items(), key=lambda kv: kv[1])
    if score_a == 0:
        dominant = ""

    all_matched = list(
        dict.fromkeys(tax_matched + ai_matched + elixir_matched + mode_b_matched)
    )

    return ScoredRole(
        score_a=score_a,
        score_b=mode_b_score,
        score_tax_equity=tax_score,
        score_ai_research=ai_score,
        score_elixir_distributed=elixir_score,
        dominant_specialty=dominant,
        score_breakdown=breakdown,
        reasoning={
            "matched": all_matched,
            "penalties": pen_hits,
            "missed": [],
        },
        mode_tags=all_matched,
        excluded=_excluded_by_block_flag(red_flags),
    )


def score_role_fast(jd_text: str) -> dict:
    """Run fast-path scoring and return a flat dict with all score fields."""
    flags = detect_red_flags(jd_text or "")
    scored = score_all_lanes(jd_text or "", red_flags=flags)
    comp = parse_compensation(jd_text or "")
    return {
        "score_a": scored.score_a,
        "score_b": scored.score_b,
        "score_tax_equity": scored.score_tax_equity,
        "score_ai_research": scored.score_ai_research,
        "score_elixir_distributed": scored.score_elixir_distributed,
        "dominant_specialty": scored.dominant_specialty,
        "score_breakdown": scored.score_breakdown,
        "excluded": scored.excluded,
        "mode_tags": list(scored.mode_tags),
        "reasoning": {"matched": scored.reasoning.get("matched", [])},
        "comp_low": comp.base_range_low,
        "comp_high": comp.base_range_high,
        "total_comp_estimate": comp.total_comp_estimate,
        "comp_confidence": comp.confidence,
        "red_flags": [
            {"severity": f.severity, "pattern": f.pattern, "matched": f.matched_phrase}
            for f in flags
        ],
    }
