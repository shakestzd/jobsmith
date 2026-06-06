"""Tests for jobsmith.core.pipeline — Slice 3b."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jobsmith import apply as apply_mod
from jobsmith.core import pipeline as core_pipeline


def test_run_phase_iter_importable_from_core():
    assert callable(core_pipeline.run_phase_iter)


def test_apply_re_export_is_same_object():
    """jobsmith.apply.run_phase_iter must be SAME object as core.pipeline.run_phase_iter
    so monkeypatch / isinstance checks across the boundary keep working."""
    assert apply_mod.run_phase_iter is core_pipeline.run_phase_iter


def test_run_phase_iter_accepts_event_sink_param():
    """The new signature accepts events: EventSink instead of (or alongside) rdr."""
    import inspect
    sig = inspect.signature(core_pipeline.run_phase_iter)
    # Must accept either 'events' or 'sink' parameter
    params = list(sig.parameters.keys())
    assert any(p in {"events", "sink"} for p in params), f"params={params}"


# ---------------------------------------------------------------------------
# Finding #1 — BackstopError must propagate from _run_backstop_gate
# ---------------------------------------------------------------------------


class TestBackstopGatePropagatesError:
    """BackstopError raised by run_backstop must NOT be caught by _run_backstop_gate.

    The outer phase-loop handler in run_phase_iter catches it and emits
    phase_failed.  Swallowing it here would let ungated output ship.
    """

    def _make_state_dir(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create minimal app state dir with prose-draft.md."""
        apps_dir = tmp_path / "applications"
        slug = "test-co-engineer-2026-01"
        state_dir = apps_dir / slug / ".apply-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "prose-draft.md").write_text("resume text", encoding="utf-8")
        return apps_dir, state_dir

    def test_backstop_error_propagates(self, tmp_path: Path) -> None:
        """When run_backstop raises BackstopError, _run_backstop_gate re-raises it."""
        from jobsmith.core.pipeline import _run_backstop_gate
        from jobsmith.reuse.backstop import BackstopError

        apps_dir, state_dir = self._make_state_dir(tmp_path)
        slug = "test-co-engineer-2026-01"

        with (
            patch("jobsmith.config.find_config", return_value=None),
            patch("jobsmith.core.pipeline.applications_dir", return_value=apps_dir),
            patch("jobsmith.core.pipeline.apply_state_dir", return_value=state_dir),
            patch(
                "jobsmith.reuse.backstop.run_backstop",
                side_effect=BackstopError("gate still failing after 3 regen(s)"),
            ),
            pytest.raises(BackstopError, match="gate still failing"),
        ):
            _run_backstop_gate(slug, tmp_path)

    def test_non_backstop_infra_error_still_raises(self, tmp_path: Path) -> None:
        """Non-BackstopError infrastructure errors (e.g. RuntimeError) also propagate now
        that the blanket except was removed.  Callers (phase loop) handle them."""
        from jobsmith.core.pipeline import _run_backstop_gate

        apps_dir, state_dir = self._make_state_dir(tmp_path)
        slug = "test-co-engineer-2026-01"

        with (
            patch("jobsmith.config.find_config", return_value=None),
            patch("jobsmith.core.pipeline.applications_dir", return_value=apps_dir),
            patch("jobsmith.core.pipeline.apply_state_dir", return_value=state_dir),
            patch(
                "jobsmith.reuse.backstop.run_backstop",
                side_effect=RuntimeError("unexpected infra error"),
            ),
            pytest.raises(RuntimeError, match="unexpected infra error"),
        ):
            _run_backstop_gate(slug, tmp_path)
