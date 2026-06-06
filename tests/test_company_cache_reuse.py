"""Tests for jobsmith.reuse.company_cache — company-key normalization and cross-role reuse.

TDD: these tests are written BEFORE the implementation.

Required by feat-62ac0b17:
  - test_company_key_normalization
  - test_ttl_expiry_forces_regen
  - test_second_role_same_company_skips_llm
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobsmith.reuse.company_cache import (
    normalize_company_key,
    check_cache,
    write_cache,
    record_company_research_metric,
)


# ---------------------------------------------------------------------------
# test_company_key_normalization
# ---------------------------------------------------------------------------


def test_company_key_normalization_basic() -> None:
    """Simple two-word names all produce the same slug."""
    assert normalize_company_key("Acme Corp") == normalize_company_key("acme corp")
    assert normalize_company_key("Acme Corp") == normalize_company_key("ACME CORP")


def test_company_key_normalization_legal_suffixes() -> None:
    """Legal suffixes (Inc, LLC, Ltd, Corp) are stripped before comparison."""
    base = normalize_company_key("Acme")
    assert normalize_company_key("Acme, Inc.") == base
    assert normalize_company_key("Acme Inc") == base
    assert normalize_company_key("Acme Inc.") == base
    assert normalize_company_key("Acme LLC") == base
    assert normalize_company_key("Acme Ltd") == base
    assert normalize_company_key("Acme Ltd.") == base
    assert normalize_company_key("Acme Corp") == base
    assert normalize_company_key("Acme Corp.") == base


def test_company_key_normalization_case_and_whitespace() -> None:
    """Different casing and whitespace produce the same key."""
    assert normalize_company_key("ACME") == normalize_company_key("acme")
    assert normalize_company_key("  Acme  ") == normalize_company_key("Acme")


def test_company_key_normalization_punctuation() -> None:
    """Punctuation differences don't produce different keys."""
    assert normalize_company_key("Smith & Wesson") == normalize_company_key("Smith and Wesson")


def test_company_key_normalization_returns_slug() -> None:
    """The returned key is a valid slug (lowercase, hyphens, no spaces)."""
    key = normalize_company_key("Acme Corp, Inc.")
    assert " " not in key
    assert key == key.lower()


def test_company_key_normalization_the_prefix() -> None:
    """Leading 'the' is stripped."""
    assert normalize_company_key("The Widget Company") == normalize_company_key("Widget Company")


# ---------------------------------------------------------------------------
# test_ttl_expiry_forces_regen
# ---------------------------------------------------------------------------


def test_ttl_expiry_forces_regen(tmp_path: Path) -> None:
    """A cache file older than TTL days causes check_cache to return None (miss)."""
    company_name = "Acme Inc"
    slug = normalize_company_key(company_name)
    cache_file = tmp_path / "companies" / f"{slug}.md"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("# Company Research\nOld content.")

    # Backdate mtime by 45 days (beyond default 30-day TTL)
    forty_five_days_ago = time.time() - 45 * 86400
    os.utime(cache_file, (forty_five_days_ago, forty_five_days_ago))

    result = check_cache(company_name, companies_dir=tmp_path / "companies", ttl_days=30)
    assert result is None, "Stale cache should produce a miss"


def test_ttl_within_window_returns_content(tmp_path: Path) -> None:
    """A fresh cache file within TTL is returned verbatim."""
    company_name = "Acme Inc"
    slug = normalize_company_key(company_name)
    cache_file = tmp_path / "companies" / f"{slug}.md"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    expected = "# Company Research\nFresh content."
    cache_file.write_text(expected)
    # mtime is "now" by default — clearly within TTL

    result = check_cache(company_name, companies_dir=tmp_path / "companies", ttl_days=30)
    assert result == expected


# ---------------------------------------------------------------------------
# test_second_role_same_company_skips_llm
# ---------------------------------------------------------------------------


def test_second_role_same_company_skips_llm(tmp_path: Path) -> None:
    """After a first application writes the cache, a second role at the same company
    hits the cache and no LLM (WebFetch/synthesis) call is needed.

    The test simulates:
      1. First role: cache miss → write_cache writes the file.
      2. Second role (different slug, same normalized company): check_cache returns hit.
    """
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()

    company_variants = [
        "Schneider Electric",
        "Schneider Electric Inc.",
        "SCHNEIDER ELECTRIC",
    ]
    research_content = "# Company Research\nSchneider content."

    # Role 1: write cache
    write_cache("Schneider Electric", research_content, companies_dir=companies_dir)

    # Role 2 (different spelling) should hit the same normalized key
    for variant in company_variants:
        result = check_cache(variant, companies_dir=companies_dir, ttl_days=30)
        assert result == research_content, (
            f"Expected cache hit for variant '{variant}', got None"
        )


def test_second_role_different_company_no_hit(tmp_path: Path) -> None:
    """A different company does NOT get a spurious cache hit."""
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()

    write_cache("Acme Corp", "# Acme content", companies_dir=companies_dir)

    result = check_cache("Widget Co", companies_dir=companies_dir, ttl_days=30)
    assert result is None


# ---------------------------------------------------------------------------
# record_company_research_metric
# ---------------------------------------------------------------------------


def test_record_company_research_metric_reused(tmp_path: Path) -> None:
    """record_company_research_metric writes a 'reused' value to run_metrics."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE run_metrics "
        "(slug TEXT, metric_key TEXT, metric_value TEXT, created_at TEXT, "
        "PRIMARY KEY (slug, metric_key))"
    )
    conn.commit()

    record_company_research_metric(conn, slug="acme-swe-2024", outcome="reused")

    row = conn.execute(
        "SELECT metric_value FROM run_metrics "
        "WHERE slug = 'acme-swe-2024' AND metric_key = 'company_research_source'"
    ).fetchone()
    assert row is not None
    assert row[0] == "reused"


def test_record_company_research_metric_generated(tmp_path: Path) -> None:
    """record_company_research_metric writes a 'generated' value to run_metrics."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE run_metrics "
        "(slug TEXT, metric_key TEXT, metric_value TEXT, created_at TEXT, "
        "PRIMARY KEY (slug, metric_key))"
    )
    conn.commit()

    record_company_research_metric(conn, slug="acme-pm-2024", outcome="generated")

    row = conn.execute(
        "SELECT metric_value FROM run_metrics "
        "WHERE slug = 'acme-pm-2024' AND metric_key = 'company_research_source'"
    ).fetchone()
    assert row is not None
    assert row[0] == "generated"
