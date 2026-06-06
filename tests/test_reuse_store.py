"""Tests for jobsmith.reuse.store and migration 009.

TDD: these tests were written BEFORE implementation.

Tests:
  - test_content_hash_stable_and_sensitive
  - test_is_fresh_hash_and_ttl
  - test_config_defaults_present
  - test_migration_009_idempotent
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobsmith import db as jobsmith_db
from jobsmith.config import ReuseSettings
from jobsmith.reuse.store import content_hash, is_fresh

# ---------------------------------------------------------------------------
# content_hash — stable and sensitive
# ---------------------------------------------------------------------------


def test_content_hash_stable_and_sensitive():
    """Hash is identical for cosmetic whitespace/case changes but differs on real changes."""
    base = {"jd": "Senior Engineer role", "skills": ["python", "sql"]}

    # Cosmetic: extra trailing space in value
    cosmetic_space = {"jd": "Senior Engineer role  ", "skills": ["python", "sql"]}
    # Cosmetic: leading whitespace in value
    cosmetic_leading = {"jd": "  Senior Engineer role", "skills": ["python", "sql"]}
    # Cosmetic: different case
    cosmetic_case = {"jd": "SENIOR ENGINEER ROLE", "skills": ["PYTHON", "SQL"]}

    # Same hash for cosmetic changes
    assert content_hash(base) == content_hash(cosmetic_space)
    assert content_hash(base) == content_hash(cosmetic_leading)
    assert content_hash(base) == content_hash(cosmetic_case)

    # Real change: different content
    changed = {"jd": "Junior Engineer role", "skills": ["python", "sql"]}
    assert content_hash(base) != content_hash(changed)

    # Real change: extra key
    extra_key = {"jd": "Senior Engineer role", "skills": ["python", "sql"], "extra": "data"}
    assert content_hash(base) != content_hash(extra_key)

    # Stability: same inputs always yield the same hash
    assert content_hash(base) == content_hash(base)
    assert content_hash(base) == content_hash(dict(base))


# ---------------------------------------------------------------------------
# is_fresh — hash and TTL checks
# ---------------------------------------------------------------------------


def test_is_fresh_hash_and_ttl():
    """is_fresh returns False when hash differs or row is older than TTL."""
    now = datetime.now(tz=timezone.utc)
    recent_ts = now.isoformat()
    old_ts = (now - timedelta(days=40)).isoformat()

    ttl_30d = timedelta(days=30)

    # Fresh: same hash, recent timestamp
    assert is_fresh({"content_hash": "abc123", "created_at": recent_ts}, "abc123", ttl_30d) is True

    # Stale: different hash (real content change)
    assert is_fresh({"content_hash": "abc123", "created_at": recent_ts}, "different_hash", ttl_30d) is False

    # Stale: same hash but older than TTL
    assert is_fresh({"content_hash": "abc123", "created_at": old_ts}, "abc123", ttl_30d) is False

    # Stale: different hash AND old
    assert is_fresh({"content_hash": "abc123", "created_at": old_ts}, "different_hash", ttl_30d) is False


# ---------------------------------------------------------------------------
# config defaults — all reuse knobs present
# ---------------------------------------------------------------------------


def test_config_defaults_present():
    """ReuseSettings has all documented knobs with correct defaults."""
    cfg = ReuseSettings()

    # Fuzzy match cutoff (0-100, Levenshtein/ratio threshold)
    assert isinstance(cfg.fuzzy_cutoff, float)
    assert 0.0 <= cfg.fuzzy_cutoff <= 1.0

    # JD overlap threshold for warm-start reuse
    assert isinstance(cfg.jd_overlap_warm_start_threshold, float)
    assert 0.0 <= cfg.jd_overlap_warm_start_threshold <= 1.0

    # Dedup threshold for near-duplicate JD detection
    assert isinstance(cfg.dedup_threshold, float)
    assert 0.0 <= cfg.dedup_threshold <= 1.0

    # Company research TTL (days)
    assert isinstance(cfg.company_ttl_days, int)
    assert cfg.company_ttl_days == 30

    # Retry/regen limit before giving up on reuse
    assert isinstance(cfg.regen_retry_bound, int)
    assert cfg.regen_retry_bound >= 1

    # Verify defaults are sensible (not zero)
    assert cfg.fuzzy_cutoff > 0.0
    assert cfg.jd_overlap_warm_start_threshold > 0.0
    assert cfg.dedup_threshold > 0.0


# ---------------------------------------------------------------------------
# migration 009 — idempotent double-run
# ---------------------------------------------------------------------------


def test_migration_009_idempotent(tmp_path: Path):
    """Running _PIPELINE_MIGRATIONS twice creates tables exactly once, no error."""
    db_path = tmp_path / "jobsmith.db"

    # First run: creates schema
    conn1 = jobsmith_db.open_pipeline_db(db_path)

    # Verify all four reuse tables exist
    expected_tables = {
        "canonical_requirements",
        "requirement_evidence_map",
        "application_fingerprints",
        "run_metrics",
    }
    rows = conn1.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    actual_tables = {row[0] for row in rows}
    for table in expected_tables:
        assert table in actual_tables, f"Table {table!r} not created by migration 009"

    conn1.close()

    # Second run: must be a no-op (idempotent)
    conn2 = jobsmith_db.open_pipeline_db(db_path)
    rows2 = conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    actual_tables2 = {row[0] for row in rows2}
    for table in expected_tables:
        assert table in actual_tables2

    # schema_migrations should record 009 exactly once
    count = conn2.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = '009_reuse_store'"
    ).fetchone()[0]
    assert count == 1

    conn2.close()


# ---------------------------------------------------------------------------
# store read/write helpers — basic round-trip
# ---------------------------------------------------------------------------


def test_store_roundtrip_canonical_requirements(tmp_path: Path):
    """Write and read back a canonical_requirements row."""
    from jobsmith.reuse.store import (
        get_canonical_requirement,
        upsert_canonical_requirement,
    )

    db_path = tmp_path / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)

    inputs = {"jd_text": "Senior Python Engineer at Acme"}
    h = content_hash(inputs)
    upsert_canonical_requirement(conn, content_hash=h, payload="parsed_output_here")

    row = get_canonical_requirement(conn, content_hash=h)
    assert row is not None
    assert row["content_hash"] == h
    assert row["payload"] == "parsed_output_here"

    # Second upsert replaces, no error
    upsert_canonical_requirement(conn, content_hash=h, payload="updated_output")
    row2 = get_canonical_requirement(conn, content_hash=h)
    assert row2["payload"] == "updated_output"

    conn.close()


def test_store_roundtrip_application_fingerprints(tmp_path: Path):
    """Write and read back an application_fingerprints row."""
    from jobsmith.reuse.store import (
        get_application_fingerprint,
        upsert_application_fingerprint,
    )

    db_path = tmp_path / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)

    slug = "acme-senior-engineer-2026-06"
    h = "deadbeef1234"
    upsert_application_fingerprint(conn, slug=slug, content_hash=h)

    row = get_application_fingerprint(conn, slug=slug)
    assert row is not None
    assert row["slug"] == slug
    assert row["content_hash"] == h

    conn.close()


def test_store_roundtrip_run_metrics(tmp_path: Path):
    """Write and read back a run_metrics row."""
    from jobsmith.reuse.store import get_run_metrics, upsert_run_metric

    db_path = tmp_path / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)

    slug = "acme-senior-engineer-2026-06"
    upsert_run_metric(conn, slug=slug, metric_key="cache_hit_rate", metric_value="0.75")

    rows = get_run_metrics(conn, slug=slug)
    assert len(rows) == 1
    assert rows[0]["metric_key"] == "cache_hit_rate"
    assert rows[0]["metric_value"] == "0.75"

    conn.close()
