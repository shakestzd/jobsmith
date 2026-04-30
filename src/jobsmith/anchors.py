"""Anchor-bullet primitives — the regex library and threshold constants.

Anchor bullets are the load-bearing achievements on a tailored resume —
those containing dollar amounts, percentages, or asset-scale counts above
declared thresholds. They MUST survive JD tailoring unless an explicit
reason to drop is logged.

This module is the single source of truth for:
- the regex patterns that extract anchor metrics from prose
- the numeric thresholds that classify a metric as "load-bearing"

Both `scripts/anchor_bullet_guard.py` and `scripts/fact_check_draft.py`
import from here. If the regex needs an update, change it once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------- regex library ----------

# $250M, $1B, $50.5K, $120,000, $120K, $132
_MONEY_RE = re.compile(r"\$\d+(?:[.,]\d+)*\s*[KMBk]?")

# 97.3%, 99.9%, 100%, 72%
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?%")

# Asset-count anchors: "200K solar assets", "500K customers", "70K systems".
# Negative lookbehind for `$` rules out "$230K project" (that's money, not count).
# Allow 1-3 connective words between the number and the unit noun so adjectival
# qualifiers ("70K qualifying systems") still match. "projects" and "companies"
# excluded — too prone to false matches with low counts.
_ASSET_COUNT_RE = re.compile(
    r"(?<!\$)\b(\d+(?:\.\d+)?)\s*([KMB])\+?\s+(?:[\w\-]+\s+){0,3}"
    r"(?:assets?|systems?|customers?|accounts?|users?|clients?|"
    r"properties|sites?|households?|homesteads?)\b",
    re.IGNORECASE,
)


# ---------- threshold constants (defaults — overridable via JobsmithConfig) ----------

# Dollar amount above which a money-anchor is "load-bearing".
DEFAULT_MONEY_THRESHOLD_USD = 10_000_000  # $10M

# Percentage above which a percent-anchor is "load-bearing".
DEFAULT_PERCENT_THRESHOLD = 50.0  # 50%

# Asset-count above which an asset-count-anchor is "load-bearing".
DEFAULT_ASSET_COUNT_THRESHOLD = 100_000  # 100K


# ---------- value normalization ----------


@dataclass
class Anchor:
    """One anchor metric within a bullet. A bullet may have several."""

    kind: str  # 'money' | 'percent' | 'asset_count'
    raw: str  # the matched text, e.g. '$250M' or '75%'
    value: float  # normalized — USD for money, percent points, raw count


def parse_money_to_usd(raw: str) -> float | None:
    """Normalize '$250M', '$1B', '$50.5K' to a USD float.

    Returns None if the raw string can't be parsed.
    """
    match = re.match(r"\$(\d+(?:[.,]\d+)*)\s*([KMBk]?)", raw)
    if not match:
        return None
    digits = match.group(1).replace(",", "")
    suffix = match.group(2).upper()
    try:
        value = float(digits)
    except ValueError:
        return None
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "": 1}[suffix]
    return value * multiplier


def parse_percent(raw: str) -> float | None:
    """Normalize '97.3%' to 97.3."""
    match = re.match(r"(\d+(?:\.\d+)?)%", raw)
    if not match:
        return None
    return float(match.group(1))


def parse_asset_count(raw: str) -> int | None:
    """Normalize '200K' to 200000, '1.5M' to 1500000."""
    match = re.match(r"(\d+(?:\.\d+)?)\s*([KMB])?", raw, re.IGNORECASE)
    if not match:
        return None
    digits = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "": 1}[suffix]
    return int(digits * multiplier)


def extract_anchors(
    text: str,
    money_threshold: float = DEFAULT_MONEY_THRESHOLD_USD,
    percent_threshold: float = DEFAULT_PERCENT_THRESHOLD,
    asset_count_threshold: int = DEFAULT_ASSET_COUNT_THRESHOLD,
) -> list[Anchor]:
    """Extract all anchor metrics from a bullet's text.

    Returns the list of anchors above the configured thresholds.
    """
    anchors: list[Anchor] = []

    for match in _MONEY_RE.finditer(text):
        usd = parse_money_to_usd(match.group(0))
        if usd is not None and usd >= money_threshold:
            anchors.append(Anchor(kind="money", raw=match.group(0), value=usd))

    for match in _PERCENT_RE.finditer(text):
        pct = parse_percent(match.group(0))
        if pct is not None and pct >= percent_threshold:
            anchors.append(Anchor(kind="percent", raw=match.group(0), value=pct))

    for match in _ASSET_COUNT_RE.finditer(text):
        digits = match.group(1)
        suffix = match.group(2) or ""
        count = parse_asset_count(f"{digits}{suffix}")
        if count is not None and count >= asset_count_threshold:
            anchors.append(Anchor(kind="asset_count", raw=match.group(0), value=float(count)))

    return anchors


__all__ = [
    "Anchor",
    "DEFAULT_ASSET_COUNT_THRESHOLD",
    "DEFAULT_MONEY_THRESHOLD_USD",
    "DEFAULT_PERCENT_THRESHOLD",
    "_ASSET_COUNT_RE",
    "_MONEY_RE",
    "_PERCENT_RE",
    "extract_anchors",
    "parse_asset_count",
    "parse_money_to_usd",
    "parse_percent",
]
