"""TDD test: uncaught exceptions inside the apply pipeline emit a terminal
phase_failed event (bug-84db2d3c / GitHub #61).

Repro: when ``headless.run_phase`` raises an exception mid-gather the
generator used to propagate the exception bare — no terminal event was
emitted. The SSE transcript ended without an error marker.

After the fix, ``run_phase_iter`` must:
- catch the exception inside the per-phase loop
- yield ``PipelineEvent(kind='phase_failed', phase='gather',
    payload={"error": "RuntimeError: synthetic"})``
- then return (not re-raise)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jobsmith.apply import (
    _PHASES,
    PipelineEvent,
    derive_slug,
    run_phase_iter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_repo(tmp_path: Path) -> Path:
    """Minimal .apply-config.yaml repo tree."""
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
def mock_plugin_dir(tmp_path: Path) -> Path:
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
# Test
# ---------------------------------------------------------------------------


def test_exception_in_run_phase_emits_phase_failed_event(
    minimal_repo: Path, mock_plugin_dir: Path
) -> None:
    """An exception raised by headless.run_phase must be caught and surfaced
    as a PipelineEvent(kind='phase_failed') — not silently swallowed or
    propagated bare.

    This test FAILS before the fix (exception propagates out of the
    generator without emitting any terminal event).
    """
    url = "https://example.com/jobs/engineer"
    slug = derive_slug(url)

    # Gather phase: run_phase raises a RuntimeError mid-execution.
    def _exploding_run_phase(*args, **kwargs):
        raise RuntimeError("synthetic")
        yield  # make it a generator (never reached)

    collected: list[PipelineEvent] = []
    with (
        patch("jobsmith.apply.headless.run_phase", _exploding_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=mock_plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
        patch("jobsmith.apply._auto_freeze_contracts"),
    ):
        # Must NOT raise — exception must be caught inside the generator.
        for event in run_phase_iter(url, cwd=minimal_repo, force=True):
            collected.append(event)

    assert collected, "run_phase_iter yielded no events at all"

    last = collected[-1]
    assert last.kind == "phase_failed", (
        f"Expected last event kind='phase_failed', got {last.kind!r}. "
        f"All events: {[e.kind for e in collected]}"
    )
    error_text = (last.payload or {}).get("error", "")
    assert "synthetic" in error_text, (
        f"Expected 'synthetic' in error field, got {error_text!r}"
    )
