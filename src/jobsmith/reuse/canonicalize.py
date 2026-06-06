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

Canonical requirement-hash contract (Finding #5)
-------------------------------------------------
``requirement_content_hash(req)`` is the SINGLE source of truth for the
content hash stored in ``canonical_requirements.content_hash`` and keyed by
``requirement_evidence_map.requirement_hash``.

The hash is computed over the dict
``{"canonical_tag": <tag_or_None>, "normalized_phrase": <normalized>}``.
Fields ``raw`` and any extra prompt-level fields are intentionally EXCLUDED so
that cosmetic JD wording changes (e.g. "Advanced SQL" vs "advanced SQL
experience") do NOT bust the cache as long as the normalized identity is stable.

All callers — ``db_ingest.ingest_canonical_requirements``, test helpers, and
the selector prompt — MUST derive the hash via this function or from the
``content_hash`` field that ``ingest_canonical_requirements`` stores.  The
``apply-bullet-selector`` prompt resolves the hash at runtime via
``jobsmith reuse lookup-bullet --requirement-raw "<text>"`` which internally
calls ``match()`` → ``matched_hash``.  The ``content_hash`` field is NEVER
emitted directly in the jd-parsed JSON; the Python layer always recomputes it.
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


def requirement_content_hash(req: dict) -> str:
    """Return the canonical content hash for a requirement dict.

    This is the SINGLE definition of the requirement-hash contract used by
    ``canonical_requirements.content_hash`` and
    ``requirement_evidence_map.requirement_hash``.

    The hash is over the stable identity fields only::

        {"canonical_tag": <str | None>, "normalized_phrase": <str>}

    The ``raw`` field is excluded because two JDs can phrase the same
    requirement differently (e.g. "Advanced SQL" vs "strong SQL skills"),
    yet both should resolve to the same canonical entry when the tag or
    normalized phrase matches.

    Parameters
    ----------
    req:
        A requirement item dict.  Must contain at least
        ``canonical_tag`` (``str | None``) and ``normalized_phrase`` (``str``).
        Extra keys (e.g. ``raw``) are ignored.

    Returns
    -------
    str
        SHA-256 hex digest (64 chars) over the normalized identity payload.
    """
    from jobsmith.reuse.store import content_hash

    identity = {
        "canonical_tag": req.get("canonical_tag"),
        "normalized_phrase": req.get("normalized_phrase", ""),
    }
    return content_hash(identity)


__all__ = ["canonicalize", "requirement_content_hash"]
