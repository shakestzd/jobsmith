"""In-memory subprocess supervisor (feat-cf348e05).

What this is
------------
A module-level singleton that spawns ``jobsmith apply`` (or any other)
subprocesses, tracks them in an in-memory registry, and exposes their
stdout/stderr line-by-line to async consumers — primarily the SSE events
endpoint (``src/jobsmith/api/events.py``).

This is the **foundation slice** for the "0.7 Run pipeline from UI" track.
Three downstream slices depend on it:

1. ``POST /api/applications`` (create slug + initial DB row)
2. ``POST /api/applications/{slug}/run`` (calls ``supervisor.start``)
3. Frontend ``PipelineTab`` wiring (consumes ``event: log`` SSE frames)

Storage decision
----------------
SQLite remains canonical for ``apply_runs`` / ``specialist_outputs`` (the
pipeline writes those rows itself). The supervisor's registry is purely
**in-memory** — handles vanish on process restart, which is fine because
the UI re-derives state from SQLite on next load.

Lifecycle (important for the SSE consumer)
------------------------------------------
- ``start()`` spawns the subprocess and immediately returns the ``run_id``.
- Two background tasks drain stdout/stderr into a per-run ``deque`` capped
  at ``max_buffered_lines``. Each appended line is also pushed to every
  registered subscriber queue so live consumers see it without polling.
- ``stream(run_id)`` yields **buffered lines first** (so a late subscriber
  catches up), then live lines, then exits cleanly when the process has
  terminated and no more lines remain.
- The subprocess is **never killed** by a subscriber disconnect — when the
  SSE client drops, only its subscriber queue is removed. The next
  reconnect picks up from the buffer.

Verbosity / filtering
---------------------
The supervisor itself does no verbosity filtering — it streams every line.
The SSE layer applies the user's ``verbosity`` query parameter when
deciding whether to forward each ``log`` event to the wire.

Public API surface
------------------
``LogLine``, ``RunHandle``, ``RunSupervisor``, ``get_supervisor``.

The default singleton is constructed with ``max_buffered_lines=10_000``.
Tests construct their own ``RunSupervisor(max_buffered_lines=...)`` to
exercise the cap without spawning a million-line stub.
"""
from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

__all__ = [
    "LogLine",
    "RunHandle",
    "RunSupervisor",
    "get_supervisor",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


StreamName = Literal["stdout", "stderr"]
RunStatus = Literal["running", "done", "failed", "killed"]


@dataclass(frozen=True)
class LogLine:
    """A single line of captured subprocess output.

    ``timestamp`` is captured at the moment the line is appended to the
    buffer (i.e. when the supervisor reads it from the OS pipe), in ISO
    8601 with a trailing ``Z`` for UTC.
    """

    stream: StreamName
    line: str
    timestamp: str


@dataclass
class RunHandle:
    """Public-facing snapshot of a registered run.

    Mutated in place by the supervisor as the process progresses; callers
    may read fields without locking — Python's GIL makes attribute reads
    atomic.  Do **not** mutate from outside the supervisor.
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
    """Everything the supervisor tracks for a run, public + private."""

    handle: RunHandle
    process: asyncio.subprocess.Process | None = None
    buffer: deque[LogLine] = field(default_factory=deque)
    subscribers: list[asyncio.Queue[LogLine | None]] = field(default_factory=list)
    drain_tasks: list[asyncio.Task] = field(default_factory=list)
    finished_event: asyncio.Event = field(default_factory=asyncio.Event)


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


# ---------------------------------------------------------------------------
# RunSupervisor
# ---------------------------------------------------------------------------


class RunSupervisor:
    """Tracks subprocess runs and broadcasts their output line-by-line.

    Thread-safety: this class is **not** thread-safe.  All methods must be
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

    async def start(
        self,
        slug: str,
        argv: list[str],
        cwd: Path,
    ) -> str:
        """Spawn ``argv`` in ``cwd`` and register a new run.

        Returns the supervisor-assigned ``run_id``. The subprocess and its
        stdout/stderr drain tasks are started before this returns.
        """
        if not argv:
            raise ValueError("argv must not be empty")

        run_id = uuid.uuid4().hex
        handle = RunHandle(
            run_id=run_id,
            slug=slug,
            status="running",
            exit_code=None,
            started_at=_now_iso(),
            finished_at=None,
        )
        record = _RunRecord(handle=handle)

        # Spawn the subprocess BEFORE registering the run as active. Otherwise
        # a spawn failure (binary missing, permission denied, OOM, etc.)
        # leaves the slug permanently in `_active_by_slug` and every future
        # re-run returns 409 Conflict.
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                # Put the subprocess in its own process group so SIGTERM hits
                # the whole tree (e.g. shells that fork children). POSIX-only;
                # on Windows we let the platform handle it.
                preexec_fn=os.setsid if os.name == "posix" else None,
            )
        except (OSError, FileNotFoundError, PermissionError):
            # Re-raise after recording the failure on the handle so callers
            # see a consistent "failed run" record. Do NOT register the run
            # as active.
            handle.status = "failed"
            handle.finished_at = _now_iso()
            raise

        record.process = process
        # Now that spawn succeeded, register the run.
        self._runs[run_id] = record
        self._active_by_slug[slug] = run_id

        # Kick off drain tasks.  They run until the streams hit EOF, then
        # the wait task observes the process exit and finalises the handle.
        if process.stdout is not None:
            record.drain_tasks.append(
                asyncio.create_task(
                    self._drain(record, process.stdout, "stdout"),
                    name=f"supervisor-stdout-{run_id}",
                )
            )
        if process.stderr is not None:
            record.drain_tasks.append(
                asyncio.create_task(
                    self._drain(record, process.stderr, "stderr"),
                    name=f"supervisor-stderr-{run_id}",
                )
            )
        record.drain_tasks.append(
            asyncio.create_task(
                self._wait(record),
                name=f"supervisor-wait-{run_id}",
            )
        )

        return run_id

    def get(self, run_id: str) -> RunHandle | None:
        """Return the public handle for ``run_id``, or ``None`` if unknown."""
        record = self._runs.get(run_id)
        return record.handle if record is not None else None

    def get_active_for_slug(self, slug: str) -> str | None:
        """Return the run_id of the slug's active (running) run, else None.

        Used by the re-run endpoint to detect 409-conflict scenarios.
        """
        run_id = self._active_by_slug.get(slug)
        if run_id is None:
            return None
        record = self._runs.get(run_id)
        if record is None or record.handle.status != "running":
            # Stale entry — clean it up.
            self._active_by_slug.pop(slug, None)
            return None
        return run_id

    async def stream(self, run_id: str) -> AsyncIterator[LogLine]:
        """Yield ``LogLine`` objects for ``run_id`` until the run terminates.

        Behaviour:

        - Unknown run_id: returns immediately (yields nothing).
        - Known run, still running:
            1. yield a snapshot of the buffer in arrival order;
            2. then yield live lines from a per-subscriber queue;
            3. when the process exits AND the subscriber queue is empty,
               return.
        - Known run, already finished: yield buffered lines, then return.

        Subscriber disconnect: if the consumer breaks out of the loop or
        closes its generator, the subscriber queue is cleaned up — but the
        subprocess is **not** killed.  This is intentional: the user
        navigating away from a tab must not abort their pipeline run.
        """
        record = self._runs.get(run_id)
        if record is None:
            return

        # Register a subscriber queue *before* snapshotting the buffer to
        # avoid a race: any line appended after we snapshot will land in
        # the queue.  We dedupe by tracking the buffer length we already
        # yielded.
        queue: asyncio.Queue[LogLine | None] = asyncio.Queue()
        record.subscribers.append(queue)
        try:
            # Snapshot buffered lines (in arrival order).
            snapshot = list(record.buffer)
            for line in snapshot:
                yield line

            # If the process is already finished AND we drained the queue
            # of any straggler lines, we can return.  Otherwise we drain
            # live until the producer pushes the sentinel ``None``.
            while True:
                if record.handle.status != "running" and queue.empty():
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    # Producer signals end-of-stream.
                    return
                # Skip replays already covered by the snapshot.
                # We do this naively by reference equality on the LogLine
                # tuple — duplicates only happen for items that were in the
                # buffer before we registered.  An exact equality check
                # filters them while keeping later items.
                if item in snapshot:
                    continue
                yield item
        finally:
            try:
                record.subscribers.remove(queue)
            except ValueError:
                pass

    async def kill(self, run_id: str, *, timeout_s: float = 5.0) -> bool:
        """Terminate ``run_id``.

        SIGTERM the process group; if it has not exited within
        ``timeout_s`` seconds, escalate to SIGKILL.  Returns ``True`` if
        we actually terminated a running process, ``False`` if the run
        was already done or unknown.
        """
        record = self._runs.get(run_id)
        if record is None or record.process is None:
            return False
        if record.handle.status != "running":
            return False

        process = record.process
        try:
            if os.name == "posix":
                # Signal the whole process group set up via setsid.
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    process.terminate()
            else:
                process.terminate()
        except ProcessLookupError:
            # Already gone — let _wait finalise.
            pass

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            # Escalate.
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

        # Mark as killed (overrides whatever _wait would otherwise set).
        record.handle.status = "killed"
        record.handle.exit_code = process.returncode
        record.handle.finished_at = _now_iso()
        self._active_by_slug.pop(record.handle.slug, None)

        # Notify subscribers of end-of-stream.
        for q in record.subscribers:
            q.put_nowait(None)
        record.finished_event.set()
        return True

    # ------------------------------------------------------------------
    # Internal coroutines
    # ------------------------------------------------------------------

    async def _drain(
        self,
        record: _RunRecord,
        reader: asyncio.StreamReader,
        stream_name: StreamName,
    ) -> None:
        """Read ``reader`` line-by-line, append each to buffer + queues."""
        while True:
            raw = await reader.readline()
            if not raw:
                # EOF.
                return
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            line = LogLine(
                stream=stream_name,
                line=text,
                timestamp=_now_iso(),
            )
            self._append(record, line)

    def _append(self, record: _RunRecord, line: LogLine) -> None:
        """Append ``line`` to the buffer (capped) and broadcast to queues."""
        buf = record.buffer
        buf.append(line)
        # Trim from the left when over cap.
        while len(buf) > self._max_buffered_lines:
            buf.popleft()
        for q in record.subscribers:
            # Queues are unbounded; put_nowait cannot fail.
            q.put_nowait(line)

    async def _wait(self, record: _RunRecord) -> None:
        """Wait for the subprocess to exit, finalise the handle, notify subs."""
        process = record.process
        if process is None:
            return

        exit_code = await process.wait()

        # Drain coroutines may still be flushing the last few bytes.  Wait
        # for them so the buffer is complete before we mark the handle as
        # done — otherwise a fast consumer would miss the tail.
        for task in record.drain_tasks:
            if task is asyncio.current_task():
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass

        # If kill() already finalised, do not overwrite its 'killed' state.
        if record.handle.status == "running":
            record.handle.exit_code = exit_code
            record.handle.status = "done" if exit_code == 0 else "failed"
            record.handle.finished_at = _now_iso()
            self._active_by_slug.pop(record.handle.slug, None)

        # Tell every subscriber: end of stream.
        for q in record.subscribers:
            q.put_nowait(None)
        record.finished_event.set()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_default_supervisor = RunSupervisor()


def get_supervisor() -> RunSupervisor:
    """Return the process-wide default supervisor.

    Tests that need isolation construct their own ``RunSupervisor()`` and
    pass it in directly.  Production code paths (the SSE endpoint, the
    upcoming POST /run endpoint) call ``get_supervisor()``.
    """
    return _default_supervisor
