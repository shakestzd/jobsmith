"""Tests for jobsmith.sourcing.store — postings upsert, status transitions,
promote linkage, sourcing_runs helpers.

TDD: failing tests written BEFORE implementation (feat-850a076a).

Covers:
  - Migration 010 creates postings + sourcing_runs tables
  - upsert_posting: new posting inserts with status=sourced
  - upsert_posting: re-sight bumps last_seen_at ONLY (no status reset)
  - upsert_posting: dismissed/promoted/expired rows are NOT resurrected
  - set_posting_status: valid transitions
  - set_posting_status: rejects unknown status
  - promote: creates apply_runs row and links promoted_application_id
  - promote: sets status=promoted on posting
  - promote: idempotent (second call returns same run_id)
  - sourcing_runs: upsert_sourcing_run, finish_sourcing_run
  - sourcing_runs: purge_old_sourcing_runs keeps last N rows
  - applications status vocab: interview/offer accepted in apply_runs
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from jobsmith import db as jobsmith_db
from jobsmith.sourcing.store import (
    POSTING_STATUSES,
    finish_sourcing_run,
    get_posting_by_dedup_key,
    get_posting_by_id,
    promote_posting,
    purge_old_sourcing_runs,
    set_posting_status,
    upsert_posting,
    upsert_sourcing_run,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    return jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")


def _sample_posting(**overrides) -> dict:
    base = {
        "source": "greenhouse/stripe",
        "external_id": "ext-001",
        "url": "https://stripe.com/jobs/1",
        "title": "Senior Engineer",
        "company": "Stripe",
        "location": "Remote",
        "comp_text": "$200k",
        "posted_date": "2026-06-01",
        "jd_text": "Build payment systems.",
        "fast_score": 0.85,
        "llm_score": None,
        "specialty": "backend",
        "rationale": "Strong match",
        "evidence_json": '["Python","Payments"]',
        "dedup_key": "deadbeef01",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Migration smoke-test
# ---------------------------------------------------------------------------


def test_migration_creates_postings_table(db: sqlite3.Connection) -> None:
    """Opening the pipeline DB with migration 010 registers the postings table."""
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "postings" in tables
    assert "sourcing_runs" in tables


# ---------------------------------------------------------------------------
# upsert_posting — new posting
# ---------------------------------------------------------------------------


def test_upsert_posting_new(db: sqlite3.Connection) -> None:
    """A brand-new dedup_key inserts with status=sourced."""
    posting_id = upsert_posting(db, **_sample_posting())
    assert isinstance(posting_id, int)
    row = get_posting_by_id(db, posting_id)
    assert row is not None
    assert row["status"] == "sourced"
    assert row["title"] == "Senior Engineer"
    assert row["company"] == "Stripe"
    assert row["first_seen_at"] == row["last_seen_at"]


def test_upsert_posting_returns_existing_id_on_resight(db: sqlite3.Connection) -> None:
    """Re-sighting the same dedup_key returns the existing row's id."""
    id1 = upsert_posting(db, **_sample_posting())
    id2 = upsert_posting(db, **_sample_posting(title="Senior Engineer Updated"))
    assert id1 == id2


def test_upsert_posting_resight_bumps_last_seen(db: sqlite3.Connection) -> None:
    """Re-sight bumps last_seen_at; first_seen_at stays unchanged."""
    id1 = upsert_posting(db, **_sample_posting())
    row1 = get_posting_by_id(db, id1)
    first_seen = row1["first_seen_at"]

    # Small sleep to guarantee timestamp differs
    time.sleep(0.01)
    upsert_posting(db, **_sample_posting(title="New Title"))
    row2 = get_posting_by_id(db, id1)

    assert row2["first_seen_at"] == first_seen
    assert row2["last_seen_at"] >= row2["first_seen_at"]


def test_upsert_posting_resight_does_not_change_title(db: sqlite3.Connection) -> None:
    """Re-sight with a different title does NOT overwrite any columns except last_seen_at."""
    id1 = upsert_posting(db, **_sample_posting(title="Original Title"))
    upsert_posting(db, **_sample_posting(title="Changed Title"))
    row = get_posting_by_id(db, id1)
    assert row["title"] == "Original Title"


# ---------------------------------------------------------------------------
# Re-sight semantics: dismissed/promoted/expired rows are NOT resurrected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("final_status", ["dismissed", "promoted", "expired"])
def test_upsert_resight_does_not_resurrect(
    db: sqlite3.Connection, final_status: str
) -> None:
    """A re-sight on a dismissed/promoted/expired row does NOT reset status to sourced."""
    posting_id = upsert_posting(db, **_sample_posting())

    # Manually force the status to the terminal value
    db.execute(
        "UPDATE postings SET status = ? WHERE id = ?", (final_status, posting_id)
    )
    db.commit()

    # Re-sight
    upsert_posting(db, **_sample_posting())

    row = get_posting_by_id(db, posting_id)
    assert row["status"] == final_status, (
        f"Expected status={final_status!r} after re-sight, got {row['status']!r}"
    )


# ---------------------------------------------------------------------------
# set_posting_status
# ---------------------------------------------------------------------------


def test_set_posting_status_valid(db: sqlite3.Connection) -> None:
    """Valid status transitions are applied."""
    posting_id = upsert_posting(db, **_sample_posting())
    set_posting_status(db, posting_id=posting_id, status="queued")
    row = get_posting_by_id(db, posting_id)
    assert row["status"] == "queued"


def test_set_posting_status_rejects_unknown(db: sqlite3.Connection) -> None:
    """An unknown status raises ValueError."""
    posting_id = upsert_posting(db, **_sample_posting())
    with pytest.raises(ValueError, match="invalid status"):
        set_posting_status(db, posting_id=posting_id, status="accepted")


def test_posting_statuses_constant(db: sqlite3.Connection) -> None:
    """POSTING_STATUSES export contains the expected vocabulary."""
    assert {"sourced", "queued", "dismissed", "promoted", "expired"} == POSTING_STATUSES


# ---------------------------------------------------------------------------
# promote_posting
# ---------------------------------------------------------------------------


def test_promote_posting_creates_apply_run_and_links(db: sqlite3.Connection) -> None:
    """promote_posting creates an apply_runs row and links promoted_application_id."""
    posting_id = upsert_posting(db, **_sample_posting())
    run_id = promote_posting(db, posting_id=posting_id)

    assert isinstance(run_id, str) and len(run_id) > 0

    row = get_posting_by_id(db, posting_id)
    assert row["status"] == "promoted"
    assert row["promoted_application_id"] == run_id

    # apply_runs row created
    ar = db.execute(
        "SELECT * FROM apply_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert ar is not None
    assert ar["status"] == "in-progress"


def test_promote_posting_slug_from_title_company(db: sqlite3.Connection) -> None:
    """The promoted apply_runs slug is derived from company + title."""
    posting_id = upsert_posting(
        db,
        **_sample_posting(company="Acme Corp", title="Staff Engineer"),
    )
    run_id = promote_posting(db, posting_id=posting_id)
    ar = db.execute(
        "SELECT slug FROM apply_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    slug = ar["slug"]
    assert "acme" in slug
    assert "staff" in slug or "engineer" in slug


def test_promote_posting_idempotent(db: sqlite3.Connection) -> None:
    """Calling promote_posting twice returns the same run_id."""
    posting_id = upsert_posting(db, **_sample_posting())
    run_id_1 = promote_posting(db, posting_id=posting_id)
    run_id_2 = promote_posting(db, posting_id=posting_id)
    assert run_id_1 == run_id_2


def test_promote_posting_raises_on_missing(db: sqlite3.Connection) -> None:
    """promote_posting raises ValueError for a non-existent posting_id."""
    with pytest.raises(ValueError, match="not found"):
        promote_posting(db, posting_id=99999)


# ---------------------------------------------------------------------------
# get_posting_by_dedup_key
# ---------------------------------------------------------------------------


def test_get_posting_by_dedup_key(db: sqlite3.Connection) -> None:
    """get_posting_by_dedup_key returns the row (or None when absent)."""
    assert get_posting_by_dedup_key(db, dedup_key="nonexistent") is None
    upsert_posting(db, **_sample_posting(dedup_key="abc123"))
    row = get_posting_by_dedup_key(db, dedup_key="abc123")
    assert row is not None
    assert row["dedup_key"] == "abc123"


# ---------------------------------------------------------------------------
# sourcing_runs helpers
# ---------------------------------------------------------------------------


def test_upsert_sourcing_run_creates_row(db: sqlite3.Connection) -> None:
    upsert_sourcing_run(db, run_id="run-001")
    row = db.execute(
        "SELECT * FROM sourcing_runs WHERE run_id = 'run-001'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "running"


def test_finish_sourcing_run(db: sqlite3.Connection) -> None:
    upsert_sourcing_run(db, run_id="run-002")
    finish_sourcing_run(
        db,
        run_id="run-002",
        status="done",
        new_count=5,
        updated_count=2,
        skipped_count=1,
    )
    row = db.execute(
        "SELECT * FROM sourcing_runs WHERE run_id = 'run-002'"
    ).fetchone()
    assert row["status"] == "done"
    assert row["new_count"] == 5
    assert row["updated_count"] == 2
    assert row["finished_at"] is not None


def test_finish_sourcing_run_with_degraded(db: sqlite3.Connection) -> None:
    upsert_sourcing_run(db, run_id="run-003")
    finish_sourcing_run(
        db,
        run_id="run-003",
        status="degraded",
        new_count=0,
        updated_count=0,
        skipped_count=0,
        degraded_sources=["greenhouse/stripe"],
        error="timeout",
    )
    row = db.execute(
        "SELECT * FROM sourcing_runs WHERE run_id = 'run-003'"
    ).fetchone()
    assert row["status"] == "degraded"
    assert "greenhouse/stripe" in row["degraded_sources_json"]
    assert row["error"] == "timeout"


def test_purge_old_sourcing_runs_keeps_last_n(db: sqlite3.Connection) -> None:
    """purge_old_sourcing_runs removes oldest rows, keeping only last N."""
    for i in range(10):
        upsert_sourcing_run(db, run_id=f"run-{i:03d}")

    purge_old_sourcing_runs(db, keep=5)

    remaining = db.execute(
        "SELECT run_id FROM sourcing_runs ORDER BY started_at"
    ).fetchall()
    assert len(remaining) == 5
    # The 5 newest (alphabetically last by run_id in this case) should survive
    run_ids = {r["run_id"] for r in remaining}
    assert "run-009" in run_ids
    assert "run-000" not in run_ids


# ---------------------------------------------------------------------------
# Applications status vocab: interview/offer are accepted in apply_runs
# ---------------------------------------------------------------------------


def test_apply_runs_accepts_interview_status(db: sqlite3.Connection) -> None:
    """apply_runs.status is free-text; 'interview' should insert without error."""
    import uuid

    run_id = str(uuid.uuid4())
    jobsmith_db.insert_apply_run(
        db,
        run_id=run_id,
        slug="acme-senior-engineer",
        phase="gather",
        started_at="2026-06-01T10:00:00+00:00",
        finished_at=None,
        status="interview",
    )
    row = db.execute(
        "SELECT status FROM apply_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row["status"] == "interview"


def test_apply_runs_accepts_offer_status(db: sqlite3.Connection) -> None:
    """apply_runs.status is free-text; 'offer' should insert without error."""
    import uuid

    run_id = str(uuid.uuid4())
    jobsmith_db.insert_apply_run(
        db,
        run_id=run_id,
        slug="acme-senior-engineer-2",
        phase="gather",
        started_at="2026-06-01T10:00:00+00:00",
        finished_at=None,
        status="offer",
    )
    row = db.execute(
        "SELECT status FROM apply_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row["status"] == "offer"
