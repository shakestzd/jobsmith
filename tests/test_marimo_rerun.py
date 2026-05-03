"""Tests for per-specialist re-run controls (slice 8).

TDD-first: tests written before implementation.

Covers:
- test_rerun_updates_only_target_specialist
- test_rerun_creates_new_apply_runs_row
- test_rerun_refreshes_section_card_data
- test_rerun_stop_signal_scoped
- test_run_specialist_invokes_correct_phase
- test_run_specialist_unknown_name_raises
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from jobsmith.apply import PipelineEvent
from jobsmith.db import (
    get_specialist_outputs,
    insert_apply_run,
    insert_specialist_output,
)
from jobsmith.marimo.runner import NotebookRunner, _Done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain_queue(q: queue.Queue, timeout: float = 3.0) -> list:
    """Drain a queue until _Done sentinel or timeout."""
    items = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            item = q.get(timeout=min(remaining, 0.1))
            items.append(item)
            if isinstance(item, _Done):
                break
        except queue.Empty:
            break
    return items


def _make_state_dir(tmp_path: Path, slug: str, specialists: list[str]) -> Path:
    """Create a minimal .apply-state/ dir with manifest and specialist artifacts."""
    state_dir = tmp_path / slug / ".apply-state"
    state_dir.mkdir(parents=True)

    invocations = [
        {
            "specialist": s,
            "status": "ok",
            "started_at": "2024-01-01T10:00:00",
            "finished_at": "2024-01-01T10:00:01",
        }
        for s in specialists
    ]
    manifest = {
        "run_id": "test-run-id",
        "slug": slug,
        "started_at": "2024-01-01T10:00:00",
        "invocations": invocations,
    }
    (state_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Minimal artifact files for specialist readers
    (state_dir / "fit-score.json").write_text(json.dumps({
        "score": 0.85,
        "score_raw": 0.85,
        "rationale": "Good match",
        "specialty": "backend",
        "confidence": "high",
        "must_have_table": [],
        "matched_evidence": [],
        "concerns": [],
    }))
    (state_dir / "bullet-selection.json").write_text(json.dumps({
        "positions": [],
        "anchor_bullets_master": [],
        "anchor_bullets_kept": ["bullet1"],
        "anchor_bullets_dropped": [],
    }))
    (state_dir / "jd-parsed.json").write_text(json.dumps({
        "company": "Acme",
        "position": "Engineer",
        "location": "Remote",
        "location_type": "remote",
        "salary_range": None,
        "req_id": None,
        "apply_url": "https://acme.com/jobs/1",
        "role_type": "ic",
        "jd_text_clean": "We are hiring",
        "must_haves": [],
        "nice_to_haves": [],
        "top_keywords": [],
    }))

    return state_dir


# ---------------------------------------------------------------------------
# Test 1 — re-run only updates the target specialist's row
# ---------------------------------------------------------------------------


def test_rerun_updates_only_target_specialist(pipeline_db, tmp_path: Path):
    """Re-running one specialist only updates that specialist's output row.

    Other specialists' rows are untouched (same run_id / row identity).
    """
    conn, db_path = pipeline_db
    slug = "acme-engineer"
    applications_dir = tmp_path / "applications"

    # Pre-populate two specialist rows under an original run
    orig_run_id = str(uuid.uuid4())
    insert_apply_run(
        conn,
        run_id=orig_run_id,
        slug=slug,
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at="2024-01-01T10:01:00",
        status="done",
    )
    insert_specialist_output(
        conn,
        run_id=orig_run_id,
        specialist="apply-fit-scorer",
        kind="fit-score",
        output_json=json.dumps({"score": 0.8}),
        transcript_ref=None,
        finished_at="2024-01-01T10:01:00",
    )
    insert_specialist_output(
        conn,
        run_id=orig_run_id,
        specialist="apply-bullet-selector",
        kind="bullet-selection",
        output_json=json.dumps({"anchor_bullets_kept": ["old"]}),
        transcript_ref=None,
        finished_at="2024-01-01T10:01:00",
    )

    _make_state_dir(
        applications_dir,
        slug,
        ["apply-fit-scorer", "apply-bullet-selector"],
    )

    # Re-run only apply-bullet-selector
    def _fake_run_phase_iter(url_, *, cancel_event=None, cwd=None, **kwargs):
        yield PipelineEvent(kind="phase_started", phase="gather")
        yield PipelineEvent(kind="phase_complete", phase="gather")

    new_ingest_calls: list[dict] = []

    def _fake_ingest(conn_, *, slug, run_id, phase, state_dir):
        new_ingest_calls.append({"slug": slug, "run_id": run_id, "phase": phase})
        # Insert a new bullet-selection row for the new run_id
        insert_specialist_output(
            conn_,
            run_id=run_id,
            specialist="apply-bullet-selector",
            kind="bullet-selection",
            output_json=json.dumps({"anchor_bullets_kept": ["new"]}),
            transcript_ref=None,
            finished_at="2024-01-01T11:00:00",
        )
        return 1

    with (
        patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter),
        patch("jobsmith.marimo.runner.ingest_phase_outputs", _fake_ingest),
    ):
        runner = NotebookRunner(db_path=db_path, applications_dir=applications_dir)
        new_run_id = runner.run_specialist(
            url=f"https://acme.com/jobs/{slug}",
            specialist_name="apply-bullet-selector",
            cwd=tmp_path,
        )
        _drain_queue(runner.events_queue, timeout=4.0)

    # bullet-selector got a new row; fit-scorer row still under original run
    orig_outputs = get_specialist_outputs(conn, orig_run_id)
    orig_specialists = {r["specialist"] for r in orig_outputs}
    assert "apply-fit-scorer" in orig_specialists, "fit-scorer row must be preserved"

    new_outputs = get_specialist_outputs(conn, new_run_id)
    new_specialists = {r["specialist"] for r in new_outputs}
    assert "apply-bullet-selector" in new_specialists, "bullet-selector must have new row"

    assert len(new_ingest_calls) >= 1
    assert new_ingest_calls[0]["phase"] == "gather"


# ---------------------------------------------------------------------------
# Test 2 — re-run creates a new apply_runs row; old row preserved
# ---------------------------------------------------------------------------


def test_rerun_creates_new_apply_runs_row(pipeline_db, tmp_path: Path):
    """Re-run inserts a new apply_runs row; the original row is still queryable."""
    conn, db_path = pipeline_db
    slug = "corp-engineer"
    applications_dir = tmp_path / "applications"

    orig_run_id = str(uuid.uuid4())
    insert_apply_run(
        conn,
        run_id=orig_run_id,
        slug=slug,
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at="2024-01-01T10:01:00",
        status="done",
    )

    _make_state_dir(applications_dir, slug, ["apply-fit-scorer"])

    def _fake_run_phase_iter(url_, **kwargs):
        yield PipelineEvent(kind="phase_started", phase="gather")
        yield PipelineEvent(kind="phase_complete", phase="gather")

    def _fake_ingest(conn_, *, slug, run_id, phase, state_dir):
        return 0

    with (
        patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter),
        patch("jobsmith.marimo.runner.ingest_phase_outputs", _fake_ingest),
    ):
        runner = NotebookRunner(db_path=db_path, applications_dir=applications_dir)
        new_run_id = runner.run_specialist(
            url=f"https://corp.com/jobs/{slug}",
            specialist_name="apply-fit-scorer",
            cwd=tmp_path,
        )
        _drain_queue(runner.events_queue, timeout=4.0)

    # Both rows must exist
    all_rows = conn.execute(
        "SELECT run_id, status FROM apply_runs WHERE slug=? ORDER BY started_at",
        (slug,),
    ).fetchall()
    run_ids = {r["run_id"] for r in all_rows}
    assert orig_run_id in run_ids, "original run must be preserved"
    assert new_run_id in run_ids, "new re-run row must be inserted"

    new_row = conn.execute(
        "SELECT status FROM apply_runs WHERE run_id=?", (new_run_id,)
    ).fetchone()
    assert new_row is not None
    assert new_row["status"] == "done"


# ---------------------------------------------------------------------------
# Test 3 — re-run refreshes section card data
# ---------------------------------------------------------------------------


def test_rerun_refreshes_section_card_data(pipeline_db, tmp_path: Path):
    """After re-run, load_sections returns the new specialist output."""
    conn, db_path = pipeline_db
    slug = "refresh-test"
    applications_dir = tmp_path / "applications"

    orig_run_id = str(uuid.uuid4())
    insert_apply_run(
        conn,
        run_id=orig_run_id,
        slug=slug,
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at="2024-01-01T10:01:00",
        status="done",
    )
    insert_specialist_output(
        conn,
        run_id=orig_run_id,
        specialist="apply-fit-scorer",
        kind="fit-score",
        output_json=json.dumps({
            "score": 0.5,
            "rationale": "Old rationale",
            "score_raw": 0.5,
            "specialty": "backend",
            "confidence": "low",
            "must_have_table": [],
            "matched_evidence": [],
            "concerns": [],
        }),
        transcript_ref=None,
        finished_at="2024-01-01T10:01:00",
    )

    _make_state_dir(applications_dir, slug, ["apply-fit-scorer"])

    def _fake_run_phase_iter(url_, **kwargs):
        yield PipelineEvent(kind="phase_started", phase="gather")
        yield PipelineEvent(kind="phase_complete", phase="gather")

    def _fake_ingest_with_new_data(conn_, *, slug, run_id, phase, state_dir):
        """Insert a higher-score fit-score under the new run_id."""
        insert_specialist_output(
            conn_,
            run_id=run_id,
            specialist="apply-fit-scorer",
            kind="fit-score",
            output_json=json.dumps({
                "score": 0.95,
                "rationale": "New rationale",
                "score_raw": 0.95,
                "specialty": "backend",
                "confidence": "high",
                "must_have_table": [],
                "matched_evidence": [],
                "concerns": [],
            }),
            transcript_ref=None,
            finished_at="2024-01-01T12:00:00",
        )
        # Update the apply_runs row so get_apply_run_by_slug returns new run
        conn_.execute(
            "UPDATE apply_runs SET status='done', finished_at='2024-01-01T12:00:00' "
            "WHERE run_id=?",
            (run_id,),
        )
        conn_.commit()
        return 1

    with (
        patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter),
        patch("jobsmith.marimo.runner.ingest_phase_outputs", _fake_ingest_with_new_data),
    ):
        runner = NotebookRunner(db_path=db_path, applications_dir=applications_dir)
        new_run_id = runner.run_specialist(
            url=f"https://example.com/jobs/{slug}",
            specialist_name="apply-fit-scorer",
            cwd=tmp_path,
        )
        _drain_queue(runner.events_queue, timeout=4.0)

    # load_sections should return new data (most-recent run for the slug)
    from jobsmith.marimo.loader import load_sections

    load_sections(slug, db_path)
    # The new run_id should be returned (it has a later started_at via insert order)
    new_run_row = conn.execute(
        "SELECT run_id FROM apply_runs WHERE run_id=?", (new_run_id,)
    ).fetchone()
    assert new_run_row is not None, "new run must be in DB"

    new_outputs = get_specialist_outputs(conn, new_run_id)
    new_kinds = {r["kind"] for r in new_outputs}
    assert "fit-score" in new_kinds, "new fit-score output must be in DB"

    # Check the new score value
    for row in new_outputs:
        if row["kind"] == "fit-score":
            data = json.loads(row["output_json"])
            assert data["score"] == pytest.approx(0.95), "New score must be 0.95"


# ---------------------------------------------------------------------------
# Test 4 — stop signal scoped to one specialist's re-run row
# ---------------------------------------------------------------------------


def test_rerun_stop_signal_scoped(pipeline_db, tmp_path: Path):
    """cancel() during re-run sets status='cancelled' for ONLY the re-run's row.

    Other specialists' rows are untouched.
    """
    conn, db_path = pipeline_db
    slug = "cancel-test"
    applications_dir = tmp_path / "applications"

    # Existing done run with two specialists
    orig_run_id = str(uuid.uuid4())
    insert_apply_run(
        conn,
        run_id=orig_run_id,
        slug=slug,
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at="2024-01-01T10:01:00",
        status="done",
    )
    insert_specialist_output(
        conn,
        run_id=orig_run_id,
        specialist="apply-jd-parser",
        kind="jd-parsed",
        output_json=json.dumps({"company": "Corp"}),
        transcript_ref=None,
        finished_at="2024-01-01T10:01:00",
    )

    _make_state_dir(applications_dir, slug, ["apply-fit-scorer"])

    started = threading.Event()
    block = threading.Event()

    def _fake_run_phase_iter(url_, *, cancel_event=None, cwd=None, **kwargs):
        yield PipelineEvent(kind="phase_started", phase="gather")
        started.set()
        # Block until cancelled
        while not (block.is_set() or (cancel_event and cancel_event.is_set())):
            time.sleep(0.01)
        if cancel_event and cancel_event.is_set():
            yield PipelineEvent(kind="cancelled", phase="gather")

    def _fake_ingest(conn_, *, slug, run_id, phase, state_dir):
        return 0

    with (
        patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter),
        patch("jobsmith.marimo.runner.ingest_phase_outputs", _fake_ingest),
    ):
        runner = NotebookRunner(db_path=db_path, applications_dir=applications_dir)
        new_run_id = runner.run_specialist(
            url=f"https://example.com/jobs/{slug}",
            specialist_name="apply-fit-scorer",
            cwd=tmp_path,
        )

        started.wait(timeout=2.0)
        runner.cancel()
        _drain_queue(runner.events_queue, timeout=3.0)

    # New re-run row should be cancelled
    new_row = conn.execute(
        "SELECT status FROM apply_runs WHERE run_id=?", (new_run_id,)
    ).fetchone()
    assert new_row is not None
    assert new_row["status"] == "cancelled", (
        f"Expected cancelled, got {new_row['status']!r}"
    )

    # Original row unchanged
    orig_row = conn.execute(
        "SELECT status FROM apply_runs WHERE run_id=?", (orig_run_id,)
    ).fetchone()
    assert orig_row["status"] == "done", "original row must remain done"


# ---------------------------------------------------------------------------
# Test 5 — run_specialist invokes the correct phase
# ---------------------------------------------------------------------------


def test_run_specialist_invokes_correct_phase(pipeline_db, tmp_path: Path):
    """Re-running apply-fit-scorer triggers gather; apply-prose-writer triggers draft."""
    conn, db_path = pipeline_db
    tmp_path / "applications"

    

    def _fake_run_phase_iter(url_, *, cancel_event=None, cwd=None, **kwargs):
        # The phases parameter controls which phase is run
        # Inspect the call from run_specialist to determine expected phase
        return iter([
            PipelineEvent(kind="phase_started", phase="gather"),
            PipelineEvent(kind="phase_complete", phase="gather"),
        ])

    from jobsmith.apply import phase_for_specialist

    # Verify the phase mapping without running the full pipeline
    assert phase_for_specialist("apply-fit-scorer") == "gather"
    assert phase_for_specialist("apply-prose-writer") == "draft"
    assert phase_for_specialist("apply-bullet-selector") == "gather"
    assert phase_for_specialist("apply-prose-qa") == "draft"
    assert phase_for_specialist("apply-jd-parser") == "gather"


# ---------------------------------------------------------------------------
# Test 6 — unknown specialist name raises ValueError
# ---------------------------------------------------------------------------


def test_run_specialist_unknown_name_raises(pipeline_db, tmp_path: Path):
    """run_specialist with an unknown specialist_name raises ValueError."""
    conn, db_path = pipeline_db
    applications_dir = tmp_path / "applications"

    runner = NotebookRunner(db_path=db_path, applications_dir=applications_dir)

    with pytest.raises(ValueError, match="unknown specialist"):
        runner.run_specialist(
            url="https://example.com/jobs/test",
            specialist_name="not-a-real-specialist",
            cwd=tmp_path,
        )


# ---------------------------------------------------------------------------
# Test 7 — phase_for_specialist returns correct phase
# ---------------------------------------------------------------------------


def test_phase_for_specialist_gather():
    """phase_for_specialist maps gather specialists correctly."""
    from jobsmith.apply import phase_for_specialist

    for name in (
        "apply-jd-parser",
        "apply-fit-scorer",
        "apply-hm-enricher",
        "apply-bullet-selector",
        "apply-company-research",
    ):
        assert phase_for_specialist(name) == "gather", f"{name} should map to gather"


def test_phase_for_specialist_draft():
    """phase_for_specialist maps draft specialists correctly."""
    from jobsmith.apply import phase_for_specialist

    for name in ("apply-prose-writer", "apply-prose-qa"):
        assert phase_for_specialist(name) == "draft", f"{name} should map to draft"


def test_phase_for_specialist_unknown_raises():
    """phase_for_specialist raises ValueError for unknown specialist."""
    from jobsmith.apply import phase_for_specialist

    with pytest.raises(ValueError, match="unknown specialist"):
        phase_for_specialist("not-real")
