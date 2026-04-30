"""Anchor-bullet preservation guardrail.

Scans master work.yml for "anchor" bullets — those with high-impact metrics
that must survive JD tailoring unless an explicit reason is logged. Reuses
the money / percent regexes from fact_check_draft.py (no reimplementation).

Anchor thresholds (frozen in specialist-contracts.yaml):
    - dollar amount >= $10M
    - percentage >= 50%
    - asset count >= 100K (solar assets, customers, accounts, etc.)

Exit codes:
    0  all anchors preserved, or all drops have logged reasons
    1  one or more anchors dropped without logged reason
    2  internal error (master missing, JSON malformed, etc.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Reuse the regex library from fact_check_draft.py — explicit import,
# no reimplementation. If fact_check_draft.py moves, both fail together.
sys.path.insert(0, str(Path(__file__).parent))
from fact_check_draft import _MONEY_RE, _PERCENT_RE  # noqa: E402

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

# Anchor thresholds — must match specialist-contracts.yaml.
MONEY_THRESHOLD_USD = 10_000_000
PERCENT_THRESHOLD = 50.0
ASSET_COUNT_THRESHOLD = 100_000


# ---------- types ----------


@dataclass
class Anchor:
    """One anchor metric within a bullet (a bullet may have several)."""

    kind: str  # 'money' | 'percent' | 'asset_count'
    raw: str   # the matched text, e.g. "$250M" or "75%"
    value: float  # normalized — USD for money, percent points, raw count


@dataclass
class Bullet:
    bullet_id: str            # stable 12-char hash of the bullet text
    text: str
    company: str
    position_title: str
    position_index: int
    bullet_index: int
    anchors: list[Anchor] = field(default_factory=list)

    @property
    def is_anchor(self) -> bool:
        return bool(self.anchors)


@dataclass
class GuardResult:
    exit_code: int
    anchor_bullets: list[Bullet]
    kept: list[Bullet]
    dropped_without_reason: list[Bullet]
    dropped_with_reason: list[tuple[Bullet, str]]


# ---------- parsers ----------


def _bullet_id(text: str) -> str:
    """Stable, content-derived id. SHA-1 hex first 12 chars."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _parse_money(raw: str) -> float | None:
    """Convert '$250M' -> 250_000_000, '$1.5B' -> 1_500_000_000, '$95K' -> 95_000."""
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


def find_anchors_in_text(text: str) -> list[Anchor]:
    """Detect every anchor metric in a single bullet's text."""
    anchors: list[Anchor] = []

    for m in _MONEY_RE.finditer(text):
        val = _parse_money(m.group(0))
        if val is not None and val >= MONEY_THRESHOLD_USD:
            anchors.append(Anchor(kind="money", raw=m.group(0), value=val))

    for m in _PERCENT_RE.finditer(text):
        val = _parse_percent(m.group(0))
        if val is not None and val >= PERCENT_THRESHOLD:
            anchors.append(Anchor(kind="percent", raw=m.group(0), value=val))

    for m in _ASSET_COUNT_RE.finditer(text):
        val = _parse_asset_count(m.group(1), m.group(2))
        if val is not None and val >= ASSET_COUNT_THRESHOLD:
            anchors.append(Anchor(kind="asset_count", raw=m.group(0), value=val))

    return anchors


def parse_master_bullets(master_path: Path) -> list[Bullet]:
    """Read master work.yml and return all bullets with anchor annotations."""
    data = yaml.safe_load(master_path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"{master_path} root must be a list of positions")

    out: list[Bullet] = []
    for pi, pos in enumerate(data):
        company = pos.get("location", "")
        title = pos.get("title", "")
        for bi, text in enumerate(pos.get("details", []) or []):
            anchors = find_anchors_in_text(text)
            out.append(
                Bullet(
                    bullet_id=_bullet_id(text),
                    text=text,
                    company=company,
                    position_title=title,
                    position_index=pi,
                    bullet_index=bi,
                    anchors=anchors,
                )
            )
    return out


def load_selection(selection_path: Path) -> dict[str, dict]:
    """Index bullet-selection.json by master_bullet_id.

    Selection shape (per contract):
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
    if not decisions_path.exists():
        return {}
    data = json.loads(decisions_path.read_text())
    return {k: v for k, v in data.items() if isinstance(v, str)}


# ---------- core ----------


def evaluate(
    master_path: Path,
    selection_path: Path,
    decisions_path: Path,
) -> GuardResult:
    bullets = parse_master_bullets(master_path)
    anchors = [b for b in bullets if b.is_anchor]
    selection = load_selection(selection_path)
    decisions = load_decisions(decisions_path)

    kept: list[Bullet] = []
    dropped_with_reason: list[tuple[Bullet, str]] = []
    dropped_without_reason: list[Bullet] = []

    for b in anchors:
        sel = selection.get(b.bullet_id)
        if sel is None:
            # No selection entry yet — treat as missing if a selection file
            # was provided. If no selection file, all anchors are "preserved
            # by default" (selector hasn't run yet).
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

        reason = (
            sel.get("reason_if_dropped")
            or decisions.get(b.bullet_id, "")
        ).strip()
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


# ---------- diff renderer ----------


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


# ---------- cli ----------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--master", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--decisions", type=Path, required=True)
    ap.add_argument("--diff-out", type=Path, required=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        result = evaluate(args.master, args.selection, args.decisions)
    except FileNotFoundError as e:
        print(f"anchor-guard: missing file {e.filename}", file=sys.stderr)
        return 2
    except (ValueError, yaml.YAMLError, json.JSONDecodeError) as e:
        print(f"anchor-guard: parse error {e}", file=sys.stderr)
        return 2

    args.diff_out.parent.mkdir(parents=True, exist_ok=True)
    args.diff_out.write_text(render_diff_md(result, args.master))

    if not args.quiet:
        print(
            f"anchor-guard: {len(result.kept)} kept, "
            f"{len(result.dropped_with_reason)} dropped-with-reason, "
            f"{len(result.dropped_without_reason)} dropped-without-reason "
            f"(exit {result.exit_code})"
        )

    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
