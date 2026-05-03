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

    (state_dir / "manifest.json").write_text(
        json.dumps({
            "phases": {
                "gather": {
                    "status": "complete",
                    "specialists": {
                        "jd-parser": {"output": "jd-parsed.json", "status": "complete"},
                        "fit-scorer": {"output": "fit-score.json", "status": "complete"},
                        "bullet-selector": {"output": "bullet-selection.json", "status": "complete"},
                    },
                }
            }
        })
    )

    return state_dir
