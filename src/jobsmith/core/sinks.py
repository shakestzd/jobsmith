"""Reference :class:`~jobsmith.core.protocols.EventSink` implementations.

- :class:`NullEventSink` drops every event (tests, ``--silent`` runs).
- :class:`CallbackEventSink` wraps a plain callable so callers can write
  small adapters without subclassing.

The ``ApplyRenderer`` adapter (slice 3) and FastAPI SSE broadcaster
(slice 4) are the production sinks; both implement the same protocol.
"""
from __future__ import annotations

from collections.abc import Callable

from jobsmith.core.events import PipelineEvent


class NullEventSink:
    """Discards every event. Useful as a default for tests and for code
    paths that do not need to observe pipeline progress.
    """

    def emit(self, event: PipelineEvent) -> None:  # noqa: D401 — protocol impl
        """Drop *event* silently."""
        return None


class CallbackEventSink:
    """Forwards every event to a single callable.

    The callable signature is ``callback(event: PipelineEvent) -> None``.
    Exceptions raised by the callback are intentionally swallowed so a
    broken sink never aborts the pipeline.
    """

    def __init__(self, callback: Callable[[PipelineEvent], None]) -> None:
        self._callback = callback

    def emit(self, event: PipelineEvent) -> None:  # noqa: D401 — protocol impl
        try:
            self._callback(event)
        except Exception:  # noqa: BLE001 — sinks must never abort the pipeline.
            return None
