"""jobsmith.reuse.company_cache — company-key normalization and cross-role file-cache helpers.

Extends the existing file-based company research cache (research.py) so that
applications to the same company under different name spellings or for different
roles all share one cache entry.

Public API
----------
normalize_company_key(company_name) -> str
    Strip legal suffixes and common stop-words, then produce a stable slug.
    "Acme, Inc.", "Acme Inc", "ACME" all map to "acme".

check_cache(company_name, *, companies_dir, ttl_days) -> str | None
    Return cached research content when a fresh file exists; None otherwise.

write_cache(company_name, content, *, companies_dir) -> Path
    Write research content to the canonical cache path and return it.

record_company_research_metric(conn, *, slug, outcome) -> None
    Write metric_key="company_research_source", metric_value=outcome ("reused"
    or "generated") to the run_metrics table for slice-9 reporting.

Metric key documented for slice-9 consumers
--------------------------------------------
  metric_key  : "company_research_source"
  metric_value: "reused" | "generated"
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jobsmith.research import is_fresh, slugify

# ---------------------------------------------------------------------------
# Legal suffixes and common stop-words stripped before comparing company names.
# Order matters: longer multi-word suffixes must precede shorter single-word ones.
# ---------------------------------------------------------------------------
_LEGAL_SUFFIXES: tuple[str, ...] = (
    "incorporated",
    "corporation",
    "limited",
    "company",
    "inc",
    "llc",
    "ltd",
    "corp",
    "co",
    "plc",
    "lp",
)

_LEADING_STOP_WORDS: tuple[str, ...] = ("the",)

# Metric key used by this module; imported and documented for slice-9.
METRIC_KEY_COMPANY_RESEARCH_SOURCE = "company_research_source"


def normalize_company_key(company_name: str) -> str:
    """Return a stable slug for *company_name* independent of legal suffix or casing.

    Steps applied in order:
      1. Lowercase + strip surrounding whitespace.
      2. Replace '&' / '+' with 'and'.
      3. Remove all punctuation (keep alphanumerics and spaces).
      4. Collapse multiple spaces.
      5. Strip leading stop-words ('the').
      6. Strip trailing legal suffixes (inc, llc, ltd, corp, co, plc, lp, …).
      7. Re-collapse spaces and replace with hyphens.

    Examples::
        normalize_company_key("Acme, Inc.")   → "acme"
        normalize_company_key("Acme Inc")     → "acme"
        normalize_company_key("ACME")         → "acme"
        normalize_company_key("The Widget Company") → "widget"
        normalize_company_key("Smith & Wesson") → "smith-and-wesson"
    """
    name = company_name.strip().lower()
    # Normalize ampersand / plus to 'and'
    name = name.replace("&", "and").replace("+", "and")
    # Remove punctuation (keep alphanumerics and spaces)
    name = re.sub(r"[^\w\s]", " ", name)
    # Underscores (in \w) → space
    name = name.replace("_", " ")
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()
    # Strip leading stop-words
    tokens = name.split()
    while tokens and tokens[0] in _LEADING_STOP_WORDS:
        tokens = tokens[1:]
    # Strip trailing legal suffixes (repeat until stable)
    changed = True
    while changed and tokens:
        changed = False
        if tokens[-1] in _LEGAL_SUFFIXES:
            tokens = tokens[:-1]
            changed = True
    # Reassemble — if all tokens were stripped fall back to full slugify
    if not tokens:
        return slugify(company_name)
    return "-".join(tokens)


def _cache_path(company_name: str, companies_dir: Path) -> Path:
    """Return the canonical .md path under *companies_dir* for *company_name*."""
    key = normalize_company_key(company_name)
    return companies_dir / f"{key}.md"


def check_cache(
    company_name: str,
    *,
    companies_dir: Path,
    ttl_days: int,
) -> str | None:
    """Return cached research content when fresh; None on miss or stale.

    Uses normalize_company_key so variant spellings of the same company
    resolve to the same cache file.

    Parameters
    ----------
    company_name:
        Raw company name from the JD (may include legal suffixes, odd casing).
    companies_dir:
        Directory that holds <key>.md research files (e.g. private/companies).
    ttl_days:
        Maximum age in whole days before the entry is considered stale.
        Read from JobsmithConfig().reuse.company_ttl_days at the call site.
    """
    path = _cache_path(company_name, companies_dir)
    if not is_fresh(path, window_days=ttl_days):
        return None
    return path.read_text(encoding="utf-8")


def write_cache(
    company_name: str,
    content: str,
    *,
    companies_dir: Path,
) -> Path:
    """Write *content* to the canonical cache path for *company_name*.

    Creates *companies_dir* if it does not exist. Returns the path written.
    """
    companies_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(company_name, companies_dir)
    path.write_text(content, encoding="utf-8")
    return path


def record_company_research_metric(
    conn: sqlite3.Connection,
    *,
    slug: str,
    outcome: str,
) -> None:
    """Record whether company research was reused or generated for *slug*.

    Writes to the ``run_metrics`` table (created by migration 009_reuse_store)
    using metric_key="company_research_source" and metric_value=outcome.

    Parameters
    ----------
    conn:
        Open SQLite connection to the jobsmith DB.
    slug:
        Application slug (e.g. "acme-swe-2024").
    outcome:
        Either "reused" (cache hit) or "generated" (fresh LLM call).

    Metric documented for slice-9 consumers
    ----------------------------------------
    table      : run_metrics
    metric_key : "company_research_source"
    metric_value: "reused" | "generated"
    """
    conn.execute(
        "INSERT OR REPLACE INTO run_metrics "
        "(slug, metric_key, metric_value, created_at) VALUES (?, ?, ?, ?)",
        (
            slug,
            METRIC_KEY_COMPANY_RESEARCH_SOURCE,
            outcome,
            datetime.now(tz=timezone.utc).isoformat(),
        ),
    )
    conn.commit()


__all__ = [
    "METRIC_KEY_COMPANY_RESEARCH_SOURCE",
    "check_cache",
    "normalize_company_key",
    "record_company_research_metric",
    "write_cache",
]
