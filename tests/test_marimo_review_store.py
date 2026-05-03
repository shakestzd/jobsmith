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


def test_persist_amendment_dedup_after_accept_reject(tmp_path: Path):
    """Re-parsing the same directive after accept/reject must not re-insert.

    Regression for roborev #920 MEDIUM: dedup previously matched only
    status='pending', so once the user accepted or rejected an
    amendment, re-parsing the same chat history would create a fresh
    pending row, restoring the dropdown after the user dismissed it.
    """
    from jobsmith.marimo.directive_parser import Amendment
    from jobsmith.marimo.review_store import persist_amendment, set_status

    review_dir = tmp_path / ".review"
    review_dir.mkdir()

    a = Amendment(
        id="00000000-0000-4000-8000-000000000001",
        section="work",
        index=0,
        field="bullet[2]",
        op="replace",
        value="quantify impact",
    )
    first_id = persist_amendment("slug-x", a, review_dir)
    set_status("slug-x", first_id, "accepted", review_dir)

    # Re-parse same directive (fresh UUID4 from parser); persist must dedup
    # against the accepted row instead of inserting a new pending one.
    a2 = Amendment(
        id="00000000-0000-4000-8000-000000000002",
        section="work",
        index=0,
        field="bullet[2]",
        op="replace",
        value="quantify impact",
    )
    second_id = persist_amendment("slug-x", a2, review_dir)
    assert second_id == first_id, (
        "dedup must return the existing accepted amendment_id"
    )

    # And again after rejection
    set_status("slug-x", first_id, "rejected", review_dir)
    a3 = Amendment(
        id="00000000-0000-4000-8000-000000000003",
        section="work",
        index=0,
        field="bullet[2]",
        op="replace",
        value="quantify impact",
    )
    third_id = persist_amendment("slug-x", a3, review_dir)
    assert third_id == first_id


def test_persist_amendment_reinsert_after_stale(tmp_path: Path):
    """Stale (archived from a prior run) amendments do NOT block re-insert.

    A new apply run produces fresh content; if the user is still asking for
    the same edit, that should land as a new pending row, not match the
    archived stale row from the previous cycle.
    """
    from jobsmith.marimo.directive_parser import Amendment
    from jobsmith.marimo.review_store import persist_amendment, set_status

    review_dir = tmp_path / ".review"
    review_dir.mkdir()

    a = Amendment(
        id="00000000-0000-4000-8000-000000000010",
        section="cover-letter",
        index=None,
        field="opening",
        op="replace",
        value="emphasize cross-functional impact",
    )
    first_id = persist_amendment("slug-y", a, review_dir)
    set_status("slug-y", first_id, "stale", review_dir)

    a2 = Amendment(
        id="00000000-0000-4000-8000-000000000011",
        section="cover-letter",
        index=None,
        field="opening",
        op="replace",
        value="emphasize cross-functional impact",
    )
    second_id = persist_amendment("slug-y", a2, review_dir)
    assert second_id != first_id, (
        "stale rows must not block re-insertion in a new run"
    )



def test_persist_amendment_roundtrips_index_and_field(tmp_path: Path):
    """Roborev #921 HIGH: target_index + target_field round-trip via the DB.

    Without these columns, AMEND work[0].bullet[2] persists as
    (section=work, op=replace, value=...) only — and Finalize reconstructs
    Amendment(index=None, field=None) which the YAML applier rejects.
    """
    import sqlite3

    from jobsmith.marimo.directive_parser import Amendment
    from jobsmith.marimo.review_store import persist_amendment

    review_dir = tmp_path / ".review"
    review_dir.mkdir()

    a = Amendment(
        id="00000000-0000-4000-8000-000000000777",
        section="work",
        index=0,
        field="bullet[2]",
        op="replace",
        value="quantify impact",
    )
    persist_amendment("slug-rt", a, review_dir)

    # Open the DB directly and check the columns landed
    conn = sqlite3.connect(str(review_dir / "slug-rt.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT target_index, target_field, section, op, value "
        "FROM amendments WHERE slug=?",
        ("slug-rt",),
    ).fetchone()
    conn.close()

    assert row["target_index"] == 0
    assert row["target_field"] == "bullet[2]"
    assert row["section"] == "work"
    assert row["op"] == "replace"
    assert row["value"] == "quantify impact"


def test_persist_amendment_dedup_distinguishes_targets(tmp_path: Path):
    """Two AMENDs differing only by index must NOT dedup against each other.

    Without target_index/target_field in the dedup query, edits to
    different bullets in the same section would collapse to one row.
    """
    from jobsmith.marimo.directive_parser import Amendment
    from jobsmith.marimo.review_store import persist_amendment

    review_dir = tmp_path / ".review"
    review_dir.mkdir()

    a = Amendment(
        id="00000000-0000-4000-8000-000000000801",
        section="work",
        index=0,
        field="bullet[0]",
        op="replace",
        value="same value",
    )
    b = Amendment(
        id="00000000-0000-4000-8000-000000000802",
        section="work",
        index=0,
        field="bullet[1]",  # different bullet target
        op="replace",
        value="same value",
    )
    id_a = persist_amendment("slug-dedup", a, review_dir)
    id_b = persist_amendment("slug-dedup", b, review_dir)
    assert id_a != id_b, "amendments targeting different bullets must NOT dedup"

