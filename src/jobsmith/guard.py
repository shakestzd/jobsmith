"""Anchor-bullet preservation guardrail.

Reads master `work.yml`, identifies anchor bullets (those with ≥$10M /
≥50% / ≥100K-asset metrics by default), cross-references them against
a tailored `bullet-selection.json`, and reports any anchor dropped
without a logged reason.

The CLI surface is `jobsmith anchor-check`. The Python API is
`check_anchors(master_path, selection_path, decisions_path) -> GuardResult`.

Exit codes (when invoked via CLI):
    0  all anchors preserved, or all drops have logged reasons
    1  one or more anchors dropped without logged reason
    2  internal error (master missing, JSON malformed, etc.)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .anchors import (
    DEFAULT_ASSET_COUNT_THRESHOLD,
    DEFAULT_MONEY_THRESHOLD_USD,
    DEFAULT_PERCENT_THRESHOLD,
    Anchor,
    _ASSET_COUNT_RE,
    _MONEY_RE,
    _PERCENT_RE,
    is_anchor,
)

# ---------- types ----------


@dataclass
class Bullet:
    """A single bullet from master work.yml with its anchors annotated."""

    bullet_id: str  # stable 12-char hash of the bullet text
    text: str
    company: str
    position_title: str
    position_index: int
    bullet_index: int
    anchors: list[Anchor] = field(default_factory=list)
    # Object-form fields — populated when details entry is a dict, not a string.
    anchor_explicit: bool | None = None   # user-declared anchor flag (overrides regex)
    anchor_reason: str | None = None      # human rationale for the anchor designation
    tags: list[str] = field(default_factory=list)
    drop_when: str | None = None

    @property
    def is_anchor(self) -> bool:
        return bool(self.anchors)


@dataclass
class GuardResult:
    """Outcome of cross-referencing anchors against a tailored selection."""

    exit_code: int
    anchor_bullets: list[Bullet]
    kept: list[Bullet]
    dropped_without_reason: list[Bullet]
    dropped_with_reason: list[tuple[Bullet, str]]


# ---------- internal helpers ----------


def _bullet_id(text: str) -> str:
    """Stable, content-derived id. SHA-1 hex first 12 chars."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _parse_money(raw: str) -> float | None:
    """Convert '$250M' -> 250_000_000."""
    m = re.match(r"\$([\d.,]+)\s*([KMBk]?)", raw)
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    try:
        num = float(num_str)
    except ValueError:
        return None
    suffix = m.group(2).upper()
    multipliers = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return num * multipliers.get(suffix, 1)


def _parse_percent(raw: str) -> float | None:
    m = re.match(r"(\d+(?:\.\d+)?)%", raw)
    return float(m.group(1)) if m else None


def _parse_asset_count(num: str, suffix: str) -> float | None:
    try:
        n = float(num)
    except ValueError:
        return None
    multipliers = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return n * multipliers.get(suffix.upper(), 1)


# ---------- public API ----------


def find_anchors_in_text(
    text: str,
    money_threshold: float = DEFAULT_MONEY_THRESHOLD_USD,
    percent_threshold: float = DEFAULT_PERCENT_THRESHOLD,
    asset_count_threshold: float = DEFAULT_ASSET_COUNT_THRESHOLD,
) -> list[Anchor]:
    """Detect every anchor metric in a single bullet's text."""
    anchors: list[Anchor] = []

    for m in _MONEY_RE.finditer(text):
        val = _parse_money(m.group(0))
        if val is not None and val >= money_threshold:
            anchors.append(Anchor(kind="money", raw=m.group(0), value=val))

    for m in _PERCENT_RE.finditer(text):
        val = _parse_percent(m.group(0))
        if val is not None and val >= percent_threshold:
            anchors.append(Anchor(kind="percent", raw=m.group(0), value=val))

    for m in _ASSET_COUNT_RE.finditer(text):
        val = _parse_asset_count(m.group(1), m.group(2))
        if val is not None and val >= asset_count_threshold:
            anchors.append(Anchor(kind="asset_count", raw=m.group(0), value=val))

    return anchors


def parse_master_bullets(
    master_path: Path,
    money_threshold: float = DEFAULT_MONEY_THRESHOLD_USD,
    percent_threshold: float = DEFAULT_PERCENT_THRESHOLD,
    asset_count_threshold: float = DEFAULT_ASSET_COUNT_THRESHOLD,
) -> list[Bullet]:
    """Read master work.yml and return all bullets with anchor annotations."""
    data = yaml.safe_load(master_path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"{master_path} root must be a list of positions")

    out: list[Bullet] = []
    for pi, pos in enumerate(data):
        company = pos.get("location", "")
        title = pos.get("title", "")
        for bi, entry in enumerate(pos.get("details", []) or []):
            # Support both string form ("text") and dict form ({bullet, anchor, ...}).
            if isinstance(entry, dict):
                text = entry.get("bullet", "")
                anchor_explicit: bool | None = entry.get("anchor", None)
                anchor_reason: str | None = entry.get("anchor_reason", None)
                tags: list[str] = entry.get("tags", []) or []
                drop_when: str | None = entry.get("drop_when", None)
            else:
                text = entry
                anchor_explicit = None
                anchor_reason = None
                tags = []
                drop_when = None

            anchors = find_anchors_in_text(
                text, money_threshold, percent_threshold, asset_count_threshold
            )
            out.append(
                Bullet(
                    bullet_id=_bullet_id(text),
                    text=text,
                    company=company,
                    position_title=title,
                    position_index=pi,
                    bullet_index=bi,
                    anchors=anchors,
                    anchor_explicit=anchor_explicit,
                    anchor_reason=anchor_reason,
                    tags=tags,
                    drop_when=drop_when,
                )
            )
    return out


def load_selection(selection_path: Path) -> dict[str, dict]:
    """Index bullet-selection.json by master_bullet_id.

    Selection shape (per specialist-contracts.yaml):
        {"positions": [{"company": ..., "title": ..., "bullets": [
            {"master_bullet_id": "...", "included": bool,
             "rephrased": str|null, "reason_if_dropped": str|null}, ...
        ]}, ...]}
    """
    if not selection_path.exists():
        return {}
    data = json.loads(selection_path.read_text())
    out: dict[str, dict] = {}
    for pos in data.get("positions", []):
        for b in pos.get("bullets", []):
            bid = b.get("master_bullet_id")
            if bid:
                out[bid] = b
    return out


def load_decisions(decisions_path: Path) -> dict[str, str]:
    """Read bullet-decisions.json — anchor-drop reasons keyed by bullet_id."""
    if not decisions_path.exists():
        return {}
    data = json.loads(decisions_path.read_text())
    return {k: v for k, v in data.items() if isinstance(v, str)}


def check_anchors(
    master_path: Path,
    selection_path: Path,
    decisions_path: Path | None = None,
    money_threshold: float = DEFAULT_MONEY_THRESHOLD_USD,
    percent_threshold: float = DEFAULT_PERCENT_THRESHOLD,
    asset_count_threshold: float = DEFAULT_ASSET_COUNT_THRESHOLD,
) -> GuardResult:
    """Cross-reference anchor bullets in master against a tailored selection.

    The CLI calls this and translates the GuardResult to an exit code.
    Returns immediately if `selection_path` doesn't exist (interpreted as
    "selector hasn't run yet" — all anchors are preserved by default).
    """
    bullets = parse_master_bullets(
        master_path, money_threshold, percent_threshold, asset_count_threshold
    )
    thresholds = (money_threshold, percent_threshold, asset_count_threshold)
    anchors = [b for b in bullets if is_anchor(b, *thresholds)]
    selection = load_selection(selection_path)
    decisions = load_decisions(decisions_path) if decisions_path else {}

    kept: list[Bullet] = []
    dropped_with_reason: list[tuple[Bullet, str]] = []
    dropped_without_reason: list[Bullet] = []

    for b in anchors:
        sel = selection.get(b.bullet_id)
        if sel is None:
            # No selection entry — if no selection file at all, the selector
            # hasn't run yet, so treat all anchors as preserved.
            if not selection:
                kept.append(b)
                continue
            reason = decisions.get(b.bullet_id, "").strip()
            if reason and reason != "pending-inquiry":
                dropped_with_reason.append((b, reason))
            else:
                dropped_without_reason.append(b)
            continue

        if sel.get("included") is True:
            kept.append(b)
            continue

        reason = (sel.get("reason_if_dropped") or decisions.get(b.bullet_id, "")).strip()
        if reason and reason != "pending-inquiry":
            dropped_with_reason.append((b, reason))
        else:
            dropped_without_reason.append(b)

    exit_code = 1 if dropped_without_reason else 0
    return GuardResult(
        exit_code=exit_code,
        anchor_bullets=anchors,
        kept=kept,
        dropped_without_reason=dropped_without_reason,
        dropped_with_reason=dropped_with_reason,
    )


def render_diff_md(result: GuardResult, master_path: Path) -> str:
    """Side-by-side anchor-aware diff for .apply-state/bullet-diff.md."""
    lines = [
        "# Anchor bullet diff",
        "",
        f"Master: `{master_path}`",
        "",
        f"- anchor bullets in master: {len(result.anchor_bullets)}",
        f"- kept: {len(result.kept)}",
        f"- dropped with reason: {len(result.dropped_with_reason)}",
        f"- dropped without reason: {len(result.dropped_without_reason)}",
        "",
    ]

    if result.dropped_without_reason:
        lines.append("## ❌ Dropped without logged reason (HALT)")
        lines.append("")
        for b in result.dropped_without_reason:
            anchors_summary = ", ".join(f"{a.raw} ({a.kind})" for a in b.anchors)
            lines += [
                f"- `{b.bullet_id}` — {b.company} / {b.position_title}",
                f"  - anchors: {anchors_summary}",
                f"  - text: {b.text}",
                "",
            ]

    if result.dropped_with_reason:
        lines.append("## ⚠️  Dropped with logged reason")
        lines.append("")
        for b, reason in result.dropped_with_reason:
            anchors_summary = ", ".join(f"{a.raw}" for a in b.anchors)
            lines += [
                f"- `{b.bullet_id}` — {b.company} / {b.position_title}",
                f"  - anchors: {anchors_summary}",
                f"  - reason: {reason}",
                f"  - text: {b.text}",
                "",
            ]

    if result.kept:
        lines.append("## ✅ Anchors preserved")
        lines.append("")
        for b in result.kept:
            anchors_summary = ", ".join(f"{a.raw}" for a in b.anchors)
            lines += [f"- `{b.bullet_id}` — {anchors_summary} — {b.company}"]
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "Bullet",
    "GuardResult",
    "check_anchors",
    "find_anchors_in_text",
    "load_decisions",
    "load_selection",
    "parse_master_bullets",
    "render_diff_md",
]
