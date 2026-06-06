"""jobsmith.reuse.canonicalize — normalize a raw requirement phrase.

Given a raw requirement string, emits ``(canonical_tag | None, normalized_phrase)``.

Design decisions
----------------
- Normalization: lowercase + strip surrounding whitespace + collapse internal
  runs of whitespace to a single space.  This is intentionally minimal — we
  want the normalized phrase to be human-readable, not over-stemmed.
- Tag resolution: delegates to :mod:`jobsmith.reuse.taxonomy`.  The taxonomy
  dict is loaded once at module import and cached in ``_TAXONOMY`` / ``_ALIAS_MAP``
  so repeated calls pay no I/O cost.
- No external dependencies: only stdlib + PyYAML (already a project dep for
  config loading).
"""
from __future__ import annotations

import re

from jobsmith.reuse.taxonomy import build_alias_map, load_taxonomy

# Module-level cache — loaded once per interpreter session.
_TAXONOMY = load_taxonomy()
_ALIAS_MAP = build_alias_map(_TAXONOMY)

_WS_RE = re.compile(r"\s+")


def _normalize(raw: str) -> str:
    """Lowercase, strip surrounding whitespace, collapse internal whitespace."""
    return _WS_RE.sub(" ", raw.strip().lower())


def canonicalize(raw: str) -> tuple[str | None, str]:
    """Return ``(canonical_tag_or_None, normalized_phrase)`` for *raw*.

    Parameters
    ----------
    raw:
        Raw requirement phrase as it appears in the JD.

    Returns
    -------
    tuple[str | None, str]
        - ``canonical_tag``: e.g. ``"tag:sql"`` if the phrase is a known
          synonym, else ``None``.
        - ``normalized_phrase``: lowercased, whitespace-collapsed version of
          *raw*.  Always a non-empty string when *raw* is non-empty.

    Examples
    --------
    >>> canonicalize("Advanced SQL")
    ('tag:sql', 'advanced sql')
    >>> canonicalize("some unknown requirement XYZ")
    (None, 'some unknown requirement xyz')
    """
    normalized = _normalize(raw)
    tag = _ALIAS_MAP.get(normalized)
    return tag, normalized


__all__ = ["canonicalize"]
