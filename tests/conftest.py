"""Shared pytest fixtures (auto-loaded for all tests/ files).

Currently scoped to apply-pipeline DB fixtures: pipeline_db, review_db,
and fixture_state_dir.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsmith import db as jobsmith_db


@pytest.fixture()
def pipeline_db(tmp_path: Path):
    """Open pipeline DB connection with schema applied."""
    db_path = tmp_path / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    yield conn, db_path
    conn.close()


@pytest.fixture()
def review_db(tmp_path: Path):
    """Open per-slug review DB connection with schema applied."""
    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    conn = jobsmith_db.open_review_db("test-slug", review_dir)
    yield conn, review_dir
    conn.close()


@pytest.fixture()
def fixture_state_dir(tmp_path: Path) -> Path:
    """Minimal .apply-state/ with jd-parsed, fit-score, bullet-selection artifacts."""
    state_dir = tmp_path / ".apply-state"
    state_dir.mkdir()

    (state_dir / "jd-parsed.json").write_text(
        json.dumps({
            "company": "Acme Corp",
            "position": "Software Engineer",
            "location": "Remote",
            "location_type": "remote",
            "salary_range": "$120k-$160k",
            "req_id": "SWE-001",
            "apply_url": "https://acme.com/jobs/1",
            "role_type": "ic",
            "jd_text_clean": "We are hiring...",
            "must_haves": ["Python", "AWS"],
            "nice_to_haves": ["Rust"],
            "top_keywords": ["python", "cloud"],
        })
    )

    (state_dir / "fit-score.json").write_text(
        json.dumps({
            "score": 0.82,
            "score_raw": 0.82,
            "rationale": "Strong match",
            "specialty": "backend",
            "confidence": "high",
            "must_have_table": [],
            "matched_evidence": ["5 years Python"],
            "concerns": [],
            "pitch": "Experienced backend engineer",
        })
    )

    (state_dir / "bullet-selection.json").write_text(
        json.dumps({
            "positions": [],
            "anchor_bullets_master": [],
            "anchor_bullets_kept": [],
            "anchor_bullets_dropped": [],
        })
    )

    # Real apply-pipeline manifest format: flat invocations[] at top level,
    # specialist names match SPECIALIST_TO_ARTIFACT in _state_readers.
    (state_dir / "manifest.json").write_text(
        json.dumps({
            "run_id": "test-run-id",
            "slug": "test-slug",
            "started_at": "2024-01-01T10:00:00",
            "invocations": [
                {
                    "specialist": "apply-jd-parser",
                    "status": "ok",
                    "started_at": "2024-01-01T10:00:01",
                    "finished_at": "2024-01-01T10:00:02",
                },
                {
                    "specialist": "apply-fit-scorer",
                    "status": "ok",
                    "started_at": "2024-01-01T10:00:03",
                    "finished_at": "2024-01-01T10:00:04",
                },
                {
                    "specialist": "apply-bullet-selector",
                    "status": "ok",
                    "started_at": "2024-01-01T10:00:05",
                    "finished_at": "2024-01-01T10:00:06",
                },
            ],
        })
    )

    return state_dir
