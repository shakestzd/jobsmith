"""Centralized apply-pipeline core (trk-ad6d8227).

This package collects the business logic that previously lived inside
``jobsmith.apply`` so that both the FastAPI app (``jobsmith.api``) and the
Typer CLI (``jobsmith.cli``) can drive the pipeline through the same
in-process function calls.

Public surface (built up over slices 1-6):
    PipelineEvent  — phase-granular event dataclass
    EventSink      — protocol for receiving pipeline events
    ConfirmGate    — protocol for inter-phase confirm gates
    NullEventSink  — drops events on the floor (tests, --silent runs)
    CallbackEventSink — adapter wrapping a plain callable
    AutoYesGate    — always proceeds (FastAPI default)
    ClickConfirmGate — wraps ``click.confirm`` for terminal users

Slices 2-5 will land slug/path/manifest helpers and the ``run_apply``
entrypoint into this package; until then ``jobsmith.apply`` re-exports
remain the canonical import sites.
"""
from __future__ import annotations

from jobsmith.core import paths, session, slug  # noqa: F401
from jobsmith.core.confirm import AutoYesGate, ClickConfirmGate
from jobsmith.core.events import PipelineEvent
from jobsmith.core.protocols import ConfirmGate, EventSink
from jobsmith.core.sinks import CallbackEventSink, NullEventSink

__all__ = [
    "AutoYesGate",
    "CallbackEventSink",
    "ClickConfirmGate",
    "ConfirmGate",
    "EventSink",
    "NullEventSink",
    "PipelineEvent",
]
