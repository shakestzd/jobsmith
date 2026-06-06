"""Tests for jobsmith.reuse.evidence_map — requirement→bullet mapping.

TDD: failing tests written BEFORE implementation.

Column mapping (requirement_evidence_map):
  requirement_hash  = content_hash of the canonical requirement payload
  evidence_key      = master_bullet_id (12-char SHA-1 hex from guard._bullet_id)
  evidence_text     = content_hash of the bullet's current text (from store.content_hash)

Freshness/invalidation:
  A mapping row is VALID when:
    content_hash(current_master_bullet_text) == stored evidence_text
  Any edit to the master bullet text (real content change) produces a different
  hash and INVALIDATES the mapping → regenerate path.
  Cosmetic whitespace/case changes produce the same hash → no invalidation.

Tests:
  - test_populate_from_bullet_selection_json
  - test_lookup_valid_returns_bullet_id
  - test_lookup_invalidated_when_bullet_text_changes
  - test_lookup_no_match_requirement
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from jobsmith import db as jobsmith_db
from jobsmith.reuse.evidence_map import lookup_mapped_bullet, populate_from_bullet_selection
from jobsmith.reuse.store import content_hash

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bullet_id(text: str) -> str:
    """Mirror guard._bullet_id — SHA-1 hex first 12 chars."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _make_requirement_hash(raw: str) -> str:
    """Hash a minimal requirement payload as ingest_canonical_requirements would."""
    payload = {"raw": raw, "canonical_tag": None, "normalized_phrase": raw.strip().lower()}
    return content_hash(payload)


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "jobsmith.db"
    return jobsmith_db.open_pipeline_db(db_path)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

BULLET_TEXT_A = "Led migration of $250M solar asset portfolio to new data platform, reducing latency by 60%."
BULLET_TEXT_B = "Automated 200K+ solar asset monitoring pipelines using Python and Airflow."

REQUIREMENT_A = "Python data engineering experience"
REQUIREMENT_B = "Solar energy domain knowledge"


def _sample_jd_parsed() -> dict:
    return {
        "must_haves": [
            {"raw": REQUIREMENT_A, "canonical_tag": None, "normalized_phrase": REQUIREMENT_A.strip().lower()},
        ],
        "nice_to_haves": [
            {"raw": REQUIREMENT_B, "canonical_tag": None, "normalized_phrase": REQUIREMENT_B.strip().lower()},
        ],
    }


def _sample_bullet_selection(req_a_hash: str, req_b_hash: str) -> dict:
    """Simulate a bullet-selection.json emitted by apply-bullet-selector.

    Each bullet in positions[] carries:
      master_bullet_id  — stable 12-char hash of bullet text
      included          — bool
      matched_requirement_hash — (new field) canonical req hash this bullet covers
    """
    bid_a = _bullet_id(BULLET_TEXT_A)
    bid_b = _bullet_id(BULLET_TEXT_B)
    return {
        "positions": [
            {
                "company": "SolarCo",
                "title": "Data Engineer",
                "bullets": [
                    {
                        "master_bullet_id": bid_a,
                        "text": BULLET_TEXT_A,
                        "included": True,
                        "rephrased": None,
                        "reason_if_dropped": None,
                        "matched_requirement_hash": req_a_hash,
                    },
                    {
                        "master_bullet_id": bid_b,
                        "text": BULLET_TEXT_B,
                        "included": True,
                        "rephrased": None,
                        "reason_if_dropped": None,
                        "matched_requirement_hash": req_b_hash,
                    },
                ],
            }
        ],
        "anchor_bullets_master": [bid_a],
        "anchor_bullets_kept": [bid_a],
        "anchor_bullets_dropped": [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_populate_from_bullet_selection_json(tmp_path: Path):
    """populate_from_bullet_selection inserts mapping rows for each bullet with a
    matched_requirement_hash."""
    conn = _open_db(tmp_path)

    req_a_hash = _make_requirement_hash(REQUIREMENT_A)
    req_b_hash = _make_requirement_hash(REQUIREMENT_B)

    selection = _sample_bullet_selection(req_a_hash, req_b_hash)
    rows_written = populate_from_bullet_selection(conn, selection=selection)

    assert rows_written == 2, f"Expected 2 rows, got {rows_written}"

    # Verify rows exist in DB
    from jobsmith.reuse.store import get_requirement_evidence

    bid_a = _bullet_id(BULLET_TEXT_A)
    bid_b = _bullet_id(BULLET_TEXT_B)

    row_a = get_requirement_evidence(conn, requirement_hash=req_a_hash, evidence_key=bid_a)
    assert row_a is not None, "Row for bullet A not found"
    assert row_a["evidence_key"] == bid_a
    # evidence_text is content_hash of bullet text
    assert row_a["evidence_text"] == content_hash(BULLET_TEXT_A)

    row_b = get_requirement_evidence(conn, requirement_hash=req_b_hash, evidence_key=bid_b)
    assert row_b is not None, "Row for bullet B not found"
    assert row_b["evidence_text"] == content_hash(BULLET_TEXT_B)

    conn.close()


def test_populate_idempotent(tmp_path: Path):
    """Running populate twice produces no duplicate rows and returns 0 on the
    second call (all rows already present)."""
    conn = _open_db(tmp_path)

    req_a_hash = _make_requirement_hash(REQUIREMENT_A)
    req_b_hash = _make_requirement_hash(REQUIREMENT_B)
    selection = _sample_bullet_selection(req_a_hash, req_b_hash)

    first = populate_from_bullet_selection(conn, selection=selection)
    second = populate_from_bullet_selection(conn, selection=selection)

    assert first == 2
    assert second == 0, f"Second populate should return 0 new rows, got {second}"

    conn.close()


def test_lookup_valid_returns_bullet_id(tmp_path: Path):
    """lookup_mapped_bullet returns the master_bullet_id when bullet text is unchanged."""
    conn = _open_db(tmp_path)

    req_a_hash = _make_requirement_hash(REQUIREMENT_A)
    req_b_hash = _make_requirement_hash(REQUIREMENT_B)
    selection = _sample_bullet_selection(req_a_hash, req_b_hash)
    populate_from_bullet_selection(conn, selection=selection)

    bid_a = _bullet_id(BULLET_TEXT_A)

    # Current bullet text hash == stored hash → valid → return bullet_id
    result = lookup_mapped_bullet(
        conn,
        requirement_hash=req_a_hash,
        current_bullet_texts={bid_a: BULLET_TEXT_A},
    )
    assert result == bid_a, f"Expected {bid_a!r}, got {result!r}"

    conn.close()


def test_lookup_invalidated_when_bullet_text_changes(tmp_path: Path):
    """lookup_mapped_bullet returns None when the master bullet has been edited
    (hash mismatch → auto-invalidation → regenerate path)."""
    conn = _open_db(tmp_path)

    req_a_hash = _make_requirement_hash(REQUIREMENT_A)
    req_b_hash = _make_requirement_hash(REQUIREMENT_B)
    selection = _sample_bullet_selection(req_a_hash, req_b_hash)
    populate_from_bullet_selection(conn, selection=selection)

    bid_a = _bullet_id(BULLET_TEXT_A)
    edited_text = BULLET_TEXT_A + " [edited by user]"

    # Current bullet text differs → stale → None
    result = lookup_mapped_bullet(
        conn,
        requirement_hash=req_a_hash,
        current_bullet_texts={bid_a: edited_text},
    )
    assert result is None, f"Expected None (invalidated), got {result!r}"

    conn.close()


def test_lookup_no_match_requirement(tmp_path: Path):
    """lookup_mapped_bullet returns None when no mapping row exists for the
    requirement_hash (new requirement — regenerate path)."""
    conn = _open_db(tmp_path)

    unknown_req_hash = _make_requirement_hash("Kubernetes orchestration at scale")
    bid_a = _bullet_id(BULLET_TEXT_A)

    result = lookup_mapped_bullet(
        conn,
        requirement_hash=unknown_req_hash,
        current_bullet_texts={bid_a: BULLET_TEXT_A},
    )
    assert result is None, f"Expected None (no mapping), got {result!r}"

    conn.close()


def test_lookup_no_bullet_texts_supplied(tmp_path: Path):
    """lookup_mapped_bullet returns None when current_bullet_texts is empty,
    because we cannot validate freshness without knowing the current text."""
    conn = _open_db(tmp_path)

    req_a_hash = _make_requirement_hash(REQUIREMENT_A)
    req_b_hash = _make_requirement_hash(REQUIREMENT_B)
    selection = _sample_bullet_selection(req_a_hash, req_b_hash)
    populate_from_bullet_selection(conn, selection=selection)

    result = lookup_mapped_bullet(
        conn,
        requirement_hash=req_a_hash,
        current_bullet_texts={},
    )
    assert result is None, "Expected None when no bullet texts supplied for freshness check"

    conn.close()


def test_populate_skips_bullets_without_requirement_hash(tmp_path: Path):
    """populate_from_bullet_selection skips bullets that have no
    matched_requirement_hash (older bullet-selection.json without reuse fields)."""
    conn = _open_db(tmp_path)

    selection = {
        "positions": [
            {
                "company": "OldCo",
                "title": "Engineer",
                "bullets": [
                    {
                        "master_bullet_id": _bullet_id(BULLET_TEXT_A),
                        "text": BULLET_TEXT_A,
                        "included": True,
                        # No matched_requirement_hash
                    }
                ],
            }
        ],
        "anchor_bullets_master": [],
        "anchor_bullets_kept": [],
        "anchor_bullets_dropped": [],
    }

    rows_written = populate_from_bullet_selection(conn, selection=selection)
    assert rows_written == 0, f"Expected 0 rows for selection without requirement hashes, got {rows_written}"

    conn.close()


def test_populate_skips_excluded_bullets(tmp_path: Path):
    """populate_from_bullet_selection skips bullets with included=False."""
    conn = _open_db(tmp_path)

    req_a_hash = _make_requirement_hash(REQUIREMENT_A)

    selection = {
        "positions": [
            {
                "company": "SolarCo",
                "title": "Data Engineer",
                "bullets": [
                    {
                        "master_bullet_id": _bullet_id(BULLET_TEXT_A),
                        "text": BULLET_TEXT_A,
                        "included": False,  # dropped bullet
                        "rephrased": None,
                        "reason_if_dropped": "Not relevant",
                        "matched_requirement_hash": req_a_hash,
                    },
                ],
            }
        ],
        "anchor_bullets_master": [],
        "anchor_bullets_kept": [],
        "anchor_bullets_dropped": [],
    }

    rows_written = populate_from_bullet_selection(conn, selection=selection)
    assert rows_written == 0, "Dropped bullets should not be mapped"

    conn.close()
