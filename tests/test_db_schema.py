"""Schema, connection, writer, and typed-deserialiser tests for jobsmith.db.

Ingest + backfill tests live in test_db_ingest.py.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from jobsmith import db as jobsmith_db


def test_schema_creates_all_tables(tmp_path: Path):
    """Fresh DBs have all five expected tables across both scopes."""
    pipeline_conn = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
    pipeline_tables = {
        row[0]
        for row in pipeline_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    pipeline_conn.close()
    assert {"apply_runs", "specialist_outputs"} <= pipeline_tables

    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    review_conn = jobsmith_db.open_review_db("my-company-job", review_dir)
    review_tables = {
        row[0]
        for row in review_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    review_conn.close()
    assert {"amendments", "chat_sessions", "chat_messages"} <= review_tables


def test_wal_mode_enabled(tmp_path: Path):
    """Pipeline DB opens with journal_mode=wal."""
    conn = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_review_wal_mode_enabled(tmp_path: Path):
    """Review DB opens with journal_mode=wal."""
    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    conn = jobsmith_db.open_review_db("my-slug", review_dir)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_apply_run_insert_and_query(pipeline_db):
    """Insert an apply_run row and retrieve it by slug."""
    conn, _ = pipeline_db
    run_id = str(uuid.uuid4())
    jobsmith_db.insert_apply_run(
        conn,
        run_id=run_id,
        slug="acme-swe",
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at="2024-01-01T10:05:00",
        status="complete",
    )

    row = jobsmith_db.get_apply_run_by_slug(conn, "acme-swe")
    assert row is not None
    assert row["run_id"] == run_id
    assert row["phase"] == "gather"
    assert row["status"] == "complete"


def test_specialist_output_insert(pipeline_db):
    """Insert a specialist_output row; output_json round-trips."""
    conn, _ = pipeline_db
    run_id = str(uuid.uuid4())
    jobsmith_db.insert_apply_run(
        conn,
        run_id=run_id,
        slug="beta-company",
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at=None,
        status="in-progress",
    )
    jobsmith_db.insert_specialist_output(
        conn,
        run_id=run_id,
        specialist="fit-scorer",
        kind="fit-score",
        output_json=json.dumps({"score": 0.75, "rationale": "Good match"}),
        transcript_ref=None,
        finished_at="2024-01-01T10:03:00",
    )

    rows = jobsmith_db.get_specialist_outputs(conn, run_id)
    assert len(rows) == 1
    assert rows[0]["kind"] == "fit-score"
    assert json.loads(rows[0]["output_json"])["score"] == 0.75


def test_amendment_pk_constraint(review_db):
    """Inserting a duplicate amendment_id raises IntegrityError."""
    conn, _ = review_db
    amendment_id = str(uuid.uuid4())
    jobsmith_db.insert_amendment(
        conn,
        amendment_id=amendment_id,
        slug="test-slug",
        run_id=None,
        section="summary",
        op="replace",
        value="new text",
        status="pending",
        created_at="2024-01-01T10:00:00",
    )
    with pytest.raises(sqlite3.IntegrityError):
        jobsmith_db.insert_amendment(
            conn,
            amendment_id=amendment_id,
            slug="test-slug",
            run_id=None,
            section="summary",
            op="replace",
            value="different text",
            status="pending",
            created_at="2024-01-01T10:01:00",
        )


def test_typed_deserializer_roundtrip(pipeline_db):
    """Inserting a fit-score row deserialises into the FitScore typed model."""
    conn, _ = pipeline_db
    run_id = str(uuid.uuid4())
    jobsmith_db.insert_apply_run(
        conn,
        run_id=run_id,
        slug="deserialize-test",
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at=None,
        status="in-progress",
    )
    jobsmith_db.insert_specialist_output(
        conn,
        run_id=run_id,
        specialist="fit-scorer",
        kind="fit-score",
        output_json=json.dumps({
            "score": 0.9,
            "score_raw": 0.9,
            "rationale": "Excellent fit",
            "specialty": "ml",
            "confidence": "high",
            "must_have_table": [],
            "matched_evidence": ["10 years ML"],
            "concerns": [],
            "pitch": "ML expert",
        }),
        transcript_ref=None,
        finished_at="2024-01-01T10:03:00",
    )

    rows = jobsmith_db.get_specialist_outputs(conn, run_id)
    model = jobsmith_db.deserialize_output(rows[0])
    assert model.score == 0.9
    assert model.specialty == "ml"


def test_review_db_per_slug_isolation(tmp_path: Path):
    """Amendments inserted into slug-A's review DB do not appear in slug-B's."""
    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    conn_a = jobsmith_db.open_review_db("slug-a", review_dir)
    conn_b = jobsmith_db.open_review_db("slug-b", review_dir)

    jobsmith_db.insert_amendment(
        conn_a,
        amendment_id=str(uuid.uuid4()),
        slug="slug-a",
        run_id=None,
        section="experience",
        op="append",
        value="Extra bullet",
        status="pending",
        created_at="2024-01-01T10:00:00",
    )
    count_in_b = conn_b.execute("SELECT COUNT(*) FROM amendments").fetchone()[0]
    conn_a.close()
    conn_b.close()
    assert count_in_b == 0
