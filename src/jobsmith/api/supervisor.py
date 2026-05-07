"""In-memory run supervisor — Slice 4 (trk-ad6d8227).

What this is
------------
A module-level singleton that registers ``core_run_apply`` in-process tasks,
tracks them in an in-memory registry, and exposes their structured events to
async consumers — primarily the SSE events endpoint (``src/jobsmith/api/events.py``).

Slice 4 change
--------------
The supervisor no longer spawns subprocesses or tails transcript files.
Instead, ``register_run`` creates a ``_RunRecord``, returns an
``_SupervisorEventSink``, and the caller wraps ``core_run_apply`` in
``asyncio.to_thread`` using that sink as the ``events`` argument.

Each :class:`~jobsmith.core.events.PipelineEvent` the pipeline emits is
converted to a :class:`TranscriptEvent` payload and broadcast to every SSE
subscriber via the shared ``_append`` mechanism.

Storage decision
----------------
SQLite remains canonical for ``apply_runs`` / ``specialist_outputs`` (the
pipeline writes those rows itself). The supervisor's registry is purely
**in-memory** — handles vanish on process restart, which is fine because
the UI re-derives state from SQLite on next load.

Lifecycle
---------
- ``register_run(run_id, slug)`` allocates the record, returns the sink.
- The caller creates an asyncio Task that runs ``core_run_apply(..., events=sink)``.
- ``set_task(run_id, task)`` wires the Task to the record so ``kill()`` can cancel it.
- ``on_run_complete(run_id, rc)`` finalises the handle and notifies subscribers.
- ``stream(run_id)`` yields buffered items first (so a late subscriber catches up),
  then live items, then exits when the run is done.

Backward-compat stubs
---------------------
``LogLine``, ``SynthPhaseEvent``, ``TranscriptEvent`` are still exported so
``events.py`` continues to compile. After Slice 5 the LogLine/SynthPhaseEvent
stubs can be removed; only ``TranscriptEvent`` is used in the new SSE path.

Public API surface
------------------
``TranscriptEvent``, ``RunHandle``, ``RunSupervisor``, ``get_supervisor``.
``LogLine`` and ``SynthPhaseEvent`` are kept as backwards-compat stubs.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from jobsmith.core.events import PipelineEvent

logger = logging.getLogger(__name__)

__all__ = [
    "LogLine",
    "SynthPhaseEvent",
    "TranscriptEvent",
    "RunHandle",
    "RunSupervisor",
    "get_supervisor",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


RunStatus = Literal["running", "done", "failed", "killed"]

# Union of items that can appear in the supervisor stream.
StreamItem = "TranscriptEvent"


@dataclass(frozen=True)
class LogLine:
    """Backward-compat stub — subprocess stdout/stderr no longer exists.

    Kept so ``events.py`` (and any other consumer) can still import and
    isinstance-check without ImportError. The supervisor no longer emits
    these after Slice 4.
    """

    stream: str
    line: str
    timestamp: str


@dataclass(frozen=True)
class SynthPhaseEvent:
    """Backward-compat stub — failure synthesis via transcript scan is gone.

    The pipeline emits ``phase_failed`` PipelineEvents directly over the
    EventSink. Kept for import compat with ``events.py``; the supervisor
    no longer creates these after Slice 4.
    """

    run_id: str
    status: str
    last_phase: str
    error_excerpt: str


@dataclass(frozen=True)
class TranscriptEvent:
    """A structured pipeline event forwarded to SSE subscribers.

    The ``payload`` dict mirrors the JSON shape written to ``apply_state_log``
    and formerly to ``transcript.jsonl`` — consumers switch on ``payload['type']``
    (or ``payload['_phase_boundary']`` for boundary markers).

    In Slice 4 this is the *only* item type the supervisor emits; the
    pipeline's ``EventSink`` converts each :class:`~jobsmith.core.events.PipelineEvent`
    to a ``TranscriptEvent`` payload before buffering.
    """

    run_id: str
    payload: dict


@dataclass
class RunHandle:
    """Public-facing snapshot of a registered run.

    Mutated in place by the supervisor as the task progresses; callers
    may read fields without locking — Python's GIL makes attribute reads
    atomic. Do **not** mutate from outside the supervisor.
    """

    run_id: str
    slug: str
    status: RunStatus
    exit_code: int | None
    started_at: str
    finished_at: str | None


# ---------------------------------------------------------------------------
# Internal per-run record
# ---------------------------------------------------------------------------


@dataclass
class _RunRecord:
    """Everything the supervisor tracks for a run."""

    handle: RunHandle
    # Buffer holds TranscriptEvent items.
    buffer: deque = field(default_factory=deque)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    finished_event: asyncio.Event = field(default_factory=asyncio.Event)
    # The asyncio Task running core_run_apply in a thread. Set after creation.
    task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO 8601 UTC timestamp with trailing 'Z'."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _pipeline_event_to_payload(event: PipelineEvent) -> dict:
    """Convert a PipelineEvent to a JSON-serialisable dict for the SSE wire."""
    base: dict = {"type": event.kind, "phase": event.phase}
    base.update(event.payload)
    return base


# ---------------------------------------------------------------------------
# EventSink implementation
# ---------------------------------------------------------------------------


class _SupervisorEventSink:
    """EventSink that broadcasts PipelineEvents to a run's SSE subscribers.

    Constructed by ``RunSupervisor.register_run`` and passed as the ``events``
    argument to ``core_run_apply``.  Each ``emit()`` call converts the
    :class:`~jobsmith.core.events.PipelineEvent` to a :class:`TranscriptEvent`
    payload and appends it to the shared buffer + subscriber queues.

    Errors here must NEVER abort the pipeline — every exception is swallowed.
    """

    def __init__(
        self,
        supervisor: "RunSupervisor",
        record: _RunRecord,
    ) -> None:
        self._supervisor = supervisor
        self._record = record

    def emit(self, event: PipelineEvent) -> None:
        try:
            payload = _pipeline_event_to_payload(event)
            te = TranscriptEvent(
                run_id=self._record.handle.run_id,
                payload=payload,
            )
            self._supervisor._append(self._record, te)
        except Exception:  # noqa: BLE001 — sinks must never abort the pipeline
            return


# ---------------------------------------------------------------------------
# AutoYesGate: ConfirmGate that always proceeds
# ---------------------------------------------------------------------------


class AutoYesGate:
    """ConfirmGate that always returns True (API path has no interactive user)."""

    def proceed(self, *, phase_name: str, phase_num: int) -> bool:
        return True


# ---------------------------------------------------------------------------
# RunSupervisor
# ---------------------------------------------------------------------------


class RunSupervisor:
    """Tracks in-process pipeline runs and broadcasts their events line-by-line.

    Thread-safety: this class is **not** thread-safe. All methods must be
    called from a single asyncio event loop (the FastAPI worker loop).
    """

    def __init__(self, *, max_buffered_lines: int = 10_000) -> None:
        if max_buffered_lines <= 0:
            raise ValueError("max_buffered_lines must be > 0")
        self._max_buffered_lines = max_buffered_lines
        self._runs: dict[str, _RunRecord] = {}
        self._active_by_slug: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_run(self, *, run_id: str, slug: str) -> _SupervisorEventSink:
        """Allocate a new run record and return its EventSink.

        The returned sink is passed as the ``events`` argument to
        ``core_run_apply``. The caller is responsible for wrapping
        ``core_run_apply`` in ``asyncio.to_thread`` and wiring the
        resulting Task via ``set_task``.

        Args:
            run_id: Caller-minted run identifier (shared with ``core_run_apply``
                via its ``run_id`` kwarg so DB rows correlate).
            slug: Application slug (used for conflict detection).

        Returns:
            An :class:`_SupervisorEventSink` that implements the
            :class:`~jobsmith.core.protocols.EventSink` protocol.
        """
        handle = RunHandle(
            run_id=run_id,
            slug=slug,
            status="running",
            exit_code=None,
            started_at=_now_iso(),
            finished_at=None,
        )
        record = _RunRecord(handle=handle)
        self._runs[run_id] = record
        self._active_by_slug[slug] = run_id
        return _SupervisorEventSink(supervisor=self, record=record)

    def set_task(self, run_id: str, task: asyncio.Task) -> None:
        """Wire the asyncio Task to the run record so kill() can cancel it."""
        record = self._runs.get(run_id)
        if record is not None:
            record.task = task

    def on_run_complete(self, run_id: str, rc: int) -> None:
        """Finalise the run handle after core_run_apply returns.

        Called from the task wrapper (``_launch_run`` wrapper in applications.py)
        once the to_thread task resolves. Marks the handle done/failed, cleans
        up the active-slug mapping, and notifies all SSE subscribers.

        Args:
            run_id: The run that completed.
            rc: Return code from ``core_run_apply`` (0 = success).
        """
        record = self._runs.get(run_id)
        if record is None:
            return
        if record.handle.status == "running":
            record.handle.exit_code = rc
            record.handle.status = "done" if rc == 0 else "failed"
            record.handle.finished_at = _now_iso()
            self._active_by_slug.pop(record.handle.slug, None)

        # Notify subscribers: end of stream.
        for q in record.subscribers:
            q.put_nowait(None)
        record.finished_event.set()

    def get(self, run_id: str) -> RunHandle | None:
        """Return the public handle for ``run_id``, or ``None`` if unknown."""
        record = self._runs.get(run_id)
        return record.handle if record is not None else None

    def get_active_for_slug(self, slug: str) -> str | None:
        """Return the run_id of the slug's active (running) run, else None."""
        run_id = self._active_by_slug.get(slug)
        if run_id is None:
            return None
        record = self._runs.get(run_id)
        if record is None or record.handle.status != "running":
            self._active_by_slug.pop(slug, None)
            return None
        return run_id

    async def stream(
        self, run_id: str
    ) -> AsyncIterator[TranscriptEvent]:
        """Yield items for ``run_id`` until the run terminates.

        Items are :class:`TranscriptEvent` (structured pipeline events).

        Behaviour:
        - Unknown run_id: returns immediately (yields nothing).
        - Known run, still running: yield buffered items, then live items,
          then return when the run finishes.
        - Known run, already finished: yield buffered items, then return.

        Subscriber disconnect: if the consumer breaks out of the loop or
        closes its generator, the subscriber queue is cleaned up — but the
        in-process task is **not** cancelled. The user navigating away must
        not abort their pipeline run.
        """
        record = self._runs.get(run_id)
        if record is None:
            return

        queue: asyncio.Queue = asyncio.Queue()
        record.subscribers.append(queue)
        try:
            snapshot = list(record.buffer)
            for item in snapshot:
                yield item

            while True:
                if record.handle.status != "running" and queue.empty():
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    return
                if item in snapshot:
                    continue
                yield item
        finally:
            with contextlib.suppress(ValueError):
                record.subscribers.remove(queue)

    async def kill(self, run_id: str, *, timeout_s: float = 5.0) -> bool:
        """Cancel the in-process task for ``run_id``.

        Returns ``True`` if we actually cancelled a running task, ``False``
        if the run was already done or unknown.

        Args:
            run_id: The run to cancel.
            timeout_s: How long to wait for the task to acknowledge cancellation.
        """
        record = self._runs.get(run_id)
        if record is None:
            return False
        if record.handle.status != "running":
            return False

        task = record.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Finalise handle.
        if record.handle.status == "running":
            record.handle.status = "killed"
            record.handle.exit_code = -1
            record.handle.finished_at = _now_iso()
            self._active_by_slug.pop(record.handle.slug, None)

        for q in record.subscribers:
            q.put_nowait(None)
        record.finished_event.set()
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append(
        self, record: _RunRecord, item: TranscriptEvent
    ) -> None:
        """Append ``item`` to the buffer (capped) and broadcast to queues."""
        buf = record.buffer
        buf.append(item)
        while len(buf) > self._max_buffered_lines:
            buf.popleft()
        for q in record.subscribers:
            q.put_nowait(item)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_default_supervisor = RunSupervisor()


def get_supervisor() -> RunSupervisor:
    """Return the process-wide default supervisor."""
    return _default_supervisor
