"""Known-gaps harvest and matcher for coverage sourcing (feat-d20ff292).

Reads halt envelopes from apply_state, extracts distinctive gap terms,
and matches them against posting JD text at crawl time.

Public API
----------
harvest_known_gaps(conn) -> list[dict]
    Read all apply_state rows with status=halt, extract gap strings from
    must_have and (when present) additional_uncovered_must_haves, filter
    out any gap whose terms already appear in the current master digest,
    and return deduplicated gap dicts ready for match_posting.

extract_gap_terms(gap: str) -> list[str]
    Derive 1-3 distinctive lowercase terms from a gap string.
    Applies a curated stoplist, min token length 3, and deduplication.
    Returns multi-word tokens first (they are more distinctive).

match_posting(jd_text: str, gaps: list[dict]) -> list[dict]
    Case-insensitive search for each gap's terms in jd_text.
    Returns [{gap: <label>, term: <matched term>}, ...], one entry per
    matching gap (not per term — first matching term wins per gap).

Design decisions
----------------
- No new dependencies: term extraction is purely string-based.
- Conservative extraction: only distinctive multi-word or domain terms,
  min length 3, curated stoplist for generic words.
- gap_hits_json payload shape: [{gap: <short label>, term: <matched term>}].
- Gaps are revalidated against the master digest at every harvest — when
  the user adds the missing bullet the gap stops matching and badges
  disappear on the next crawl.
- Asymmetric halt envelope shapes (GitLab has no additional_uncovered_must_haves,
  Arcadia has it): handled defensively, fall back to must_have alone.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

logger = logging.getLogger("jobsmith.sourcing.gaps")

__all__ = ["extract_gap_terms", "harvest_known_gaps", "match_posting"]

# ---------------------------------------------------------------------------
# Stoplist — generic words that are too broad to match meaningfully
# ---------------------------------------------------------------------------

_STOPLIST: frozenset[str] = frozenset(
    {
        "experience",
        "data",
        "modern",
        "strong",
        "years",
        "ability",
        "knowledge",
        "skills",
        "skill",
        "work",
        "working",
        "using",
        "large",
        "scale",
        "team",
        "teams",
        "build",
        "building",
        "production",
        "proficiency",
        "proficient",
        "understanding",
        "similar",
        "relevant",
        "proven",
        "demonstrated",
        "track",
        "record",
        "technical",
        "tool",
        "tools",
        "direct",
        "deep",
        "solid",
        "good",
        "great",
        "excellent",
        "with",
        "and",
        "the",
        "for",
        "of",
        "in",
        "or",
        "to",
        "a",
        "an",
        "is",
        "are",
        "at",
        "by",
        "as",
        "on",
        "more",
        "than",
        "such",
        "least",
        "any",
        "some",
        "well",
        "including",
        "required",
        "preferred",
        "plus",
    }
)

# Domain terms that are distinctive even as single words (not on stoplist)
# These represent the curated vocabulary mentioned in the plan.
_DOMAIN_TERMS: frozenset[str] = frozenset(
    {
        "dbt",
        "llm",
        "llms",
        "langchain",
        "langgraph",
        "rag",
        "hedis",
        "stars",
        "ehr",
        "etl",
        "mlops",
        "mlflow",
        "airflow",
        "kafka",
        "spark",
        "flink",
        "terraform",
        "kubernetes",
        "docker",
        "sql",
        "nosql",
        "redis",
        "postgres",
        "postgresql",
        "bigquery",
        "snowflake",
        "databricks",
        "looker",
        "tableau",
        "fivetran",
        "airbyte",
        "duckdb",
        "dagster",
        "prefect",
        "polars",
        "rust",
        "golang",
        "scala",
        "java",
        "python",
        "typescript",
    }
)

# Curated multi-word phrases to check first — these are more distinctive than
# individual tokens and are the primary over-match prevention mechanism.
_MULTI_WORD_PHRASES: list[str] = [
    "healthcare claims",
    "health claims",
    "claims data",
    "claims processing",
    "risk adjustment",
    "ehr data",
    "ehr integration",
    "dbt cloud",
    "dbt core",
    "dbt production",
    "langchain",
    "langgraph",
    "vector database",
    "vector databases",
    "llm evaluation",
    "llm-powered",
    "llm powered",
    "ai agents",
    "ai agent",
    "rag pipeline",
    "rag pipelines",
]


def extract_gap_terms(gap: str) -> list[str]:
    """Derive 1-3 distinctive lowercase terms from a gap string.

    Parameters
    ----------
    gap:
        A single gap string (one item from must_have or
        additional_uncovered_must_haves).

    Returns
    -------
    list[str]
        1-3 lowercase distinctive terms.  Multi-word phrases are preferred
        over single tokens.  Generic words (stoplist) are excluded.
        All terms have length >= 3.
    """
    gap_lower = gap.lower()
    terms: list[str] = []

    # Phase 1: check curated multi-word phrases (most distinctive).
    # Cap at 2 multi-word phrases to leave room for a single domain term
    # (prevents 3 near-duplicate phrase variants crowding out shorter tokens).
    seen_phrase_roots: set[str] = set()
    for phrase in _MULTI_WORD_PHRASES:
        if phrase in gap_lower:
            # Use the first word of the phrase as a dedup root so e.g.
            # "vector database" and "vector databases" don't both count.
            root = phrase.split()[0]
            if root in seen_phrase_roots:
                continue
            seen_phrase_roots.add(root)
            terms.append(phrase)
            if len(terms) >= 2:
                break

    # Phase 2: tokenise and check domain-specific single words
    tokens = re.split(r"[\s,;()+/\-—]+", gap_lower)
    for token in tokens:
        # Strip trailing punctuation and brackets
        token = token.strip(".'\"!?:)([]")
        if len(token) < 3:
            continue
        if token in _STOPLIST:
            continue
        if token in _DOMAIN_TERMS and token not in terms:
            terms.append(token)
            if len(terms) >= 3:
                return terms

    # Phase 3: fall back to any meaningful non-stoplist token >= 5 chars
    if not terms:
        for token in tokens:
            token = token.strip(".'\"!?:)([]")
            if len(token) < 5:
                continue
            if token in _STOPLIST:
                continue
            if token not in terms:
                terms.append(token)
                if len(terms) >= 3:
                    break

    return terms[:3]


def harvest_known_gaps(conn: sqlite3.Connection) -> list[dict]:
    """Read all apply_state halt envelopes and return active gap dicts.

    Parameters
    ----------
    conn:
        Open sqlite3 connection to the pipeline DB.

    Returns
    -------
    list[dict]
        Each dict has shape ``{"gap": <short label str>, "terms": [str, ...]}``.
        Deduplicated by gap label.
        Gaps whose extracted terms all appear in the current master digest are
        dropped (fixed-gap expiry).
    """
    from ..sourcing.coverage import build_master_digest

    # Fetch all apply_state rows (any kind) — filter by status=halt below
    rows = conn.execute(
        "SELECT slug, kind, content_blob FROM apply_state"
    ).fetchall()

    # Build set of gap strings from halt envelopes
    gap_strings: list[str] = []
    seen_gaps: set[str] = set()

    for row in rows:
        blob = row["content_blob"] if hasattr(row, "keys") else row[2]
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        status = str(data.get("status") or "").lower()
        if status != "halt":
            continue

        # Extract must_have (always present in halt envelopes)
        must_have = data.get("must_have") or []
        if isinstance(must_have, list):
            for item in must_have:
                if isinstance(item, str) and item not in seen_gaps:
                    seen_gaps.add(item)
                    gap_strings.append(item)

        # Extract additional_uncovered_must_haves WHEN PRESENT (Arcadia shape)
        additional = data.get("additional_uncovered_must_haves") or []
        if isinstance(additional, list):
            for item in additional:
                if isinstance(item, str) and item not in seen_gaps:
                    seen_gaps.add(item)
                    gap_strings.append(item)

    if not gap_strings:
        return []

    # Build master digest for revalidation — drop gaps whose terms are now covered
    master_digest = build_master_digest(conn).lower()

    result: list[dict] = []
    for gap_str in gap_strings:
        terms = extract_gap_terms(gap_str)
        if not terms:
            continue

        # Fixed-gap expiry: if ANY term now appears in master digest, drop this gap.
        # Rationale: the master digest terms are distinctive (min-length, stoplist-filtered),
        # so a match means the user has added relevant content addressing this gap.
        if any(term in master_digest for term in terms):
            logger.debug("Gap expired (covered by master): %s", gap_str[:80])
            continue

        # Short label for the gap (80 chars max, same as plan vocabulary)
        label = gap_str[:80] if len(gap_str) > 80 else gap_str

        result.append({"gap": label, "terms": terms})

    return result


def match_posting(jd_text: str, gaps: list[dict]) -> list[dict]:
    """Match gap terms against a posting's JD text.

    Parameters
    ----------
    jd_text:
        Full job description text.
    gaps:
        List of gap dicts from ``harvest_known_gaps`` (each has "gap" and "terms").

    Returns
    -------
    list[dict]
        Hits with shape ``[{gap: <label>, term: <matched term>}, ...]``.
        One entry per matching gap (first matching term wins).
        Empty list when no matches.
    """
    if not jd_text or not gaps:
        return []

    jd_lower = jd_text.lower()
    hits: list[dict] = []

    for gap_dict in gaps:
        label = gap_dict.get("gap", "")
        terms = gap_dict.get("terms") or []

        for term in terms:
            if term in jd_lower:
                hits.append({"gap": label, "term": term})
                break  # First matching term wins per gap

    return hits
