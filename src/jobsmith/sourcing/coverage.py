"""Master-bullets digest builder for coverage scoring (feat-6ec8c30a).

Reads the master_content table directly and compresses its bullets + skills
into a plain-text digest, section-grouped, hard-capped at MAX_DIGEST_CHARS
(~500 tokens / ~2000 chars).

Public API
----------
build_master_digest(conn) -> str
    Pure function over a sqlite3 Connection.  No LLM calls, no side effects.
    Deterministic: same DB content always produces the same digest.

MAX_DIGEST_CHARS
    Hard cap on digest length (2000 characters).
"""

from __future__ import annotations

import sqlite3
from typing import Any

import yaml

__all__ = ["MAX_DIGEST_CHARS", "build_master_digest"]

# Hard cap: ~500 tokens @ ~4 chars/token
MAX_DIGEST_CHARS = 2000

# Section display order — deterministic, descending by relevance to JD scoring.
# work and skill carry bullets; education and author are omitted (low signal).
_SECTION_ORDER = ["work", "skill", "education", "author"]

_EMPTY_MARKER = "[no master content loaded]"

# Truncation sentinel appended when bullets are cut
_TRUNCATED_MARKER = "...[truncated]"


def build_master_digest(conn: sqlite3.Connection) -> str:
    """Build a plain-text digest of master bullets, hard-capped at MAX_DIGEST_CHARS.

    Parameters
    ----------
    conn:
        Open sqlite3 connection to a pipeline DB (with master_content table).

    Returns
    -------
    str
        A section-grouped digest of master bullets and skills.
        Returns ``_EMPTY_MARKER`` when master_content is empty.
        Guaranteed ``len(result) <= MAX_DIGEST_CHARS``.

    Notes
    -----
    - Deterministic: same DB rows → identical digest.
    - Pure function: reads from DB, no writes, no LLM calls.
    - Bullets stored as dict-form ``{bullet: ..., ...}`` are unwrapped to their
      text via the ``bullet`` key.
    - Only work and skill sections contribute meaningful bullet text; education
      and author are included if present but low-signal rows are skipped.
    """
    rows = conn.execute(
        "SELECT section, content_blob FROM master_content ORDER BY section"
    ).fetchall()

    if not rows:
        return _EMPTY_MARKER

    # Parse each section's YAML blob into bullet lines.
    section_lines: dict[str, list[str]] = {}
    for row in rows:
        section = row["section"] if hasattr(row, "keys") else row[0]
        blob = row["content_blob"] if hasattr(row, "keys") else row[1]
        lines = _extract_lines(section, blob)
        if lines:
            section_lines[section] = lines

    if not section_lines:
        return _EMPTY_MARKER

    # Assemble digest in deterministic order.
    # Sort by canonical order first, then alphabetically for unknown sections.
    ordered_sections = sorted(
        section_lines.keys(),
        key=lambda s: (_SECTION_ORDER.index(s) if s in _SECTION_ORDER else len(_SECTION_ORDER), s),
    )

    return _assemble_digest(ordered_sections, section_lines)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _extract_bullet_text(detail: Any) -> str | None:
    """Extract text from a detail entry (plain string or dict-form bullet)."""
    if isinstance(detail, str):
        return detail.strip()
    if isinstance(detail, dict):
        text = detail.get("bullet") or detail.get("text")
        if isinstance(text, str):
            return text.strip()
    return None


def _extract_lines(section: str, blob: str) -> list[str]:
    """Parse a YAML content_blob and return a flat list of relevant text lines."""
    try:
        data = yaml.safe_load(blob)
    except yaml.YAMLError:
        return []

    if not isinstance(data, list):
        return []

    lines: list[str] = []

    if section == "skill":
        # Skills: collect category title + individual detail items
        for entry in data:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title", "")
            if title:
                lines.append(f"  {title}:")
            for detail in entry.get("details", []):
                text = _extract_bullet_text(detail)
                if text:
                    lines.append(f"    - {text}")
    else:
        # Work/education/author: collect position title + bullet details
        for entry in data:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title", "")
            org = entry.get("location", "")
            date = entry.get("date", "")
            header_parts = [p for p in [title, org, date] if p]
            if header_parts:
                lines.append(f"  {' | '.join(header_parts)}:")
            for detail in entry.get("details", []):
                text = _extract_bullet_text(detail)
                if text:
                    lines.append(f"    - {text}")

    return lines


def _assemble_digest(
    ordered_sections: list[str],
    section_lines: dict[str, list[str]],
) -> str:
    """Assemble the final digest string, respecting MAX_DIGEST_CHARS."""
    # Build section blocks and track cumulative length.
    parts: list[str] = []
    total = 0
    truncated = False

    for section in ordered_sections:
        if truncated:
            break

        header = f"[{section.upper()}]"
        block_lines = [header] + section_lines[section]
        block = "\n".join(block_lines) + "\n"

        remaining = MAX_DIGEST_CHARS - total - len(_TRUNCATED_MARKER) - 1

        if len(block) <= remaining + len(_TRUNCATED_MARKER) + 1:
            # Block fits in full
            parts.append(block)
            total += len(block)
        else:
            # Partial block — include as many lines as fit.
            current: list[str] = []
            for line in block_lines:
                candidate = "\n".join(current + [line]) + "\n"
                if len(candidate) + len(_TRUNCATED_MARKER) + 1 > MAX_DIGEST_CHARS - total:
                    break
                current.append(line)

            if current:
                partial = "\n".join(current) + "\n" + _TRUNCATED_MARKER + "\n"
                parts.append(partial)
                total += len(partial)
            else:
                # Not even the header fits — append truncation marker and stop
                parts.append(_TRUNCATED_MARKER + "\n")
                total += len(_TRUNCATED_MARKER) + 1
            truncated = True

    result = "".join(parts).rstrip("\n")

    # Final safety clamp (should never trigger, but keeps the contract ironclad)
    if len(result) > MAX_DIGEST_CHARS:
        result = result[: MAX_DIGEST_CHARS - len(_TRUNCATED_MARKER)] + _TRUNCATED_MARKER

    return result
