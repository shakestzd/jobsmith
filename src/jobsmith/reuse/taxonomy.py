"""jobsmith.reuse.taxonomy — canonical skill/requirement tag taxonomy.

Loads the versioned seed from ``taxonomy_seed.yaml`` (same directory) and
provides ``resolve_tag`` for alias → tag lookups.

Design decisions
----------------
- The seed is a plain YAML file so adding a new synonym requires only a
  YAML edit — no Python code change.  The path for contributors is:

    1. Open ``taxonomy_seed.yaml``.
    2. Append a synonym to an existing tag's ``aliases`` list, OR
    3. Add a brand-new ``tag:*`` entry with its ``aliases`` list.
    4. No code changes, no migration.  The next process start picks it up.

- Lookups are against a flat alias-to-tag dict built at load time for O(1)
  performance.  The taxonomy is small (<500 aliases) so the map fits in
  memory without concern.

- ``load_taxonomy()`` is intentionally free-standing (no global state) so
  callers can inject a custom taxonomy dict in tests without patching.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_SEED_PATH = Path(__file__).parent / "taxonomy_seed.yaml"


def load_taxonomy(path: Path | None = None) -> dict[str, dict]:
    """Load and return the taxonomy as a dict keyed by canonical tag.

    Parameters
    ----------
    path:
        Override the seed file path (useful in tests).  Defaults to the
        bundled ``taxonomy_seed.yaml`` next to this module.

    Returns
    -------
    dict[str, dict]
        ``{tag_key: {"description": str, "aliases": list[str]}}``
    """
    seed_path = path or _SEED_PATH
    with seed_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return {tag: entry for tag, entry in raw.items() if isinstance(entry, dict)}


def build_alias_map(taxonomy: dict[str, dict]) -> dict[str, str]:
    """Return a flat ``{alias: tag_key}`` dict for O(1) lookups.

    Aliases are stored lowercased in the seed; the map preserves that
    convention so callers only need to normalize their input to lowercase
    before lookup.
    """
    alias_map: dict[str, str] = {}
    for tag_key, entry in taxonomy.items():
        for alias in entry.get("aliases", []):
            alias_map[alias.strip().lower()] = tag_key
    return alias_map


def resolve_tag(phrase: str, taxonomy: dict[str, dict]) -> str | None:
    """Return the canonical tag for *phrase*, or None if unrecognized.

    Parameters
    ----------
    phrase:
        Raw requirement phrase (any case, any surrounding whitespace).
    taxonomy:
        Taxonomy dict as returned by :func:`load_taxonomy`.  Pass a custom
        dict in tests to avoid hitting the seed file.

    Returns
    -------
    str | None
        Canonical tag (e.g. ``"tag:sql"``) or ``None``.
    """
    key = phrase.strip().lower()
    alias_map = build_alias_map(taxonomy)
    return alias_map.get(key)


__all__ = ["build_alias_map", "load_taxonomy", "resolve_tag"]
