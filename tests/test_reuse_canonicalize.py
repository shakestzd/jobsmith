"""Tests for canonicalization + tiered matching — feat-5024375d.

TDD: tests written before implementation.

Required tests:
  - test_synonyms_map_to_tag
  - test_normalize_unknown_phrase
  - test_tiered_match_thresholds
  - test_taxonomy_seed_extensible
  - test_jd_parser_emits_canonical_fields_via_ingest (integration)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from jobsmith import db as jobsmith_db

# ---------------------------------------------------------------------------
# taxonomy + canonicalize
# ---------------------------------------------------------------------------


def test_synonyms_map_to_tag():
    """Multiple synonym phrases all map to the same canonical tag."""
    from jobsmith.reuse.canonicalize import canonicalize

    # SQL synonyms
    tag_sql, _ = canonicalize("advanced sql")
    assert tag_sql == "tag:sql"

    tag_sql2, _ = canonicalize("strong sql skills")
    assert tag_sql2 == "tag:sql"

    tag_sql3, _ = canonicalize("sql proficiency")
    assert tag_sql3 == "tag:sql"

    # Python synonyms
    tag_py, _ = canonicalize("python programming")
    assert tag_py == "tag:python"

    tag_py2, _ = canonicalize("python development")
    assert tag_py2 == "tag:python"

    # Machine learning synonyms
    tag_ml, _ = canonicalize("machine learning")
    assert tag_ml == "tag:machine_learning"

    tag_ml2, _ = canonicalize("ML experience")
    assert tag_ml2 == "tag:machine_learning"


def test_normalize_unknown_phrase():
    """An unrecognized phrase returns tag=None and a normalized string."""
    from jobsmith.reuse.canonicalize import canonicalize

    tag, normalized = canonicalize("some completely unknown requirement phrase XYZ-99")
    assert tag is None
    # normalized must be lowercase, stripped
    assert normalized == normalized.lower().strip()
    assert len(normalized) > 0


def test_canonicalize_normalizes_whitespace_and_case():
    """canonicalize is case-insensitive and strips surrounding whitespace."""
    from jobsmith.reuse.canonicalize import canonicalize

    tag1, norm1 = canonicalize("  SQL Proficiency  ")
    tag2, norm2 = canonicalize("sql proficiency")
    assert tag1 == tag2
    assert norm1 == norm2


def test_taxonomy_seed_extensible():
    """Taxonomy seed YAML is loadable and adding a new tag requires no code change."""
    from jobsmith.reuse.taxonomy import load_taxonomy, resolve_tag

    tax = load_taxonomy()

    # The taxonomy must be a dict with at least a few tags
    assert isinstance(tax, dict)
    assert len(tax) >= 5

    # Each entry must have an 'aliases' list
    for tag_key, entry in tax.items():
        assert "aliases" in entry, f"Tag {tag_key!r} missing 'aliases'"
        assert isinstance(entry["aliases"], list)

    # resolve_tag uses the loaded taxonomy: known alias → tag
    tag = resolve_tag("advanced sql", tax)
    assert tag == "tag:sql"

    # Unknown phrase → None
    unknown = resolve_tag("completely unknown xyz99", tax)
    assert unknown is None

    # Extensibility: inject a new entry at runtime (simulates adding to YAML with no code)
    tax["tag:new_skill"] = {"aliases": ["brand new skill", "fresh tech"], "description": "test"}
    tag_new = resolve_tag("brand new skill", tax)
    assert tag_new == "tag:new_skill"


# ---------------------------------------------------------------------------
# match — tiered resolution
# ---------------------------------------------------------------------------


def test_tiered_match_thresholds():
    """Match returns correct tier result across all three tiers."""
    from jobsmith.reuse.match import MatchResult, match

    # --- build an in-memory DB with some canonical_requirements rows ---
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE canonical_requirements "
        "(content_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
    )

    # Insert a prior req: exact tag match row
    exact_payload = json.dumps({
        "raw": "advanced sql",
        "canonical_tag": "tag:sql",
        "normalized_phrase": "advanced sql",
    })
    conn.execute(
        "INSERT INTO canonical_requirements VALUES (?, ?, datetime('now'))",
        ("hash_sql_001", exact_payload),
    )

    # Insert a prior req: no tag, distinct normalized phrase
    phrase_payload = json.dumps({
        "raw": "proficiency in apache kafka",
        "canonical_tag": None,
        "normalized_phrase": "proficiency in apache kafka",
    })
    conn.execute(
        "INSERT INTO canonical_requirements VALUES (?, ?, datetime('now'))",
        ("hash_kafka_001", phrase_payload),
    )
    conn.commit()

    # Tier 1: exact tag match — SQL synonym hits tag:sql
    result = match("sql proficiency", conn, fuzzy_cutoff=0.85)
    assert isinstance(result, MatchResult)
    assert result.decision == "reuse"
    assert result.tier == "exact_tag"
    assert result.matched_hash == "hash_sql_001"
    assert result.canonical_tag == "tag:sql"

    # Tier 2: normalized-phrase equality
    result2 = match("proficiency in apache kafka", conn, fuzzy_cutoff=0.85)
    assert result2.decision == "reuse"
    assert result2.tier == "normalized_phrase"
    assert result2.matched_hash == "hash_kafka_001"

    # Tier 3: fuzzy above cutoff — "proficiency with apache kafka" should fuzzy-match
    result3 = match("proficiency with apache kafka", conn, fuzzy_cutoff=0.50)
    assert result3.decision == "reuse"
    assert result3.tier == "fuzzy"
    assert result3.matched_hash == "hash_kafka_001"

    # Below cutoff: regenerate
    result4 = match("entirely different requirement", conn, fuzzy_cutoff=0.85)
    assert result4.decision == "regenerate"
    assert result4.matched_hash is None

    conn.close()


def test_match_result_is_dataclass():
    """MatchResult has the documented fields (for slice 4 + 5 compatibility)."""
    from jobsmith.reuse.match import MatchResult

    r = MatchResult(
        decision="reuse",
        tier="exact_tag",
        matched_hash="abc123",
        canonical_tag="tag:sql",
        similarity=1.0,
    )
    assert r.decision == "reuse"
    assert r.tier == "exact_tag"
    assert r.matched_hash == "abc123"
    assert r.canonical_tag == "tag:sql"
    assert r.similarity == 1.0


# ---------------------------------------------------------------------------
# integration: jd-parser canonical fields round-trip via db_ingest
# ---------------------------------------------------------------------------


def test_jd_parser_emits_canonical_fields_via_ingest(tmp_path: Path):
    """Feed a parsed-JD payload with canonical fields through db_ingest and assert
    canonical_requirements rows round-trip correctly.

    This simulates the post-phase ingest path:
      1. jd-parser writes jd-parsed.json with canonical fields per requirement
      2. ingest_canonical_requirements reads those fields
      3. canonical_requirements table is populated
    """
    from jobsmith.db_ingest import ingest_canonical_requirements

    db_path = tmp_path / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)

    # Simulate jd-parsed output with canonical fields
    jd_parsed = {
        "company": "Acme Corp",
        "position": "Senior Data Engineer",
        "must_haves": [
            {
                "raw": "advanced sql",
                "canonical_tag": "tag:sql",
                "normalized_phrase": "advanced sql",
            },
            {
                "raw": "experience with apache spark",
                "canonical_tag": "tag:spark",
                "normalized_phrase": "experience with apache spark",
            },
        ],
        "nice_to_haves": [
            {
                "raw": "knowledge of kubernetes",
                "canonical_tag": None,
                "normalized_phrase": "knowledge of kubernetes",
            }
        ],
    }

    n = ingest_canonical_requirements(conn, jd_parsed=jd_parsed)
    assert n == 3  # all three requirement entries ingested

    # Verify rows are in canonical_requirements
    rows = conn.execute("SELECT * FROM canonical_requirements").fetchall()
    assert len(rows) == 3

    # Verify payload structure
    payloads = [json.loads(r["payload"]) for r in rows]
    tags = {p.get("canonical_tag") for p in payloads}
    assert "tag:sql" in tags
    assert "tag:spark" in tags
    assert None in tags  # the kubernetes one has no tag

    # Idempotent: running again produces no new rows
    n2 = ingest_canonical_requirements(conn, jd_parsed=jd_parsed)
    assert n2 == 0

    rows2 = conn.execute("SELECT * FROM canonical_requirements").fetchall()
    assert len(rows2) == 3

    conn.close()
