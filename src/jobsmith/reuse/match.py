"""jobsmith.reuse.match — tiered requirement matcher.

``match(req, conn, fuzzy_cutoff)`` resolves a raw requirement phrase against
prior entries in ``canonical_requirements`` using three ordered tiers:

  Tier 1 — exact_tag:
      The phrase has a canonical tag (via taxonomy).  Find any stored row
      whose payload carries the same tag.  If found → reuse.

  Tier 2 — normalized_phrase:
      The normalized phrase exactly equals a stored normalized_phrase.
      → reuse.

  Tier 3 — fuzzy:
      Token-set ratio (stdlib only — no rapidfuzz/thefuzz dependency).
      Compared against all stored normalized phrases.  Best score above
      *fuzzy_cutoff* → reuse; below → regenerate.

Design decisions
----------------
- correctness-first: when uncertain (below cutoff), regenerate.
- Fuzzy backend: in-process token-set ratio over normalized phrases.
  No FTS5, no embeddings.  Local-first, deterministic, testable.
- Token-set ratio implementation: stdlib set operations.
  token_set_ratio(a, b) = |intersection(tokens_a, tokens_b)| /
                           max(|tokens_a|, |tokens_b|, 1)
  This is deliberately simpler than the full rapidfuzz token-set ratio
  (which also computes partial sorted-token ratios).  It is sufficient for
  short requirement phrases and avoids any new dependency.  If a richer
  algorithm is needed later, swap in rapidfuzz here without changing callers.
- The fuzzy_cutoff parameter is read by callers from
  ``JobsmithConfig().reuse.fuzzy_cutoff`` — never hardcoded here.
- MatchResult is a dataclass (not TypedDict) so slice-4 and slice-5 callers
  get attribute access and can isinstance-check if needed.

Slice 4 (evidence map) and slice 5 (JD dedup) consumers:
  They call ``match(req, conn, fuzzy_cutoff)`` and read:
    result.decision       — "reuse" | "regenerate"
    result.tier           — "exact_tag" | "normalized_phrase" | "fuzzy" | "none"
    result.matched_hash   — content_hash of the matched row, or None
    result.canonical_tag  — tag:* string if tier==exact_tag, else None
    result.similarity     — float 0.0-1.0 (1.0 for exact matches)
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from jobsmith.reuse.canonicalize import canonicalize

_WS_RE = re.compile(r"\s+")


def _tokenize(phrase: str) -> frozenset[str]:
    """Split a normalized phrase into a frozenset of tokens."""
    return frozenset(_WS_RE.split(phrase.strip().lower())) - {""}


def _token_set_ratio(a: str, b: str) -> float:
    """Compute a token-set similarity ratio between two normalized phrases.

    Returns a float in [0.0, 1.0].  Two identical phrases → 1.0.
    Completely disjoint token sets → 0.0.

    Algorithm: intersection size / max(|set_a|, |set_b|).
    This is a simple but effective proxy for token-set similarity on
    short requirement phrases (typically 3-10 tokens).
    """
    set_a = _tokenize(a)
    set_b = _tokenize(b)
    denom = max(len(set_a), len(set_b), 1)
    return len(set_a & set_b) / denom


@dataclass
class MatchResult:
    """Result returned by :func:`match`.

    Attributes
    ----------
    decision:
        ``"reuse"`` — a good match was found; ``"regenerate"`` — no match.
    tier:
        Which tier produced the match: ``"exact_tag"``, ``"normalized_phrase"``,
        ``"fuzzy"``, or ``"none"`` (when decision is ``"regenerate"``).
    matched_hash:
        ``content_hash`` of the matched ``canonical_requirements`` row, or
        ``None`` when decision is ``"regenerate"``.
    canonical_tag:
        The canonical tag (e.g. ``"tag:sql"``) when tier is ``"exact_tag"``,
        else ``None``.
    similarity:
        Similarity score used for the match (1.0 for exact matches, < 1.0
        for fuzzy, 0.0 when no match).
    """

    decision: str  # "reuse" | "regenerate"
    tier: str  # "exact_tag" | "normalized_phrase" | "fuzzy" | "none"
    matched_hash: str | None
    canonical_tag: str | None
    similarity: float


def match(
    req: str,
    conn: sqlite3.Connection,
    *,
    fuzzy_cutoff: float = 0.85,
) -> MatchResult:
    """Match *req* against stored canonical requirements.

    Parameters
    ----------
    req:
        Raw requirement phrase to match.
    conn:
        Open SQLite connection to the pipeline DB (must have
        ``canonical_requirements`` table).
    fuzzy_cutoff:
        Minimum similarity ratio for a fuzzy match to count as reuse.
        Values below this threshold cause ``decision="regenerate"``.
        Read from ``JobsmithConfig().reuse.fuzzy_cutoff`` by callers.

    Returns
    -------
    MatchResult
        See :class:`MatchResult` for field documentation.
    """
    canonical_tag, normalized = canonicalize(req)

    rows = conn.execute(
        "SELECT content_hash, payload FROM canonical_requirements"
    ).fetchall()

    # --- Tier 1: exact tag match ---
    if canonical_tag is not None:
        for row in rows:
            try:
                payload = json.loads(row["payload"] if isinstance(row, sqlite3.Row) else row[1])
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            if payload.get("canonical_tag") == canonical_tag:
                h = row["content_hash"] if isinstance(row, sqlite3.Row) else row[0]
                return MatchResult(
                    decision="reuse",
                    tier="exact_tag",
                    matched_hash=h,
                    canonical_tag=canonical_tag,
                    similarity=1.0,
                )

    # --- Tier 2: normalized-phrase equality ---
    for row in rows:
        try:
            payload = json.loads(row["payload"] if isinstance(row, sqlite3.Row) else row[1])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        if payload.get("normalized_phrase") == normalized:
            h = row["content_hash"] if isinstance(row, sqlite3.Row) else row[0]
            return MatchResult(
                decision="reuse",
                tier="normalized_phrase",
                matched_hash=h,
                canonical_tag=payload.get("canonical_tag"),
                similarity=1.0,
            )

    # --- Tier 3: fuzzy ---
    best_score = 0.0
    best_hash: str | None = None
    best_tag: str | None = None

    for row in rows:
        try:
            payload = json.loads(row["payload"] if isinstance(row, sqlite3.Row) else row[1])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        stored_phrase = payload.get("normalized_phrase", "")
        if not stored_phrase:
            continue
        score = _token_set_ratio(normalized, stored_phrase)
        if score > best_score:
            best_score = score
            best_hash = row["content_hash"] if isinstance(row, sqlite3.Row) else row[0]
            best_tag = payload.get("canonical_tag")

    if best_score >= fuzzy_cutoff:
        return MatchResult(
            decision="reuse",
            tier="fuzzy",
            matched_hash=best_hash,
            canonical_tag=best_tag,
            similarity=best_score,
        )

    return MatchResult(
        decision="regenerate",
        tier="none",
        matched_hash=None,
        canonical_tag=canonical_tag,
        similarity=best_score,
    )


__all__ = ["MatchResult", "match"]
