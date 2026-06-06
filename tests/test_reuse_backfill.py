"""Tests for jobsmith.reuse.backfill — idempotent backfill of reuse tables.

TDD Protocol: these tests are written BEFORE the implementation.
They cover:
  - backfill_slug_reuse() populates all 3 reuse stores from a fixture application
  - Running twice produces the same row counts (idempotency)
  - backfill_all_reuse() iterates all slugs under applications_dir
  - Missing artifacts are silently skipped (best-effort)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobsmith import db as jobsmith_db

# ---------------------------------------------------------------------------
# Helpers — build fixture application directories
# ---------------------------------------------------------------------------


def _make_jd_parsed(
    company: str = "Acme Corp",
    must_haves: list | None = None,
    jd_text: str = "We are looking for a Python engineer.",
) -> dict:
    must_haves = must_haves or [
        {
            "raw": "Python 3",
            "canonical_tag": "tag:python",
            "normalized_phrase": "python 3",
        }
    ]
    return {
        "company": company,
        "position": "Software Engineer",
        "jd_text_clean": jd_text,
        "must_haves": must_haves,
        "nice_to_haves": [],
    }


def _make_bullet_selection(req_hash: str) -> dict:
    return {
        "positions": [
            {
                "company": "Acme Corp",
                "title": "Staff Engineer",
                "bullets": [
                    {
                        "master_bullet_id": "abc123def456",
                        "text": "Built Python data pipeline",
                        "included": True,
                        "matched_requirement_hash": req_hash,
                    },
                ],
            }
        ]
    }


def _make_app_dir(
    applications_dir: Path,
    slug: str,
    *,
    jd_parsed: dict | None = None,
    bullet_selection: dict | None = None,
) -> Path:
    """Create a minimal applications/<slug>/.apply-state/ structure."""
    state_dir = applications_dir / slug / ".apply-state"
    state_dir.mkdir(parents=True)

    if jd_parsed is not None:
        (state_dir / "jd-parsed.json").write_text(
            json.dumps(jd_parsed), encoding="utf-8"
        )

    if bullet_selection is not None:
        (state_dir / "bullet-selection.json").write_text(
            json.dumps(bullet_selection), encoding="utf-8"
        )

    return state_dir


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(tmp_path: Path):
    db_path = tmp_path / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def applications_dir(tmp_path: Path) -> Path:
    d = tmp_path / "applications"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backfill_slug_reuse_populates_fingerprints(db_conn, applications_dir):
    """backfill_slug_reuse writes at least one row to application_fingerprints."""
    from jobsmith.reuse.backfill import backfill_slug_reuse

    jd = _make_jd_parsed()
    _make_app_dir(applications_dir, "acme-swe-2025-01", jd_parsed=jd)

    backfill_slug_reuse(db_conn, "acme-swe-2025-01", applications_dir)

    assert _row_count(db_conn, "application_fingerprints") >= 1


def test_backfill_slug_reuse_populates_canonical_requirements(db_conn, applications_dir):
    """backfill_slug_reuse writes canonical requirements from jd-parsed.json."""
    from jobsmith.reuse.backfill import backfill_slug_reuse

    must_haves = [
        {
            "raw": "5+ years Python",
            "canonical_tag": "tag:python",
            "normalized_phrase": "python 5 plus years",
        },
        {
            "raw": "AWS experience",
            "canonical_tag": "tag:aws",
            "normalized_phrase": "aws experience",
        },
    ]
    jd = _make_jd_parsed(must_haves=must_haves)
    _make_app_dir(applications_dir, "acme-swe-2025-02", jd_parsed=jd)

    backfill_slug_reuse(db_conn, "acme-swe-2025-02", applications_dir)

    assert _row_count(db_conn, "canonical_requirements") == 2


def test_backfill_slug_reuse_populates_evidence_map(db_conn, applications_dir):
    """backfill_slug_reuse writes requirement_evidence_map from bullet-selection.json."""
    from jobsmith.reuse.backfill import backfill_slug_reuse
    from jobsmith.reuse.canonicalize import canonicalize, requirement_content_hash

    tag, normalized = canonicalize("Python 3")
    req_hash = requirement_content_hash({"canonical_tag": tag, "normalized_phrase": normalized})

    jd = _make_jd_parsed()
    sel = _make_bullet_selection(req_hash)
    _make_app_dir(
        applications_dir, "acme-swe-2025-03", jd_parsed=jd, bullet_selection=sel
    )

    backfill_slug_reuse(db_conn, "acme-swe-2025-03", applications_dir)

    assert _row_count(db_conn, "requirement_evidence_map") >= 1


def test_backfill_slug_reuse_is_idempotent(db_conn, applications_dir):
    """Running backfill_slug_reuse twice yields the same row counts."""
    from jobsmith.reuse.backfill import backfill_slug_reuse
    from jobsmith.reuse.canonicalize import canonicalize, requirement_content_hash

    tag, normalized = canonicalize("Python 3")
    req_hash = requirement_content_hash({"canonical_tag": tag, "normalized_phrase": normalized})

    jd = _make_jd_parsed()
    sel = _make_bullet_selection(req_hash)
    _make_app_dir(
        applications_dir, "acme-swe-2025-04", jd_parsed=jd, bullet_selection=sel
    )

    slug = "acme-swe-2025-04"
    backfill_slug_reuse(db_conn, slug, applications_dir)
    counts_1 = {
        "fingerprints": _row_count(db_conn, "application_fingerprints"),
        "canonical": _row_count(db_conn, "canonical_requirements"),
        "evidence": _row_count(db_conn, "requirement_evidence_map"),
    }

    # Second run — must be a no-op
    backfill_slug_reuse(db_conn, slug, applications_dir)
    counts_2 = {
        "fingerprints": _row_count(db_conn, "application_fingerprints"),
        "canonical": _row_count(db_conn, "canonical_requirements"),
        "evidence": _row_count(db_conn, "requirement_evidence_map"),
    }

    assert counts_1 == counts_2


def test_backfill_slug_reuse_missing_artifacts_is_noop(db_conn, applications_dir):
    """Slug with no .apply-state dir returns 0 and does not raise."""
    from jobsmith.reuse.backfill import backfill_slug_reuse

    result = backfill_slug_reuse(db_conn, "nonexistent-slug", applications_dir)

    assert result == 0
    assert _row_count(db_conn, "application_fingerprints") == 0


def test_backfill_slug_reuse_partial_artifacts(db_conn, applications_dir):
    """Slug with jd-parsed but no bullet-selection still populates fingerprints+canonical."""
    from jobsmith.reuse.backfill import backfill_slug_reuse

    jd = _make_jd_parsed(
        must_haves=[{"raw": "Go", "canonical_tag": "tag:go", "normalized_phrase": "go"}]
    )
    _make_app_dir(applications_dir, "partial-slug-2025", jd_parsed=jd)

    backfill_slug_reuse(db_conn, "partial-slug-2025", applications_dir)

    assert _row_count(db_conn, "application_fingerprints") >= 1
    assert _row_count(db_conn, "canonical_requirements") >= 1
    # No bullet-selection → evidence map stays empty
    assert _row_count(db_conn, "requirement_evidence_map") == 0


def test_backfill_all_reuse_iterates_all_slugs(db_conn, applications_dir):
    """backfill_all_reuse processes all slugs and returns a dict of results."""
    from jobsmith.reuse.backfill import backfill_all_reuse

    for slug in ("acme-eng-2025", "beta-dev-2025"):
        _make_app_dir(
            applications_dir,
            slug,
            jd_parsed=_make_jd_parsed(company=slug),
        )

    results = backfill_all_reuse(db_conn, applications_dir)

    assert set(results.keys()) == {"acme-eng-2025", "beta-dev-2025"}
    assert _row_count(db_conn, "application_fingerprints") == 2


def test_backfill_all_reuse_skips_hidden_dirs(db_conn, applications_dir):
    """Directories starting with '.' or '_' are not backfilled."""
    from jobsmith.reuse.backfill import backfill_all_reuse

    _make_app_dir(applications_dir, ".hidden-slug", jd_parsed=_make_jd_parsed())
    _make_app_dir(applications_dir, "_template-slug", jd_parsed=_make_jd_parsed())
    _make_app_dir(
        applications_dir, "real-slug-2025", jd_parsed=_make_jd_parsed()
    )

    results = backfill_all_reuse(db_conn, applications_dir)

    assert list(results.keys()) == ["real-slug-2025"]
