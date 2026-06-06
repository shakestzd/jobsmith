"""Company-research cache helpers.

Cache convention: `private/companies/<slug>.md` holds synthesised research for
one company. A cache hit avoids a WebFetch round-trip when a second application
is made to the same company within N days (default 30).

Cross-role reuse: `cache_path_for` now delegates key derivation to
`jobsmith.reuse.company_cache.normalize_company_key` so variant spellings
("Acme, Inc.", "Acme Inc", "ACME") all resolve to the same cache file.

Public API:
    slugify(company_name) -> str
        Lower-case, hyphen-separated identifier derived from a company name.
        (Legacy helper — new callers should prefer normalize_company_key from
        jobsmith.reuse.company_cache.)

    is_fresh(path, window_days=30) -> bool
        True when *path* exists and its mtime is within *window_days*.

    cache_path_for(company_name, repo_root=None) -> Path
        Return the canonical cache path for a company slug (uses normalized key).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

_DEFAULT_WINDOW_DAYS = 30


def slugify(company_name: str) -> str:
    """Convert a company name to a lowercase hyphen-separated slug.

    Examples:
        "Acme Corp"      → "acme-corp"
        "PwC"            → "pwc"
        "Microsoft Corp." → "microsoft-corp"
        "Smith & Wesson" → "smith-wesson"
    """
    name = company_name.strip().lower()
    # Replace any character that is not alphanumeric or whitespace with a space
    name = re.sub(r"[^\w\s]", " ", name)
    # Replace underscores (included in \w) with spaces
    name = name.replace("_", " ")
    # Collapse multiple whitespace into a single space
    name = re.sub(r"\s+", " ", name).strip()
    # Replace spaces with hyphens
    return name.replace(" ", "-")


def is_fresh(path: Path, window_days: int = _DEFAULT_WINDOW_DAYS) -> bool:
    """Return True when *path* exists and its mtime is within *window_days*."""
    try:
        mtime = path.stat().st_mtime
    except (OSError, FileNotFoundError):
        return False
    age_days = (time.time() - mtime) / 86400
    return age_days <= window_days


def cache_path_for(company_name: str, repo_root: Path | None = None) -> Path:
    """Return the canonical cache path for a company's research file.

    Path: <repo_root>/private/companies/<slug>.md
    When *repo_root* is omitted, returns a relative path (`private/companies/<slug>.md`).

    Note: This legacy helper uses `slugify` for backward compatibility with
    existing cache files. New cross-role reuse goes through
    `jobsmith.reuse.company_cache.check_cache` / `write_cache`, which use
    `normalize_company_key` to strip legal suffixes for variant-tolerant matching.
    """
    slug = slugify(company_name)
    base = (repo_root / "private" / "companies") if repo_root else Path("private") / "companies"
    return base / f"{slug}.md"


__all__ = ["cache_path_for", "is_fresh", "slugify"]
