"""Tests for jobsmith.reuse.dedup — JD near-duplicate detection (feat-42d39d4a).

TDD: failing tests written BEFORE implementation.

Covers:
  - fingerprint write + exact-match dedup
  - near-duplicate (fuzzy >= threshold) dedup hit
  - below-threshold => regenerate (no reuse)
  - distinct JDs => no false dedup
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from jobsmith import db as jobsmith_db
from jobsmith.config import ReuseSettings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

JD_TEXT_BASE = (
    "Senior Python Engineer — build scalable ETL pipelines, mentor junior engineers, "
    "collaborate with data scientists on ML feature engineering. "
    "5+ years Python, SQL, Apache Spark, AWS/GCP."
)
JD_TEXT_NEAR_DUP = (
    "Senior Python Engineer — build and maintain scalable ETL pipelines, mentor junior engineers, "
    "collaborate with data scientists on ML feature engineering. "
    "5+ years Python, SQL, Apache Spark, cloud platforms AWS/GCP."
)
JD_TEXT_DISTINCT = (
    "Senior Marketing Manager — lead brand strategy, develop GTM plans, manage agency "
    "relationships, drive demand generation. MBA preferred, 10+ years B2B SaaS marketing."
)
JD_PARSED_BASE = {"must_haves": [{"raw": "Python"}], "nice_to_haves": []}
FIT_SCORE_BASE = {"overall": 0.87, "tier": "strong"}


def _db(tmp_path: Path) -> sqlite3.Connection:
    return jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")


def _state_dir(base: Path, slug: str, jd_parsed: dict, fit_score: dict) -> Path:
    sd = base / slug / ".apply-state"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "jd-parsed.json").write_text(json.dumps(jd_parsed))
    (sd / "fit-score.json").write_text(json.dumps(fit_score))
    return sd


# ---------------------------------------------------------------------------
# fingerprint write
# ---------------------------------------------------------------------------


def test_write_jd_fingerprint(tmp_path: Path):
    from jobsmith.reuse.dedup import write_jd_fingerprint
    conn = _db(tmp_path)
    write_jd_fingerprint(conn, slug="acme-2026-06", jd_text=JD_TEXT_BASE)
    row = conn.execute(
        "SELECT slug, content_hash FROM application_fingerprints WHERE slug = ?",
        ("acme-2026-06",),
    ).fetchone()
    assert row is not None
    assert len(row[1]) == 64  # SHA-256 hex


def test_write_jd_fingerprint_idempotent(tmp_path: Path):
    from jobsmith.reuse.dedup import write_jd_fingerprint
    conn = _db(tmp_path)
    write_jd_fingerprint(conn, slug="acme-2026-06", jd_text=JD_TEXT_BASE)
    write_jd_fingerprint(conn, slug="acme-2026-06", jd_text=JD_TEXT_BASE)
    count = conn.execute(
        "SELECT COUNT(*) FROM application_fingerprints WHERE slug = ?",
        ("acme-2026-06",),
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# exact-match dedup
# ---------------------------------------------------------------------------


def test_exact_match_dedup(tmp_path: Path):
    from jobsmith.reuse.dedup import find_duplicate_jd, write_jd_fingerprint
    conn = _db(tmp_path)
    write_jd_fingerprint(conn, slug="acme-2026-05", jd_text=JD_TEXT_BASE)
    result = find_duplicate_jd(conn, jd_text=JD_TEXT_BASE, current_slug="acme-2026-06", cfg=ReuseSettings())
    assert result is not None
    assert result.matched_slug == "acme-2026-05"
    assert result.similarity == 1.0
    assert result.decision == "reuse"


def test_exact_match_excludes_self(tmp_path: Path):
    from jobsmith.reuse.dedup import find_duplicate_jd, write_jd_fingerprint
    conn = _db(tmp_path)
    write_jd_fingerprint(conn, slug="acme-2026-06", jd_text=JD_TEXT_BASE)
    result = find_duplicate_jd(conn, jd_text=JD_TEXT_BASE, current_slug="acme-2026-06", cfg=ReuseSettings())
    assert result is None or (result.matched_slug != "acme-2026-06")


# ---------------------------------------------------------------------------
# near-duplicate fuzzy dedup
# ---------------------------------------------------------------------------


def test_near_duplicate_dedup_hit(tmp_path: Path):
    from jobsmith.reuse.dedup import find_duplicate_jd, write_jd_fingerprint
    conn = _db(tmp_path)
    write_jd_fingerprint(conn, slug="acme-2026-05", jd_text=JD_TEXT_BASE)
    cfg = ReuseSettings(dedup_threshold=0.70)
    result = find_duplicate_jd(conn, jd_text=JD_TEXT_NEAR_DUP, current_slug="acme-2026-06", cfg=cfg)
    assert result is not None
    assert result.matched_slug == "acme-2026-05"
    assert result.decision == "reuse"
    assert 0.0 < result.similarity <= 1.0


def test_below_threshold_regenerates(tmp_path: Path):
    from jobsmith.reuse.dedup import find_duplicate_jd, write_jd_fingerprint
    conn = _db(tmp_path)
    write_jd_fingerprint(conn, slug="acme-2026-05", jd_text=JD_TEXT_BASE)
    cfg = ReuseSettings(dedup_threshold=0.999)
    result = find_duplicate_jd(conn, jd_text=JD_TEXT_NEAR_DUP, current_slug="acme-2026-06", cfg=cfg)
    assert result is None or result.decision == "regenerate"


# ---------------------------------------------------------------------------
# distinct JDs — no false dedup
# ---------------------------------------------------------------------------


def test_distinct_jd_no_false_dedup(tmp_path: Path):
    from jobsmith.reuse.dedup import find_duplicate_jd, write_jd_fingerprint
    conn = _db(tmp_path)
    write_jd_fingerprint(conn, slug="acme-2026-05", jd_text=JD_TEXT_BASE)
    result = find_duplicate_jd(
        conn, jd_text=JD_TEXT_DISTINCT, current_slug="widgetco-2026-06", cfg=ReuseSettings()
    )
    assert result is None or result.decision == "regenerate"


def test_empty_store_returns_none(tmp_path: Path):
    from jobsmith.reuse.dedup import find_duplicate_jd
    conn = _db(tmp_path)
    result = find_duplicate_jd(conn, jd_text=JD_TEXT_BASE, current_slug="acme-2026-06", cfg=ReuseSettings())
    assert result is None


# ---------------------------------------------------------------------------
# DedupResult dataclass
# ---------------------------------------------------------------------------


def test_dedup_result_fields():
    from jobsmith.reuse.dedup import DedupResult
    r = DedupResult(decision="reuse", matched_slug="acme-2026-05", similarity=0.95)
    assert r.decision == "reuse"
    assert r.matched_slug == "acme-2026-05"
    assert r.similarity == 0.95


def test_dedup_result_regenerate():
    from jobsmith.reuse.dedup import DedupResult
    r = DedupResult(decision="regenerate", matched_slug=None, similarity=0.0)
    assert r.decision == "regenerate"
    assert r.matched_slug is None


# ---------------------------------------------------------------------------
# load_prior_artifacts
# ---------------------------------------------------------------------------


def test_load_prior_artifacts_returns_both(tmp_path: Path):
    from jobsmith.reuse.dedup import load_prior_artifacts
    sd = _state_dir(tmp_path, "acme-2026-05", JD_PARSED_BASE, FIT_SCORE_BASE)
    jd_parsed, fit_score = load_prior_artifacts(state_dir=sd)
    assert jd_parsed == JD_PARSED_BASE
    assert fit_score == FIT_SCORE_BASE


def test_load_prior_artifacts_missing_files(tmp_path: Path):
    from jobsmith.reuse.dedup import load_prior_artifacts
    sd = tmp_path / "empty-app" / ".apply-state"
    sd.mkdir(parents=True, exist_ok=True)
    jd_parsed, fit_score = load_prior_artifacts(state_dir=sd)
    assert jd_parsed == {}
    assert fit_score == {}


# ---------------------------------------------------------------------------
# best match wins
# ---------------------------------------------------------------------------


def test_best_match_wins(tmp_path: Path):
    from jobsmith.reuse.dedup import find_duplicate_jd, write_jd_fingerprint
    conn = _db(tmp_path)
    write_jd_fingerprint(conn, slug="acme-close-2026-05", jd_text=JD_TEXT_NEAR_DUP)
    write_jd_fingerprint(conn, slug="widgetco-marketing-2026-05", jd_text=JD_TEXT_DISTINCT)
    result = find_duplicate_jd(
        conn, jd_text=JD_TEXT_BASE, current_slug="acme-2026-06", cfg=ReuseSettings(dedup_threshold=0.50)
    )
    assert result is not None
    assert result.matched_slug == "acme-close-2026-05"
