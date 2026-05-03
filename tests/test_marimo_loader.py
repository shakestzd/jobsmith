"""Tests for jobsmith.marimo.loader — DB → typed Sections loader.

TDD: these tests are written BEFORE the implementation.
They cover the pure loader logic only; no marimo UI is tested here.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobsmith.db import (
    insert_apply_run,
    insert_specialist_output,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_ID = "run-loader-test-001"
_SLUG = "acme-swe-2024"
_NOW = "2024-01-01T00:00:00+00:00"


def _insert_run(conn: sqlite3.Connection, slug: str = _SLUG, run_id: str = _RUN_ID) -> None:
    insert_apply_run(
        conn,
        run_id=run_id,
        slug=slug,
        phase="gather",
        started_at=_NOW,
        finished_at=_NOW,
        status="complete",
    )


def _insert_output(conn: sqlite3.Connection, kind: str, payload: dict, run_id: str = _RUN_ID) -> None:
    insert_specialist_output(
        conn,
        run_id=run_id,
        specialist=kind,
        kind=kind,
        output_json=json.dumps(payload),
        transcript_ref=None,
        finished_at=_NOW,
    )


# ---------------------------------------------------------------------------
# test_load_complete_sections
# ---------------------------------------------------------------------------

def test_load_complete_sections(pipeline_db):
    """All specialist outputs present → all Sections fields populated (not None)."""
    from jobsmith.marimo.loader import Sections, load_sections

    conn, db_path = pipeline_db
    _insert_run(conn)
    _insert_output(conn, "bullet-selection", {
        "positions": [],
        "anchor_bullets_master": [],
        "anchor_bullets_kept": [],
        "anchor_bullets_dropped": [],
    })
    _insert_output(conn, "fit-score", {
        "score": 0.85,
        "score_raw": 0.85,
        "rationale": "Good match",
        "specialty": "backend",
        "confidence": "high",
        "must_have_table": [],
        "matched_evidence": ["Python 5y"],
        "concerns": [],
        "pitch": "Strong engineer",
    })
    _insert_output(conn, "hm-snippet", {
        "detected": True,
        "name": "Jane Smith",
        "source": "linkedin",
        "one_specific_signal": "Led Go migration",
        "suggested_hook": "I noticed your Go migration",
    })
    _insert_output(conn, "ats-check", {
        "score": 0.9,
        "issues": [],
        "suggestions": ["Add 'distributed systems'"],
    })
    _insert_output(conn, "prose-draft", {"text": "Experienced engineer seeking…"})

    sections = load_sections(_SLUG, db_path)

    assert isinstance(sections, Sections)
    assert sections.work_bullets is not None
    assert sections.fit_score is not None
    assert sections.hm_snippet is not None
    assert sections.ats_check is not None
    assert sections.prose_draft is not None
    # cover_letter depends on a file, so it may be None here — that is acceptable
    assert sections.fit_score.score == pytest.approx(0.85)
    assert sections.hm_snippet.name == "Jane Smith"


# ---------------------------------------------------------------------------
# test_load_partial_sections
# ---------------------------------------------------------------------------

def test_load_partial_sections(pipeline_db):
    """Only work-bullets and fit-score rows present → others are None."""
    from jobsmith.marimo.loader import Sections, load_sections

    conn, db_path = pipeline_db
    _insert_run(conn)
    _insert_output(conn, "bullet-selection", {
        "positions": [],
        "anchor_bullets_master": [],
        "anchor_bullets_kept": [],
        "anchor_bullets_dropped": [],
    })
    _insert_output(conn, "fit-score", {
        "score": 0.7,
        "score_raw": 0.7,
        "rationale": "Partial match",
        "specialty": "fullstack",
        "confidence": "medium",
        "must_have_table": [],
        "matched_evidence": [],
        "concerns": ["Missing AWS"],
        "pitch": "Versatile engineer",
    })

    sections = load_sections(_SLUG, db_path)

    assert isinstance(sections, Sections)
    assert sections.work_bullets is not None
    assert sections.fit_score is not None
    # Unpopulated specialist outputs → None
    assert sections.hm_snippet is None
    assert sections.ats_check is None
    assert sections.prose_draft is None
    assert sections.cover_letter is None


# ---------------------------------------------------------------------------
# test_load_unknown_slug
# ---------------------------------------------------------------------------

def test_load_unknown_slug(pipeline_db):
    """Slug not in DB → raises ApplicationNotFound."""
    from jobsmith.marimo.loader import ApplicationNotFound, load_sections

    _conn, db_path = pipeline_db

    with pytest.raises(ApplicationNotFound):
        load_sections("does-not-exist-slug", db_path)


# ---------------------------------------------------------------------------
# test_load_missing_section_placeholder
# ---------------------------------------------------------------------------

def test_load_missing_section_placeholder(pipeline_db):
    """Section absent from DB → field is None (no exception raised)."""
    from jobsmith.marimo.loader import load_sections

    conn, db_path = pipeline_db
    _insert_run(conn)
    # Only insert fit-score; all others absent

    _insert_output(conn, "fit-score", {
        "score": 0.6,
        "score_raw": 0.6,
        "rationale": "Weak match",
        "specialty": "data",
        "confidence": "low",
        "must_have_table": [],
        "matched_evidence": [],
        "concerns": ["Missing ML"],
        "pitch": "Data professional",
    })

    # Must not raise
    sections = load_sections(_SLUG, db_path)
    assert sections.work_bullets is None
    assert sections.hm_snippet is None
    assert sections.ats_check is None
    assert sections.prose_draft is None


# ---------------------------------------------------------------------------
# test_notebook_imports_cleanly
# ---------------------------------------------------------------------------

def test_notebook_imports_cleanly():
    """Importing jobsmith.marimo.apply must not raise (we do not run cells)."""
    import importlib

    # Should succeed without launching a marimo server
    mod = importlib.import_module("jobsmith.marimo.apply")
    assert mod is not None


def test_loader_reads_app_root_cover_letter_draft(tmp_path: Path):
    """Roborev #921 LOW: loader prefers <app>/cover-letter-draft.md.

    The apply pipeline writes the reviewable draft at app root, not under
    documents/. Earlier loader only looked under documents/cover-letter-final.md
    so the section was always blank for normal pipeline output.
    """
    from jobsmith.db import insert_apply_run, open_pipeline_db
    from jobsmith.marimo.loader import load_sections

    apps = tmp_path / "applications"
    apps.mkdir()
    app_dir = apps / "draft-slug"
    app_dir.mkdir()
    (app_dir / "cover-letter-draft.md").write_text("Dear hiring manager,")

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    insert_apply_run(
        conn,
        run_id="r1",
        slug="draft-slug",
        phase="render",
        started_at="2024-01-01T10:00:00",
        finished_at="2024-01-01T11:00:00",
        status="done",
    )
    conn.close()

    sections = load_sections("draft-slug", db_path, applications_dir=apps)
    assert sections.cover_letter == "Dear hiring manager,"


def test_loader_prefers_draft_over_finalized(tmp_path: Path):
    """When both draft and final exist (post-Finalize), prefer draft.

    Both are equally fresh in practice — Finalize copies through — but
    the draft path is canonical for review.
    """
    from jobsmith.db import insert_apply_run, open_pipeline_db
    from jobsmith.marimo.loader import load_sections

    apps = tmp_path / "applications"
    apps.mkdir()
    app_dir = apps / "both-slug"
    app_dir.mkdir()
    (app_dir / "cover-letter-draft.md").write_text("DRAFT version")
    (app_dir / "cover-letter-final.md").write_text("FINAL version")

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    insert_apply_run(
        conn,
        run_id="r2",
        slug="both-slug",
        phase="render",
        started_at="2024-01-01T10:00:00",
        finished_at="2024-01-01T11:00:00",
        status="done",
    )
    conn.close()

    sections = load_sections("both-slug", db_path, applications_dir=apps)
    assert sections.cover_letter == "DRAFT version"

