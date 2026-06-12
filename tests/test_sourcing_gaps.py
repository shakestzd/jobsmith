"""Tests for jobsmith.sourcing.gaps (feat-d20ff292).

TDD: tests written before implementation.

Covers:
  - extract_gap_terms: real gap strings produce expected terms, stoplist drops generic words
  - harvest_known_gaps: both halt envelope shapes (Arcadia with
    additional_uncovered_must_haves, GitLab without) are parsed
  - harvest_known_gaps: gap whose terms appear in master digest is dropped (fixed-gap expiry)
  - match_posting: GitLab JD hits AI/LLM terms; Arcadia JD hits DBT/healthcare; tax-equity misses
  - match_posting: 'claims' multi-word guard case (no false positive on short generic matches)
  - run_crawl integration: gap_hits_json written for ALL postings with JD text (including OLD ones)
  - run_crawl integration: empty-JD postings get NULL (not a false-negative clean result)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from jobsmith.db import open_pipeline_db
from jobsmith.sourcing.gaps import (
    extract_gap_terms,
    harvest_known_gaps,
    match_posting,
)

# ---------------------------------------------------------------------------
# Fixtures: real halt envelope shapes
# ---------------------------------------------------------------------------

# GitLab Senior AI Engineer — halt without additional_uncovered_must_haves
# (gaps only in summary string — must_have is the source of gaps here)
GITLAB_HALT_ENVELOPE = {
    "status": "halt",
    "reason": "uncovered_must_have",
    "must_have": [
        "Direct experience building and deploying LLM-powered products or AI agents in production",
        "Proficiency with Python and modern LLM frameworks (LangChain, LangGraph, or similar)",
        "Strong understanding of RAG, vector databases, and AI/LLM evaluation patterns",
    ],
    # Note: no additional_uncovered_must_haves key
}

# Arcadia Lead Analytics Engineer — halt WITH additional_uncovered_must_haves
ARCADIA_HALT_ENVELOPE = {
    "status": "halt",
    "reason": "uncovered_must_have",
    "must_have": [
        "5+ years of analytics engineering with dbt in production",
        "Healthcare claims data experience (HEDIS, STARS, or similar risk adjustment)",
        "Experience with EHR data integration",
    ],
    "additional_uncovered_must_haves": [
        "dbt Cloud or dbt Core in a large-scale warehouse environment",
        "healthcare claims processing pipeline",
    ],
}

# GitLab JD excerpt — should match AI/LLM gap terms
GITLAB_JD = (
    "We are looking for a Senior AI Engineer to lead development of LLM-powered features. "
    "You will work with LangChain, LangGraph, and vector databases for RAG pipelines. "
    "Experience with LLM evaluation, AI agents, and Python is required."
)

# Arcadia JD excerpt — should match DBT/healthcare gap terms
ARCADIA_JD = (
    "Lead Analytics Engineer to build dbt pipelines for healthcare claims data. "
    "HEDIS and STARS risk adjustment experience required. "
    "Proficiency with dbt Cloud and EHR data integration expected."
)

# Tax-equity posting — should NOT match GitLab or Arcadia gaps
TAX_EQUITY_JD = (
    "Senior Finance Analyst for tax equity structuring and ITC monetization. "
    "CPA preferred with strong Excel modeling and GAAP accounting knowledge."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal pipeline DB with schema applied."""
    path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(path)
    conn.close()
    return path


def _insert_apply_state(conn: sqlite3.Connection, slug: str, kind: str, blob: dict) -> None:
    """Insert a row into apply_state."""
    from jobsmith.db import put_state

    put_state(conn, slug=slug, kind=kind, content_blob=json.dumps(blob))


def _upsert_posting(conn: sqlite3.Connection, *, title: str, jd_text: str | None) -> int:
    """Insert a posting row and return its id."""
    import hashlib

    dedup_key = hashlib.sha256(title.encode()).hexdigest()
    conn.execute(
        """
        INSERT OR IGNORE INTO postings
            (source, external_id, url, title, company, location, jd_text,
             fast_score, status, dedup_key, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (
            "test/testco",
            f"test:{title}",
            f"https://example.com/{title}",
            title,
            "TestCo",
            "Remote",
            jd_text,
            0.5,
            "sourced",
            dedup_key,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM postings WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Tests: extract_gap_terms
# ---------------------------------------------------------------------------


class TestExtractGapTerms:
    def test_ai_llm_gap_produces_distinctive_terms(self):
        gap = "Direct experience building and deploying LLM-powered products or AI agents in production"
        terms = extract_gap_terms(gap)
        # Should extract multi-word or domain terms, not stopwords
        assert any("llm" in t for t in terms)

    def test_dbt_healthcare_gap_terms(self):
        gap = "5+ years of analytics engineering with dbt in production"
        terms = extract_gap_terms(gap)
        assert "dbt" in terms

    def test_healthcare_claims_gap_terms(self):
        gap = "Healthcare claims data experience (HEDIS, STARS, or similar risk adjustment)"
        terms = extract_gap_terms(gap)
        # Should get 'healthcare claims' or 'claims' but as multi-word or meaningful term
        # The plan says min length 3, stoplist drops generic words
        assert any("healthcare" in t or "claims" in t or "hedis" in t for t in terms)

    def test_stoplist_drops_generic_words(self):
        gap = "Data experience with modern tools"
        terms = extract_gap_terms(gap)
        # 'data' and 'experience' are generic stoplist words
        assert "data" not in terms
        assert "experience" not in terms

    def test_min_length_three(self):
        gap = "AI ML NLP experience with big data"
        terms = extract_gap_terms(gap)
        # Single-char and very-short tokens should not appear
        for t in terms:
            assert len(t) >= 3, f"Term too short: {repr(t)}"

    def test_returns_up_to_three_terms(self):
        gap = "Production LLM engineering with langchain and vector databases for RAG"
        terms = extract_gap_terms(gap)
        assert 1 <= len(terms) <= 3

    def test_returns_lowercase(self):
        gap = "DBT production engineering with LangChain"
        terms = extract_gap_terms(gap)
        for t in terms:
            assert t == t.lower(), f"Term not lowercase: {repr(t)}"


# ---------------------------------------------------------------------------
# Tests: harvest_known_gaps
# ---------------------------------------------------------------------------


class TestHarvestKnownGaps:
    def test_gitlab_envelope_shape_no_additional(self, tmp_path):
        """GitLab halt (no additional_uncovered_must_haves) — must_have gaps extracted."""
        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)
        _insert_apply_state(
            conn, "gitlab-senior-ai-engineer", "apply-fit-result", GITLAB_HALT_ENVELOPE
        )
        conn.commit()

        gaps = harvest_known_gaps(conn)
        conn.close()

        assert len(gaps) > 0
        gap_labels = [g["gap"] for g in gaps]
        # Should have picked up LLM-related gap
        assert any("llm" in label.lower() or "ai" in label.lower() for label in gap_labels)

    def test_arcadia_envelope_shape_with_additional(self, tmp_path):
        """Arcadia halt (with additional_uncovered_must_haves) — both fields extracted."""
        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)
        _insert_apply_state(
            conn, "arcadia-lead-analytics", "apply-fit-result", ARCADIA_HALT_ENVELOPE
        )
        conn.commit()

        gaps = harvest_known_gaps(conn)
        conn.close()

        assert len(gaps) > 0
        gap_labels = " ".join(g["gap"].lower() for g in gaps)
        assert "dbt" in gap_labels or "healthcare" in gap_labels

    def test_arcadia_additional_uncovered_also_harvested(self, tmp_path):
        """additional_uncovered_must_haves from Arcadia envelope are included."""
        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)
        _insert_apply_state(
            conn, "arcadia-lead-analytics", "apply-fit-result", ARCADIA_HALT_ENVELOPE
        )
        conn.commit()

        gaps = harvest_known_gaps(conn)
        conn.close()

        gap_labels = [g["gap"].lower() for g in gaps]
        # additional_uncovered_must_haves items should be included
        assert any("dbt cloud" in lbl or "healthcare claims" in lbl for lbl in gap_labels)

    def test_non_halt_rows_excluded(self, tmp_path):
        """apply_state rows with status != halt should not contribute gaps."""
        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)
        _insert_apply_state(
            conn,
            "some-slug",
            "apply-fit-result",
            {"status": "done", "must_have": ["some requirement"]},
        )
        conn.commit()

        gaps = harvest_known_gaps(conn)
        conn.close()

        assert gaps == []

    def test_fixed_gap_expiry_via_master_digest(self, tmp_path):
        """Gap whose terms appear in master digest is dropped at harvest time."""
        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)

        # Insert a halt with a DBT gap specifically
        dbt_halt = {
            "status": "halt",
            "reason": "uncovered_must_have",
            "must_have": [
                "5+ years of analytics engineering with dbt in production",
            ],
        }
        _insert_apply_state(conn, "test-slug", "apply-fit-result", dbt_halt)

        # Insert master_content that explicitly covers dbt
        conn.execute(
            "INSERT OR REPLACE INTO master_content (section, content_blob) VALUES (?, ?)",
            (
                "work",
                """
- title: "Analytics Engineer"
  location: "DataCo"
  date: "2022-Present"
  details:
    - "Built dbt pipelines processing 500M rows daily across Snowflake"
    - "Maintained dbt models with 99.9% test coverage"
""",
            ),
        )
        conn.commit()

        gaps = harvest_known_gaps(conn)
        conn.close()

        # The dbt gap terms are now covered by master content — should be dropped
        assert gaps == [], f"Expected no gaps after dbt added to master, got: {gaps}"

    def test_multiple_slugs_deduplicated(self, tmp_path):
        """Same gap text from two slugs appears only once."""
        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)
        same_envelope = {
            "status": "halt",
            "reason": "uncovered_must_have",
            "must_have": ["5+ years of analytics engineering with dbt in production"],
        }
        _insert_apply_state(conn, "slug-a", "apply-fit-result", same_envelope)
        _insert_apply_state(conn, "slug-b", "apply-fit-result", same_envelope)
        conn.commit()

        gaps = harvest_known_gaps(conn)
        conn.close()

        gap_labels = [g["gap"] for g in gaps]
        # Duplicate gap strings should not appear twice
        assert len(gap_labels) == len(set(gap_labels))

    def test_malformed_apply_state_json_skipped(self, tmp_path):
        """Malformed JSON in apply_state is silently skipped."""
        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO apply_state (slug, kind, content_blob, updated_at)"
            " VALUES (?, ?, ?, datetime('now'))",
            ("bad-slug", "apply-fit-result", "NOT VALID JSON {{{"),
        )
        conn.commit()

        gaps = harvest_known_gaps(conn)
        conn.close()

        assert gaps == []


# ---------------------------------------------------------------------------
# Tests: match_posting
# ---------------------------------------------------------------------------


class TestMatchPosting:
    def _gitlab_gaps(self) -> list[dict]:
        """Build gaps from GitLab halt envelope (no additional)."""
        gaps = []
        for item in GITLAB_HALT_ENVELOPE["must_have"]:
            terms = extract_gap_terms(item)
            if terms:
                gaps.append({"gap": item[:60], "terms": terms})
        return gaps

    def _arcadia_gaps(self) -> list[dict]:
        """Build gaps from Arcadia halt envelope (with additional)."""
        gaps = []
        all_items = ARCADIA_HALT_ENVELOPE["must_have"] + ARCADIA_HALT_ENVELOPE.get(
            "additional_uncovered_must_haves", []
        )
        for item in all_items:
            terms = extract_gap_terms(item)
            if terms:
                gaps.append({"gap": item[:60], "terms": terms})
        return gaps

    def test_gitlab_jd_hits_ai_llm_gaps(self):
        gaps = self._gitlab_gaps()
        hits = match_posting(GITLAB_JD, gaps)
        assert len(hits) > 0
        hit_terms = [h["term"] for h in hits]
        assert any("llm" in t for t in hit_terms)

    def test_arcadia_jd_hits_dbt_healthcare_gaps(self):
        gaps = self._arcadia_gaps()
        hits = match_posting(ARCADIA_JD, gaps)
        assert len(hits) > 0
        hit_terms = " ".join(h["term"] for h in hits)
        assert "dbt" in hit_terms or "healthcare" in hit_terms

    def test_tax_equity_jd_misses_all_gaps(self):
        """Tax-equity JD should not match GitLab or Arcadia gaps."""
        all_gaps = self._gitlab_gaps() + self._arcadia_gaps()
        hits = match_posting(TAX_EQUITY_JD, all_gaps)
        assert hits == []

    def test_hit_payload_shape(self):
        gaps = [{"gap": "dbt analytics", "terms": ["dbt"]}]
        hits = match_posting("We use dbt in production", gaps)
        assert len(hits) == 1
        assert "gap" in hits[0]
        assert "term" in hits[0]
        assert hits[0]["term"] == "dbt"

    def test_case_insensitive_match(self):
        gaps = [{"gap": "DBT production", "terms": ["dbt"]}]
        hits = match_posting("Experience with DBT Cloud required.", gaps)
        assert len(hits) == 1

    def test_empty_jd_returns_empty(self):
        gaps = [{"gap": "dbt experience", "terms": ["dbt"]}]
        hits = match_posting("", gaps)
        assert hits == []

    def test_empty_gaps_returns_empty(self):
        hits = match_posting("We use dbt in production", [])
        assert hits == []

    def test_no_duplicate_hits_for_same_gap(self):
        """A gap should appear at most once even if multiple terms match."""
        gaps = [{"gap": "dbt analytics", "terms": ["dbt", "analytics"]}]
        jd = "Use dbt for analytics pipelines in dbt Cloud"
        hits = match_posting(jd, gaps)
        gap_labels = [h["gap"] for h in hits]
        assert len(gap_labels) == len(set(gap_labels))


# ---------------------------------------------------------------------------
# Tests: run_crawl integration (gap_hits_json written to ALL JD-bearing postings)
# ---------------------------------------------------------------------------


class TestRunCrawlGapIntegration:
    """Verify gap_hits_json is written for all postings with JD text, including old ones."""

    def _make_sourcing_config(self, db_path: Path, roles_fn) -> dict:
        """Return a minimal run_crawl kwargs dict."""
        return {
            "db_path": db_path,
            "sources": [{"type": "fake", "slug": "testco"}],
            "adapter_factory": roles_fn,
            "dry_run": False,
            "no_llm": True,
        }

    def test_gap_hits_json_written_for_old_posting(self, tmp_path):
        """A posting that existed BEFORE the crawl gets gap_hits_json updated."""
        from jobsmith.sourcing.adapters.base import ATSSourceAdapter
        from jobsmith.sourcing.runner import run_crawl

        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)

        # Pre-insert a halt envelope with an LLM gap
        _insert_apply_state(
            conn, "gitlab-senior-ai-engineer", "apply-fit-result", GITLAB_HALT_ENVELOPE
        )

        # Pre-insert an OLD posting with JD text matching the LLM gap
        old_posting_id = _upsert_posting(
            conn,
            title="AI Engineer Old",
            jd_text=GITLAB_JD,
        )
        conn.close()

        # Now run crawl — it should NOT create any new postings (just re-sight nothing)
        # But it SHOULD still update gap_hits_json for old_posting_id
        class _EmptyAdapter(ATSSourceAdapter):
            name = "fake"

            def fetch(self, slug: str):
                return iter([])

        def _factory(spec):
            return _EmptyAdapter()

        run_crawl(**self._make_sourcing_config(db_path, _factory))

        conn2 = open_pipeline_db(db_path)
        row = conn2.execute(
            "SELECT gap_hits_json FROM postings WHERE id = ?", (old_posting_id,)
        ).fetchone()
        conn2.close()

        assert row is not None
        assert row[0] is not None, "gap_hits_json should be set for JD-bearing old posting"
        hits = json.loads(row[0])
        assert isinstance(hits, list)
        assert len(hits) > 0
        assert any("llm" in h["term"] for h in hits)

    def test_empty_jd_posting_gets_null_gap_hits(self, tmp_path):
        """Posting with no JD text gets NULL gap_hits_json (not empty list)."""
        from jobsmith.sourcing.adapters.base import ATSSourceAdapter
        from jobsmith.sourcing.runner import run_crawl

        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)

        _insert_apply_state(
            conn, "gitlab-senior-ai-engineer", "apply-fit-result", GITLAB_HALT_ENVELOPE
        )

        empty_jd_id = _upsert_posting(conn, title="Finance Role", jd_text=None)
        conn.close()

        class _EmptyAdapter(ATSSourceAdapter):
            name = "fake"

            def fetch(self, slug: str):
                return iter([])

        def _factory(spec):
            return _EmptyAdapter()

        run_crawl(**self._make_sourcing_config(db_path, _factory))

        conn2 = open_pipeline_db(db_path)
        row = conn2.execute(
            "SELECT gap_hits_json FROM postings WHERE id = ?", (empty_jd_id,)
        ).fetchone()
        conn2.close()

        assert row is not None
        # NULL is correct — no JD means we cannot evaluate gaps
        assert row[0] is None

    def test_no_gaps_yields_empty_json_array(self, tmp_path):
        """When there are no halt gaps, all JD-bearing postings get [] (empty array)."""
        from jobsmith.sourcing.adapters.base import ATSSourceAdapter
        from jobsmith.sourcing.runner import run_crawl

        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)

        # No halt envelopes — no gaps
        posting_id = _upsert_posting(conn, title="Tax Equity Analyst", jd_text=TAX_EQUITY_JD)
        conn.close()

        class _EmptyAdapter(ATSSourceAdapter):
            name = "fake"

            def fetch(self, slug: str):
                return iter([])

        def _factory(spec):
            return _EmptyAdapter()

        run_crawl(**self._make_sourcing_config(db_path, _factory))

        conn2 = open_pipeline_db(db_path)
        row = conn2.execute(
            "SELECT gap_hits_json FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
        conn2.close()

        assert row is not None
        assert row[0] is not None
        hits = json.loads(row[0])
        assert hits == []
