"""Tests for jobsmith.marimo.review_store — SQLite amendment persistence.

Covers:
- Idempotent persist (dedup by content)
- Distinct content creates 2 rows
- set_status updates a row
- Slug isolation (per-slug DB)
- archive_pending_for_run flips older pending → stale
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from jobsmith import db as jobsmith_db
from jobsmith.marimo.directive_parser import Amendment
from jobsmith.marimo.review_store import (
    archive_pending_for_run,
    persist_amendment,
    set_status,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def review_dir(tmp_path: Path) -> Path:
    """Create and return a fresh .review directory."""
    d = tmp_path / ".review"
    d.mkdir()
    return d


def _make_amendment(
    section: str = "work",
    field: str = "summary",
    op: str = "replace",
    value: str = "some text",
    run_id: str | None = None,
) -> Amendment:
    return Amendment(
        id=str(uuid.uuid4()),
        section=section,
        index=None,
        field=field,
        op=op,
        value=value,
        status="pending",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_persist_amendment_idempotent(review_dir: Path):
    """Call persist twice with same (slug, section, op, value, status='pending') → 1 row."""
    a = _make_amendment(value="tighten intro")
    slug = "test-co-swe"

    id1 = persist_amendment(slug, a, review_dir)
    id2 = persist_amendment(slug, a, review_dir)

    # Same stored ID returned both times
    assert id1 == id2

    # Only one row exists
    conn = jobsmith_db.open_review_db(slug, review_dir)
    count = conn.execute("SELECT COUNT(*) FROM amendments WHERE slug=?", (slug,)).fetchone()[0]
    conn.close()
    assert count == 1


def test_persist_amendment_distinct_content(review_dir: Path):
    """Two amendments with different values → 2 rows."""
    slug = "test-co-swe"
    a1 = _make_amendment(value="tighten intro")
    a2 = _make_amendment(value="quantify bullet")

    persist_amendment(slug, a1, review_dir)
    persist_amendment(slug, a2, review_dir)

    conn = jobsmith_db.open_review_db(slug, review_dir)
    count = conn.execute("SELECT COUNT(*) FROM amendments WHERE slug=?", (slug,)).fetchone()[0]
    conn.close()
    assert count == 2


def test_set_status_updates_row(review_dir: Path):
    """Persist a pending amendment then set_status to 'accepted'; row reflects change."""
    slug = "test-co-swe"
    a = _make_amendment(value="emphasize leadership")
    amendment_id = persist_amendment(slug, a, review_dir)

    set_status(slug, amendment_id, "accepted", review_dir)

    conn = jobsmith_db.open_review_db(slug, review_dir)
    row = conn.execute(
        "SELECT status FROM amendments WHERE amendment_id=?", (amendment_id,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "accepted"


def test_set_status_rejected(review_dir: Path):
    """set_status to 'rejected' also works."""
    slug = "test-co-swe"
    a = _make_amendment(value="remove redundant line")
    amendment_id = persist_amendment(slug, a, review_dir)

    set_status(slug, amendment_id, "rejected", review_dir)

    conn = jobsmith_db.open_review_db(slug, review_dir)
    row = conn.execute(
        "SELECT status FROM amendments WHERE amendment_id=?", (amendment_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "rejected"


def test_amendments_scoped_by_slug(review_dir: Path):
    """Two slugs use separate DBs; each sees only its own amendments."""
    slug_a = "company-a-role"
    slug_b = "company-b-role"

    a = _make_amendment(value="shared value text")
    persist_amendment(slug_a, a, review_dir)

    # slug_b has no amendments
    conn_b = jobsmith_db.open_review_db(slug_b, review_dir)
    count_b = conn_b.execute("SELECT COUNT(*) FROM amendments").fetchone()[0]
    conn_b.close()
    assert count_b == 0

    # slug_a has 1 amendment
    conn_a = jobsmith_db.open_review_db(slug_a, review_dir)
    count_a = conn_a.execute(
        "SELECT COUNT(*) FROM amendments WHERE slug=?", (slug_a,)
    ).fetchone()[0]
    conn_a.close()
    assert count_a == 1


def test_archive_pending_for_run(review_dir: Path):
    """Pending amendments with a different run_id flip to status='stale' after archive."""
    slug = "test-co-swe"
    old_run_id = str(uuid.uuid4())
    new_run_id = str(uuid.uuid4())

    # Insert two pending amendments linked to old_run_id
    a1 = _make_amendment(value="old amendment 1")
    a2 = _make_amendment(value="old amendment 2")
    id1 = persist_amendment(slug, a1, review_dir, run_id=old_run_id)
    id2 = persist_amendment(slug, a2, review_dir, run_id=old_run_id)

    # Archive them when a new run starts
    archive_pending_for_run(slug, new_run_id, review_dir)

    conn = jobsmith_db.open_review_db(slug, review_dir)
    rows = conn.execute(
        "SELECT amendment_id, status FROM amendments WHERE slug=?", (slug,)
    ).fetchall()
    conn.close()

    statuses = {row["amendment_id"]: row["status"] for row in rows}
    assert statuses[id1] == "stale"
    assert statuses[id2] == "stale"


def test_archive_preserves_amendments_from_current_run(review_dir: Path):
    """Amendments belonging to the current run_id are NOT archived to stale."""
    slug = "test-co-swe"
    current_run_id = str(uuid.uuid4())

    a = _make_amendment(value="current run amendment")
    amendment_id = persist_amendment(slug, a, review_dir, run_id=current_run_id)

    # Archive with the same run_id — should not change status
    archive_pending_for_run(slug, current_run_id, review_dir)

    conn = jobsmith_db.open_review_db(slug, review_dir)
    row = conn.execute(
        "SELECT status FROM amendments WHERE amendment_id=?", (amendment_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "pending"


def test_archive_null_run_id_becomes_stale(review_dir: Path):
    """Amendments with run_id=NULL (no run yet) are archived to stale on a new run."""
    slug = "test-co-swe"
    new_run_id = str(uuid.uuid4())

    a = _make_amendment(value="unlinked amendment")
    amendment_id = persist_amendment(slug, a, review_dir, run_id=None)

    archive_pending_for_run(slug, new_run_id, review_dir)

    conn = jobsmith_db.open_review_db(slug, review_dir)
    row = conn.execute(
        "SELECT status FROM amendments WHERE amendment_id=?", (amendment_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "stale"
