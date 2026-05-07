"""Protocols that decouple the apply pipeline from its rendering and confirm side-effects.

The pipeline core should know nothing about ``rich``, ``click``, FastAPI's
SSE broadcaster, or pytest fakes. Those are all consumers of ``EventSink``
(receives :class:`~jobsmith.core.events.PipelineEvent`) and ``ConfirmGate``
(decides whether to proceed past an inter-phase boundary).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from jobsmith.core.events import PipelineEvent


@runtime_checkable
class EventSink(Protocol):
    """Receives :class:`PipelineEvent` instances as the pipeline emits them.

    The CLI's :class:`~jobsmith.render.ApplyRenderer` will implement this
    in slice 3 by switching on ``event.kind`` to call its existing
    ``print_header`` / ``render_event`` / ``render_phase_summary``
    helpers. The FastAPI app will implement this by broadcasting events
    to subscribers of an SSE stream.
    """

    def emit(self, event: PipelineEvent) -> None:
        """Consume a single pipeline event. Must not raise.

        Implementations should swallow their own errors — a misbehaving
        sink (broken pipe, full queue) must not abort the pipeline.
        """
        ...


@runtime_checkable
class ConfirmGate(Protocol):
    """Decides whether the pipeline proceeds past an inter-phase confirm gate.

    Replaces the in-pipeline ``click.confirm`` call so the API path can
    auto-proceed (it has no terminal user) and tests can deterministically
    short-circuit. CLI users continue to see a prompt via
    :class:`ClickConfirmGate`.
    """

    def proceed(self, *, phase_name: str, phase_num: int) -> bool:
        """Return ``True`` to advance to the next phase, ``False`` to stop.

        Returning ``False`` exits the pipeline cleanly with rc=0 (the user
        intentionally stopped); the caller does not treat this as failure.
        """
        ...
