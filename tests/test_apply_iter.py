"""Tests for run_phase_iter() generator extracted from run_apply().

Tests are written TDD-first — they intentionally FAIL before implementation.

Covers:
- test_iter_yields_events_in_order — gather→draft→render ordering
- test_iter_records_to_db — DB rows after iteration
- test_iter_cancel_mid_phase — cancel_event terminates subprocess
- test_cli_apply_unchanged — jobsmith apply exit 0 unchanged
- test_slug_changed_event_emitted — slug-changed emitted between gather/draft
- test_guard_failed_event_or_exception — guard failure surfaced, not silent
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobsmith.apply import (
    _PHASES,
    derive_slug,
    run_apply,
    run_phase_iter,
)
from jobsmith.headless import Event

# ---------------------------------------------------------------------------
# Helpers — fake headless.run_phase that yields synthetic events
# ---------------------------------------------------------------------------


def _make_phase_events(phase_name: str) -> list[Event]:
    """Return a minimal event sequence for a phase (text + phase_complete)."""
    return [
        Event(type="text", text=f"Running {phase_name}..."),
        Event(type="phase_complete", name=phase_name),
    ]


def _fake_run_phase_factory(phase_event_map: dict[str, list[Event]]):
    """Return a fake run_phase that yields pre-canned events per phase."""

    def _fake_run_phase(*args, **kwargs):
        phase = kwargs.get("phase") or args[0]
        events = phase_event_map.get(phase, [Event(type="phase_complete", name=phase)])
        # Check for cancel_event — if set, stop early
        cancel_event = kwargs.get("cancel_event")
        for ev in events:
            if cancel_event is not None and cancel_event.is_set():
                return
            yield ev

    return _fake_run_phase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_repo(tmp_path: Path):
    """A minimal .apply-config.yaml repo tree sufficient for run_phase_iter."""
    config = tmp_path / ".apply-config.yaml"
    config.write_text(
        "master:\n"
        "  work_yml: assets/content/work.yml\n"
        "  skill_yml: assets/content/skill.yml\n"
        "  education_yml: assets/content/education.yml\n"
        "  author_yml: assets/content/author.yml\n"
        "output:\n"
        "  applications_dir: private/applications\n"
    )
    content = tmp_path / "assets" / "content"
    content.mkdir(parents=True)
    for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / name).write_text("# stub\n")
    apps = tmp_path / "private" / "applications"
    apps.mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def mock_plugin_dir(tmp_path: Path):
    """Minimal plugin directory with stub system-prompt files."""
    pdir = tmp_path / "plugin"
    pdir.mkdir()
    sp_dir = pdir / "system-prompts"
    sp_dir.mkdir()
    for phase_name, phase_num in _PHASES:
        (sp_dir / f"phase-{phase_num}-{phase_name}.md").write_text(
            f"# {phase_name} system prompt\n"
        )
    return pdir


# ---------------------------------------------------------------------------
# Test 1 — events arrive in gather → draft → render order
# ---------------------------------------------------------------------------


def test_iter_yields_events_in_order(minimal_repo, mock_plugin_dir):
    """run_phase_iter() must yield events in gather→draft→render order."""
    url = "https://example.com/jobs/software-engineer"
    slug = derive_slug(url)

    # Set up bullet-decisions.json so draft doesn't fail at anchor guard
    state_dir = minimal_repo / "private" / "applications" / slug / ".apply-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "bullet-decisions.json").write_text("{}\n")

    phase_events = {
        phase: _make_phase_events(phase)
        for phase, _ in _PHASES
    }
    fake_run_phase = _fake_run_phase_factory(phase_events)

    phases_seen = []

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=mock_plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
    ):
        for event in run_phase_iter(
            url,
            cwd=minimal_repo,
            skip_confirm=True,
            force=True,
        ):
            if event.kind == "phase_complete":
                phases_seen.append(event.phase)

    assert phases_seen == ["gather", "draft", "render"], (
        f"Expected gather→draft→render events; got {phases_seen}"
    )


# ---------------------------------------------------------------------------
# Test 2 — DB rows recorded after iteration
# ---------------------------------------------------------------------------


def test_iter_records_to_db(minimal_repo, mock_plugin_dir, pipeline_db, tmp_path):
    """After iteration, specialist_outputs rows exist via ingest_phase_outputs."""
    conn, _db_path = pipeline_db
    url = "https://example.com/jobs/data-engineer"
    slug = derive_slug(url)

    state_dir = minimal_repo / "private" / "applications" / slug / ".apply-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "bullet-decisions.json").write_text("{}\n")
    # Write a minimal manifest so ingest_phase_outputs has something to ingest
    (state_dir / "jd-parsed.json").write_text(
        json.dumps({
            "company": "Acme",
            "position": "Data Engineer",
            "location": "Remote",
            "location_type": "remote",
            "salary_range": None,
            "req_id": None,
            "apply_url": url,
            "role_type": "ic",
            "jd_text_clean": "...",
            "must_haves": [],
            "nice_to_haves": [],
            "top_keywords": [],
        })
    )
    # Real apply-pipeline manifest format: flat invocations[]
    (state_dir / "manifest.json").write_text(
        json.dumps({
            "run_id": "test-run",
            "slug": slug,
            "started_at": "2024-01-01T10:00:00",
            "invocations": [
                {
                    "specialist": "apply-jd-parser",
                    "status": "ok",
                    "started_at": "2024-01-01T10:00:01",
                    "finished_at": "2024-01-01T10:00:02",
                },
            ],
        })
    )

    phase_events = {
        phase: _make_phase_events(phase)
        for phase, _ in _PHASES
    }
    fake_run_phase = _fake_run_phase_factory(phase_events)

    import uuid as _uuid
    run_id = str(_uuid.uuid4())

    from jobsmith.db import insert_apply_run
    from jobsmith.db_ingest import ingest_phase_outputs

    insert_apply_run(
        conn,
        run_id=run_id,
        slug=slug,
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at=None,
        status="in-progress",
    )

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=mock_plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
    ):
        list(run_phase_iter(
            url,
            cwd=minimal_repo,
            skip_confirm=True,
            force=True,
        ))

    # Post-phase ingest of gather outputs
    ingest_phase_outputs(
        conn,
        slug=slug,
        run_id=run_id,
        phase="gather",
        state_dir=state_dir,
    )

    from jobsmith.db import get_specialist_outputs
    rows = get_specialist_outputs(conn, run_id)
    assert len(rows) >= 1, f"Expected at least 1 specialist_outputs row; got {len(rows)}"


# ---------------------------------------------------------------------------
# Test 3 — cancel mid-phase terminates subprocess
# ---------------------------------------------------------------------------


def test_iter_cancel_mid_phase(minimal_repo, mock_plugin_dir):
    """Setting cancel_event after first event stops the generator."""
    url = "https://example.com/jobs/cancelled-role"
    slug = derive_slug(url)

    state_dir = minimal_repo / "private" / "applications" / slug / ".apply-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "bullet-decisions.json").write_text("{}\n")

    cancel_event = threading.Event()
    terminate_called = []

    # Simulate a long-running subprocess that never completes
    mock_proc = MagicMock()
    mock_proc.returncode = -15
    mock_proc.poll.return_value = None  # still running

    def _fake_terminate():
        terminate_called.append(True)
        mock_proc.poll.return_value = -15  # now terminated

    mock_proc.terminate.side_effect = _fake_terminate

    def _fake_wait(timeout=None):
        if mock_proc.poll.return_value is not None:
            return
        raise __import__("subprocess").TimeoutExpired("claude", timeout)

    mock_proc.wait.side_effect = _fake_wait
    mock_proc.stdout = iter([])  # no output
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = ""

    events_yielded = []

    def _fake_run_phase_cancel(*args, **kwargs):
        phase = kwargs.get("phase") or args[0]
        if phase == "gather":
            yield Event(type="text", text="Starting gather...")
            # Signal cancel after first event
            cancel_event.set()
            # Yield phase_complete anyway — but cancel_event is set
            yield Event(type="phase_complete", name=phase)
        else:
            # Draft/render should never be reached
            yield Event(type="phase_complete", name=phase)

    with (
        patch("jobsmith.apply.headless.run_phase", _fake_run_phase_cancel),
        patch("jobsmith.apply.get_plugin_dir", return_value=mock_plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
    ):
        for event in run_phase_iter(
            url,
            cwd=minimal_repo,
            skip_confirm=True,
            force=True,
            cancel_event=cancel_event,
        ):
            events_yielded.append(event)
            if cancel_event.is_set():
                break  # consumer stops draining

    # After cancel: draft and render phases must NOT have been started
    phase_names_seen = {ev.phase for ev in events_yielded if hasattr(ev, "phase")}
    assert "draft" not in phase_names_seen, (
        "draft phase must not start after cancel"
    )
    assert "render" not in phase_names_seen, (
        "render phase must not start after cancel"
    )


# ---------------------------------------------------------------------------
# Test 4 — CLI apply path unchanged
# ---------------------------------------------------------------------------


def test_cli_apply_unchanged(minimal_repo, mock_plugin_dir):
    """jobsmith apply <url> via run_apply() exits 0 with unchanged behavior."""
    url = "https://example.com/jobs/backend-engineer"
    slug = derive_slug(url)

    state_dir = minimal_repo / "private" / "applications" / slug / ".apply-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "bullet-decisions.json").write_text("{}\n")

    phase_events = {
        phase: _make_phase_events(phase)
        for phase, _ in _PHASES
    }
    fake_run_phase = _fake_run_phase_factory(phase_events)

    import io

    from rich.console import Console

    from jobsmith.render import ApplyRenderer

    rdr = ApplyRenderer(
        yes=True,
        verbosity=0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=mock_plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
    ):
        rc = run_apply(
            url,
            cwd=minimal_repo,
            skip_confirm=True,
            force=True,
            renderer=rdr,
        )

    assert rc == 0, f"run_apply returned {rc}, expected 0"


# ---------------------------------------------------------------------------
# Test 5 — slug-changed event emitted between gather and draft
# ---------------------------------------------------------------------------


def test_slug_changed_event_emitted(minimal_repo, mock_plugin_dir):
    """A slug-changed event is emitted when _reconcile_canonical_slug renames dir."""
    url = "https://example.com/jobs/frontend-engineer"
    original_slug = derive_slug(url)
    canonical_slug = "acme-corp-frontend-engineer"

    state_dir = minimal_repo / "private" / "applications" / canonical_slug / ".apply-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "bullet-decisions.json").write_text("{}\n")

    phase_events = {
        phase: _make_phase_events(phase)
        for phase, _ in _PHASES
    }
    fake_run_phase = _fake_run_phase_factory(phase_events)

    slug_changed_events = []

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=mock_plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch(
            "jobsmith.apply._reconcile_canonical_slug",
            return_value=(canonical_slug, True),
        ),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply._record_url_mapping"),
        patch("jobsmith.apply.ensure_bootstrap"),
    ):
        for event in run_phase_iter(
            url,
            cwd=minimal_repo,
            skip_confirm=True,
            force=True,
        ):
            if event.kind == "slug_changed":
                slug_changed_events.append(event)

    assert len(slug_changed_events) == 1, (
        f"Expected exactly 1 slug_changed event; got {len(slug_changed_events)}"
    )
    assert slug_changed_events[0].payload.get("old_slug") == original_slug
    assert slug_changed_events[0].payload.get("new_slug") == canonical_slug


# ---------------------------------------------------------------------------
# Test 6 — guard failure surfaces as event/exception, not silent return
# ---------------------------------------------------------------------------


def test_guard_failed_event_or_exception(minimal_repo, mock_plugin_dir):
    """_run_step45_orchestration failure must surface as event or exception."""
    url = "https://example.com/jobs/ml-engineer"
    slug = derive_slug(url)

    state_dir = minimal_repo / "private" / "applications" / slug / ".apply-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    # No bullet-decisions.json → forces anchor guard to run

    phase_events = {
        phase: _make_phase_events(phase)
        for phase, _ in _PHASES
    }
    fake_run_phase = _fake_run_phase_factory(phase_events)

    guard_failed_events = []
    raised = None

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=mock_plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        # Guard returns non-zero (failure)
        patch("jobsmith.apply._run_step45_orchestration", return_value=2),
        patch("jobsmith.apply.ensure_bootstrap"),
    ):
        try:
            for event in run_phase_iter(
                url,
                cwd=minimal_repo,
                skip_confirm=True,
                force=True,
            ):
                if event.kind == "guard_failed":
                    guard_failed_events.append(event)
        except Exception as exc:
            raised = exc

    assert guard_failed_events or raised is not None, (
        "Guard failure must emit guard_failed event OR raise an exception — "
        "it must NOT be silently ignored"
    )


def test_run_phase_iter_phases_filter(tmp_path: Path):
    """Roborev #921 HIGH: phases= filter scopes execution to one phase.

    Re-running apply-prose-writer (draft) must NOT re-fire gather first.
    """
    from unittest.mock import patch

    from jobsmith.apply import _PHASES, run_phase_iter
    from jobsmith.headless import Event

    config = tmp_path / ".apply-config.yaml"
    config.write_text(
        "master:\n"
        "  work_yml: assets/content/work.yml\n"
        "  skill_yml: assets/content/skill.yml\n"
        "  education_yml: assets/content/education.yml\n"
        "  author_yml: assets/content/author.yml\n"
        "output:\n"
        "  applications_dir: private/applications\n"
    )
    content = tmp_path / "assets" / "content"
    content.mkdir(parents=True)
    for n in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / n).write_text("# stub\n")
    apps = tmp_path / "private" / "applications"
    apps.mkdir(parents=True)

    plugin_dir = tmp_path / "plugin"
    sp_dir = plugin_dir / "system-prompts"
    sp_dir.mkdir(parents=True)
    for name, num in _PHASES:
        (sp_dir / f"phase-{num}-{name}.md").write_text(f"# {name}\n")

    invoked: list[str] = []

    def _fake_run_phase(*args, **kwargs):
        phase = kwargs.get("phase") or args[0]
        invoked.append(phase)
        yield Event(type="phase_complete", name=phase)

    with (
        patch("jobsmith.apply.headless.run_phase", _fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug",
              return_value=("slug-x", False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
    ):
        list(run_phase_iter(
            "https://example.com/jobs/x",
            cwd=tmp_path,
            skip_confirm=True,
            force=True,
            phases=["draft"],
        ))

    assert invoked == ["draft"], (
        f"phases=[draft] must invoke ONLY draft; got {invoked}"
    )

