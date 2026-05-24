"""jobsmith.onboard — onboarding pipeline package (feat-19e2d594).

This package provides the ``jobsmith onboard`` command shell and the dual-entry
agentic pipeline scaffold. Slice-4 (parsers) and slice-5 (gap-interview) will
add their modules here; slice-6 (web flow) wraps the API callable exposed by
:mod:`jobsmith.onboard.pipeline`.

Public re-exports
-----------------
- :func:`~jobsmith.onboard.pipeline.run_onboard_pipeline` — API path callable
  (used by slice-6 route).
- :func:`~jobsmith.onboard.pipeline.dispatch_onboard_pipeline` — CLI path entry
  point (spawns headless subprocess via ``headless.run_phase``).
"""
from __future__ import annotations

from .pipeline import dispatch_onboard_pipeline, run_onboard_pipeline

__all__ = ["dispatch_onboard_pipeline", "run_onboard_pipeline"]
