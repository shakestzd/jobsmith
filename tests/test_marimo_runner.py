"""Tests for NotebookRunner — the threading.Thread + queue.Queue bridge.

TDD-first: these tests are written before the implementation and verify
the public API of jobsmith.marimo.runner.NotebookRunner.

Covers:
- test_run_mode_inserts_apply_run_row
- test_progress_events_consumed_in_order
- test_stop_signal_sets_cancelled
- test_flip_to_review_on_completion
- test_runner_ingests_specialist_outputs_on_phase_complete
- test_slug_changed_event_propagates
- test_re_entry_guard_blocks_double_start
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from jobsmith.apply import PipelineEvent
from jobsmith.marimo.runner import NotebookRunner, _Done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain_queue(q: queue.Queue, timeout: float = 2.0) -> list:
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


def _make_phase_events(phase: str) -> list[PipelineEvent]:
    return [
        PipelineEvent(kind="phase_started", phase=phase),
        PipelineEvent(kind="phase_complete", phase=phase),
    ]


# ---------------------------------------------------------------------------
# Test 1 — DB row created with status=running then status=done
# ---------------------------------------------------------------------------


def test_run_mode_inserts_apply_run_row(pipeline_db, tmp_path: Path):
    """Runner inserts apply_runs row (running → done) around the pipeline."""
    conn, db_path = pipeline_db
    url = "https://example.com/jobs/software-engineer"

    def _fake_run_phase_iter(url_, **kwargs):
        for phase in ("gather", "draft", "render"):
            yield PipelineEvent(kind="phase_started", phase=phase)
            yield PipelineEvent(kind="phase_complete", phase=phase)

    with patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter):
        runner = NotebookRunner(db_path=db_path, applications_dir=tmp_path)
        run_id = runner.start(url=url, cwd=tmp_path)
        items = _drain_queue(runner.events_queue, timeout=3.0)

    done = next((i for i in items if isinstance(i, _Done)), None)
    assert done is not None, "Expected _Done sentinel on queue"
    assert done.status == "done"

    from jobsmith.apply import derive_slug
    from jobsmith.db import get_apply_run_by_slug
    slug = derive_slug(url)
    row = get_apply_run_by_slug(conn, slug)
    assert row is not None, "apply_runs row must exist"
    assert row["status"] == "done", f"Expected status=done, got {row['status']}"
    assert row["run_id"] == run_id


# ---------------------------------------------------------------------------
# Test 2 — events arrive in order: phase_started, phase_complete per phase
# ---------------------------------------------------------------------------


def test_progress_events_consumed_in_order(pipeline_db, tmp_path: Path):
    """Events arrive in gather→draft→render order on the queue."""
    conn, db_path = pipeline_db
    url = "https://example.com/jobs/data-engineer"

    phases_order = ["gather", "draft", "render"]

    def _fake_run_phase_iter(url_, **kwargs):
        for phase in phases_order:
            yield PipelineEvent(kind="phase_started", phase=phase)
            yield PipelineEvent(kind="phase_complete", phase=phase)

    with patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter):
        runner = NotebookRunner(db_path=db_path, applications_dir=tmp_path)
        runner.start(url=url, cwd=tmp_path)
        items = _drain_queue(runner.events_queue, timeout=3.0)

    pipeline_events = [i for i in items if isinstance(i, PipelineEvent)]
    phase_complete_events = [e for e in pipeline_events if e.kind == "phase_complete"]
    phases_seen = [e.phase for e in phase_complete_events]

    assert phases_seen == phases_order, (
        f"Expected {phases_order}, got {phases_seen}"
    )


# ---------------------------------------------------------------------------
# Test 3 — stop signal sets cancelled status
# ---------------------------------------------------------------------------


def test_stop_signal_sets_cancelled(pipeline_db, tmp_path: Path):
    """cancel() sets threading.Event and DB status becomes cancelled."""
    conn, db_path = pipeline_db
    url = "https://example.com/jobs/cancelled-role"
    started = threading.Event()
    block = threading.Event()

    def _fake_run_phase_iter(url_, *, cancel_event=None, cwd=None, **kwargs):
        yield PipelineEvent(kind="phase_started", phase="gather")
        started.set()
        # Block until cancelled or released
        while not (block.is_set() or (cancel_event and cancel_event.is_set())):
            time.sleep(0.01)
        if cancel_event and cancel_event.is_set():
            yield PipelineEvent(kind="cancelled", phase="gather")

    with patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter):
        runner = NotebookRunner(db_path=db_path, applications_dir=tmp_path)
        runner.start(url=url, cwd=tmp_path)

        # Wait until gather starts
        started.wait(timeout=2.0)
        runner.cancel()
        items = _drain_queue(runner.events_queue, timeout=3.0)

    done = next((i for i in items if isinstance(i, _Done)), None)
    assert done is not None, "Expected _Done sentinel"
    assert done.status == "cancelled", f"Expected cancelled, got {done.status!r}"

    from jobsmith.apply import derive_slug
    from jobsmith.db import get_apply_run_by_slug
    slug = derive_slug(url)
    row = get_apply_run_by_slug(conn, slug)
    assert row is not None
    assert row["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Test 4 — flip to review: is_running() → False after completion
# ---------------------------------------------------------------------------


def test_flip_to_review_on_completion(pipeline_db, tmp_path: Path):
    """After runner thread exits, is_running() returns False and DB row is done."""
    conn, db_path = pipeline_db
    url = "https://example.com/jobs/review-role"

    def _fake_run_phase_iter(url_, **kwargs):
        yield PipelineEvent(kind="phase_started", phase="gather")
        yield PipelineEvent(kind="phase_complete", phase="gather")

    with patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter):
        runner = NotebookRunner(db_path=db_path, applications_dir=tmp_path)
        runner.start(url=url, cwd=tmp_path)
        _drain_queue(runner.events_queue, timeout=3.0)

    assert not runner.is_running(), "Runner should not be running after completion"

    from jobsmith.apply import derive_slug
    from jobsmith.db import get_apply_run_by_slug
    slug = derive_slug(url)
    row = get_apply_run_by_slug(conn, slug)
    assert row is not None
    assert row["status"] == "done"


# ---------------------------------------------------------------------------
# Test 5 — ingest_phase_outputs called after phase_complete
# ---------------------------------------------------------------------------


def test_runner_ingests_specialist_outputs_on_phase_complete(
    pipeline_db, tmp_path: Path, fixture_state_dir: Path
):
    """Runner calls ingest_phase_outputs after each phase_complete event."""
    conn, db_path = pipeline_db
    url = "https://example.com/jobs/ingest-test"

    def _fake_run_phase_iter(url_, **kwargs):
        yield PipelineEvent(kind="phase_started", phase="gather")
        yield PipelineEvent(kind="phase_complete", phase="gather")

    ingest_calls = []

    def _fake_ingest(conn_, *, slug, run_id, phase, state_dir):
        ingest_calls.append({"slug": slug, "phase": phase})
        return 0

    with (
        patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter),
        patch("jobsmith.marimo.runner.ingest_phase_outputs", _fake_ingest),
    ):
        runner = NotebookRunner(db_path=db_path, applications_dir=tmp_path)
        runner.start(url=url, cwd=tmp_path)
        _drain_queue(runner.events_queue, timeout=3.0)

    assert len(ingest_calls) >= 1, "ingest_phase_outputs must be called at least once"
    assert ingest_calls[0]["phase"] == "gather"


# ---------------------------------------------------------------------------
# Test 6 — slug_changed event propagates to consumer queue
# ---------------------------------------------------------------------------


def test_slug_changed_event_propagates(pipeline_db, tmp_path: Path):
    """slug_changed events from run_phase_iter are forwarded to the queue."""
    conn, db_path = pipeline_db
    url = "https://example.com/jobs/rename-test"
    old_slug = "rename-test"
    new_slug = "acme-corp-rename-test"

    def _fake_run_phase_iter(url_, **kwargs):
        yield PipelineEvent(kind="phase_started", phase="gather")
        yield PipelineEvent(
            kind="slug_changed",
            phase="gather",
            payload={"old_slug": old_slug, "new_slug": new_slug},
        )
        yield PipelineEvent(kind="phase_complete", phase="gather")

    with patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter):
        runner = NotebookRunner(db_path=db_path, applications_dir=tmp_path)
        runner.start(url=url, cwd=tmp_path)
        items = _drain_queue(runner.events_queue, timeout=3.0)

    slug_changed = [
        i for i in items
        if isinstance(i, PipelineEvent) and i.kind == "slug_changed"
    ]
    assert len(slug_changed) == 1, "Expected exactly 1 slug_changed event on queue"
    assert slug_changed[0].payload["new_slug"] == new_slug


# ---------------------------------------------------------------------------
# Test 7 — re-entry guard blocks double start
# ---------------------------------------------------------------------------


def test_re_entry_guard_blocks_double_start(pipeline_db, tmp_path: Path):
    """Calling start() twice raises RuntimeError or no-ops when already running."""
    conn, db_path = pipeline_db
    url = "https://example.com/jobs/double-start"
    block = threading.Event()

    def _fake_run_phase_iter(url_, **kwargs):
        yield PipelineEvent(kind="phase_started", phase="gather")
        block.wait(timeout=5.0)
        yield PipelineEvent(kind="phase_complete", phase="gather")

    with patch("jobsmith.marimo.runner.run_phase_iter", _fake_run_phase_iter):
        runner = NotebookRunner(db_path=db_path, applications_dir=tmp_path)
        runner.start(url=url, cwd=tmp_path)

        # Wait until first phase starts (runner is now running)
        time.sleep(0.05)

        with pytest.raises(RuntimeError, match="already running"):
            runner.start(url=url, cwd=tmp_path)

    block.set()  # unblock thread
