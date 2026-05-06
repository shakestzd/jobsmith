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

Terminal phase guard (feat-438090af)
------------------------------------
When the apply subprocess exits non-zero (or is SIGKILL'd), the supervisor
reads the transcript.jsonl tail (last 50 lines) and checks whether a
terminal phase event (status=success or status=failed) was already written.
If absent, it synthesises a ``SynthPhaseEvent`` and broadcasts it to every
subscriber queue before sending the end-of-stream sentinel.  This guarantees
the SSE consumer always receives at least one terminal phase signal.

Public API surface
------------------
``LogLine``, ``SynthPhaseEvent``, ``RunHandle``, ``RunSupervisor``,
``get_supervisor``, ``synth_terminal_phase_failed``.

The default singleton is constructed with ``max_buffered_lines=10_000``.
Tests construct their own ``RunSupervisor(max_buffered_lines=...)`` to
exercise the cap without spawning a million-line stub.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sqlite3
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

__all__ = [
    "LogLine",
    "SynthPhaseEvent",
    "TranscriptEvent",
    "RunHandle",
    "RunSupervisor",
    "get_supervisor",
    "synth_terminal_phase_failed",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


StreamName = Literal["stdout", "stderr"]
RunStatus = Literal["running", "done", "failed", "killed"]

# Union of items that can appear in the supervisor stream.
StreamItem = "LogLine | SynthPhaseEvent | TranscriptEvent"


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


@dataclass(frozen=True)
class TranscriptEvent:
    """A structured event tailed from the apply pipeline's transcript.jsonl.

    The renderer in ``jobsmith.render`` writes every agent event
    (``tool_call``, ``tool_result``, ``text``, phase boundary markers) to
    ``transcript.jsonl`` directly, bypassing stdout. The supervisor tails
    the file and emits each new line as a TranscriptEvent so the SSE pump
    can forward structured agent activity to the UI without parsing
    terminal-formatted log lines (bug-0e13706c).

    ``payload`` is the raw decoded JSON object, exactly as the renderer
    wrote it. Consumers are expected to switch on ``payload['type']`` (or
    ``payload['_phase_boundary']`` for boundary markers).
    """

    run_id: str
    payload: dict


@dataclass(frozen=True)
class SynthPhaseEvent:
    """A synthesised terminal phase=failed event (feat-438090af).

    Emitted by the supervisor when the subprocess exits non-zero without
    having written a terminal phase event to the transcript.  The frontend's
    phase tracker consumes this the same way it consumes a real phase event.
    """

    run_id: str
    status: str  # always "failed"
    last_phase: str  # last phase seen in transcript, or "unknown"
    error_excerpt: str  # last 1-2 non-empty stderr/transcript lines


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
    # Buffer holds LogLine, SynthPhaseEvent, or TranscriptEvent items.
    buffer: deque = field(default_factory=deque)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    drain_tasks: list[asyncio.Task] = field(default_factory=list)
    finished_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Optional transcript path for failure synthesis (feat-438090af) and
    # structured event tailing (bug-0e13706c).
    transcript_path: Path | None = None
    # Stderr lines accumulated during drain for the synth excerpt.
    _stderr_tail: list[str] = field(default_factory=list)
    # Transcript tail position (bytes consumed). The tailer resumes from here.
    # Used by the legacy file-based path; DB-based polling uses
    # ``_log_last_id`` instead (trk-60217f9f Pass 4).
    _transcript_offset: int = 0
    # apply_state_log row-id cursor — the highest id forwarded so far. The
    # tailer reads rows with ``id > _log_last_id`` each poll, advancing the
    # cursor on success. Initialized to 0 so a fresh run picks up every
    # event (including the phase-boundary marker written at open_transcript).
    _log_last_id: int = 0
    # Slug + DB path resolved at launch time. When both are present the
    # tailer prefers the DB path (Pass 4); when either is None it falls
    # back to the file-based path (Pass 4 dual-write keeps both populated
    # in normal flow, but tests and sidecar contexts may set only one).
    slug: str | None = None
    db_path: Path | None = None
    # Set by _wait the moment the subprocess exits (BEFORE awaiting drain
    # tasks). The transcript tailer uses this — not handle.status — to
    # decide when to do its final read pass and return. Without this, the
    # tailer would deadlock against _wait, which awaits the tailer before
    # flipping handle.status.
    _subprocess_exited: asyncio.Event = field(default_factory=asyncio.Event)


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


_TERMINAL_STATUSES = frozenset({"success", "failed", "done"})
# render.py emits phase events as {"type": "phase_complete"} or
# {"type": "phase_failed"} without a "status" field — recognise both
# shapes so the synth gate doesn't fire duplicate failures.
# Closes roborev branch-review MEDIUM (feat-6d76bb22).
_TERMINAL_TYPES = frozenset({"phase_complete", "phase_failed"})
_TRANSCRIPT_TAIL_LINES = 50


def synth_terminal_phase_failed(
    *,
    transcript_path: Path,
    returncode: int,
    last_stderr_lines: list[str],
) -> dict | None:
    """Return a synthesised phase=failed payload, or None.

    This is a pure function (no I/O side-effects beyond reading a file):

    - Returns ``None`` when *returncode* is 0 (clean exit — no synthesis needed).
    - Returns ``None`` when the transcript already contains a terminal phase
      event (status in {success, failed, done}) — the pipeline wrote it.
    - Returns a ``{"status": "failed", "last_phase": ..., "error_excerpt": ...}``
      dict otherwise.

    Args:
        transcript_path: Path to ``transcript.jsonl`` (may not exist).
        returncode: Subprocess exit code (negative for signals, e.g. -9 for SIGKILL).
        last_stderr_lines: Recent stderr lines for the error_excerpt.
    """
    # Zero exit = clean; no synthesis needed.
    if returncode == 0:
        return None

    last_phase = "unknown"
    has_terminal = False

    tail_lines: list[str] = []
    try:
        if transcript_path.exists():
            raw = transcript_path.read_text(encoding="utf-8", errors="replace")
            all_lines = [ln for ln in raw.splitlines() if ln.strip()]
            tail_lines = all_lines[-_TRANSCRIPT_TAIL_LINES:]
    except OSError:
        pass  # Missing or unreadable — treat as empty.

    for raw_line in tail_lines:
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        status = obj.get("status")
        phase = obj.get("phase")
        event_type = obj.get("type")
        if phase:
            last_phase = phase
        if status in _TERMINAL_STATUSES or event_type in _TERMINAL_TYPES:
            has_terminal = True
            # Do not break — keep scanning so last_phase stays current.

    if has_terminal:
        return None

    # Build error_excerpt from stderr tail (prefer non-empty lines).
    excerpt_lines = [ln for ln in last_stderr_lines if ln.strip()][-2:]
    error_excerpt = " | ".join(excerpt_lines) if excerpt_lines else ""

    return {
        "status": "failed",
        "last_phase": last_phase,
        "error_excerpt": error_excerpt,
    }


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
        *,
        transcript_path: Path | None = None,
        db_path: Path | None = None,
    ) -> str:
        """Spawn ``argv`` in ``cwd`` and register a new run.

        Returns the supervisor-assigned ``run_id``. The subprocess and its
        stdout/stderr drain tasks are started before this returns.

        Args:
            slug: Application slug (used for conflict detection).
            argv: Command + arguments to spawn.
            cwd: Working directory for the subprocess.
            transcript_path: Optional path to ``transcript.jsonl`` used by the
                terminal-phase guard (feat-438090af). When provided and the
                subprocess exits non-zero without a terminal phase event in the
                transcript, a :class:`SynthPhaseEvent` is broadcast to all
                subscribers before the end-of-stream sentinel.
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
        # trk-60217f9f Pass 4 + roborev MEDIUM (job 951): initialise the
        # apply_state_log cursor at the current max(id) for this slug so a
        # rerun does not replay history. The first row this run writes will
        # have id > log_last_id and will be forwarded exactly once.
        log_last_id = 0
        if db_path is not None and db_path.exists():
            try:
                from ..db import open_pipeline_db

                _conn = open_pipeline_db(db_path)
                try:
                    row = _conn.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM apply_state_log "
                        "WHERE slug = ?",
                        (slug,),
                    ).fetchone()
                    log_last_id = int(row[0]) if row else 0
                finally:
                    _conn.close()
            except sqlite3.Error:
                log_last_id = 0

        record = _RunRecord(
            handle=handle,
            transcript_path=transcript_path,
            slug=slug,
            db_path=db_path,
            _log_last_id=log_last_id,
        )

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
        # bug-0e13706c: tail transcript.jsonl and forward each new event as
        # a structured TranscriptEvent over SSE. trk-60217f9f Pass 4 prefers
        # the apply_state_log DB tail when a db_path is supplied (and falls
        # back to the file when not). Both paths receive the same payloads
        # because render.py dual-writes during the migration window.
        if db_path is not None and slug:
            record.drain_tasks.append(
                asyncio.create_task(
                    self._tail_state_log(record),
                    name=f"supervisor-transcript-{run_id}",
                )
            )
        elif transcript_path is not None:
            record.drain_tasks.append(
                asyncio.create_task(
                    self._tail_transcript(record),
                    name=f"supervisor-transcript-{run_id}",
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

    async def stream(self, run_id: str) -> AsyncIterator[LogLine | SynthPhaseEvent | TranscriptEvent]:
        """Yield items for ``run_id`` until the run terminates.

        Items are :class:`LogLine` (stdout/stderr output),
        :class:`SynthPhaseEvent` (synthesised terminal phase on failure),
        or :class:`TranscriptEvent` (structured event tailed from
        transcript.jsonl — bug-0e13706c).

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
        queue: asyncio.Queue = asyncio.Queue()
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
                # We do this naively by reference equality on the item
                # (frozen dataclass) — duplicates only happen for items
                # that were in the buffer before we registered.
                if item in snapshot:
                    continue
                yield item
        finally:
            with contextlib.suppress(ValueError):
                record.subscribers.remove(queue)

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
            # Accumulate recent stderr lines for the synth excerpt (capped).
            if stream_name == "stderr" and text.strip():
                record._stderr_tail.append(text)
                if len(record._stderr_tail) > 20:
                    record._stderr_tail = record._stderr_tail[-20:]

    async def _tail_transcript(self, record: _RunRecord) -> None:
        """Poll *record.transcript_path* and emit each new JSON line as a TranscriptEvent.

        The renderer writes events incrementally with a flush after each
        record (``render.py:_write_transcript``). This tailer:

        - Polls every 100 ms for file growth (no inotify; the file may not
          exist yet when the subprocess starts).
        - Reads from the persisted byte offset (resumable across restarts —
          though restarts are not supported today, the bookkeeping is cheap).
        - Splits at newlines, ignores trailing partial lines (the next poll
          will pick them up once the renderer flushes the newline).
        - Decodes each line as JSON; non-JSON lines are dropped silently
          (defensive: prevents one corrupt line from killing the stream).
        - Stops when the run is no longer ``running`` and the file has no
          more bytes after the offset.

        Failures here must NEVER break the SSE stream — the loop is wrapped
        in a try/except that logs and exits cleanly.
        """
        path = record.transcript_path
        if path is None:
            return
        try:
            while True:
                exists = path.exists()
                if exists:
                    try:
                        with path.open("rb") as fh:
                            fh.seek(record._transcript_offset)
                            chunk = fh.read()
                            new_offset = record._transcript_offset + len(chunk)
                    except OSError:
                        chunk = b""
                        new_offset = record._transcript_offset

                    if chunk:
                        # Defer offset advance until we've parsed a complete
                        # line. If the last line is partial, leave its bytes
                        # in the file (don't advance offset past them).
                        text = chunk.decode("utf-8", errors="replace")
                        complete, _, partial = text.rpartition("\n")
                        consumed_bytes = len(chunk) - len(partial.encode("utf-8"))
                        record._transcript_offset += consumed_bytes
                        if complete:
                            for raw_line in complete.splitlines():
                                line = raw_line.strip()
                                if not line:
                                    continue
                                try:
                                    payload = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                if not isinstance(payload, dict):
                                    continue
                                self._append(
                                    record,
                                    TranscriptEvent(
                                        run_id=record.handle.run_id,
                                        payload=payload,
                                    ),
                                )
                        # If we read but parsed nothing AND the run finished,
                        # exit on next iteration via the gate below.

                # Termination gate: once the subprocess has exited, do one
                # final settle-pass and stop. Any partial line still in the
                # file is abandoned (no newline = renderer never wrote it).
                # We watch _subprocess_exited rather than handle.status because
                # _wait awaits this tailer before flipping the status — using
                # status here would deadlock.
                if record._subprocess_exited.is_set():
                    # Brief grace so a final renderer flush can land.
                    await asyncio.sleep(0.15)
                    # Re-read once more for any tail bytes that arrived during sleep.
                    try:
                        if path.exists():
                            with path.open("rb") as fh:
                                fh.seek(record._transcript_offset)
                                final_chunk = fh.read()
                            if final_chunk:
                                final_text = final_chunk.decode("utf-8", errors="replace")
                                final_complete, _, _ = final_text.rpartition("\n")
                                if final_complete:
                                    for raw_line in final_complete.splitlines():
                                        line = raw_line.strip()
                                        if not line:
                                            continue
                                        try:
                                            payload = json.loads(line)
                                        except json.JSONDecodeError:
                                            continue
                                        if not isinstance(payload, dict):
                                            continue
                                        self._append(
                                            record,
                                            TranscriptEvent(
                                                run_id=record.handle.run_id,
                                                payload=payload,
                                            ),
                                        )
                    except OSError:
                        pass
                    return

                await asyncio.sleep(0.1)
        except Exception:  # noqa: BLE001 — tailer failures must not break SSE.
            logger.exception(
                "transcript tailer crashed for run_id=%r slug=%r",
                record.handle.run_id,
                record.handle.slug,
            )

    async def _tail_state_log(self, record: _RunRecord) -> None:
        """Poll ``apply_state_log`` for *record.slug* and emit each new row.

        DB-backed counterpart to :meth:`_tail_transcript` (trk-60217f9f Pass
        4). The renderer dual-writes every transcript record into
        ``apply_state_log`` — this tailer reads rows with ``id > _log_last_id``
        every 100 ms, advances the cursor on success, and stops once the
        subprocess has exited and a final settle-pass returns no new rows.

        The deadlock fix from bug-0e13706c is preserved: the tailer watches
        ``_subprocess_exited`` (not ``handle.status``) so ``_wait`` does not
        block on a status flip that depends on this task completing.
        """
        slug = record.slug
        db_path = record.db_path
        if not slug or db_path is None:
            return
        try:
            from ..db import open_pipeline_db, read_state_log

            # roborev job 953 MEDIUM — tail by row-id only, not slug.
            # The orchestrator's ``jobsmith db rekey-slug`` step moves
            # apply_state_log rows from the URL-derived launch slug
            # ``record.slug`` to the canonical company-position slug as
            # soon as ``apply-jd-parser`` finishes; a slug-pinned filter
            # would silently drop every transcript event written after
            # rekey (i.e. most of gather + all of draft + render). The
            # ``_log_last_id`` cursor is unique per supervisor run and
            # the project DB is single-tenant, so polling without a
            # slug filter cannot cross-pollute concurrent runs.
            while True:
                try:
                    conn = open_pipeline_db(db_path)
                    try:
                        rows = read_state_log(
                            conn, after_id=record._log_last_id
                        )
                    finally:
                        conn.close()
                except sqlite3.Error:
                    rows = []

                for row_id, _ts, payload_str in rows:
                    try:
                        payload = json.loads(payload_str)
                    except json.JSONDecodeError:
                        record._log_last_id = max(record._log_last_id, row_id)
                        continue
                    if not isinstance(payload, dict):
                        record._log_last_id = max(record._log_last_id, row_id)
                        continue
                    self._append(
                        record,
                        TranscriptEvent(
                            run_id=record.handle.run_id, payload=payload
                        ),
                    )
                    record._log_last_id = max(record._log_last_id, row_id)

                # Termination gate (mirror of _tail_transcript).
                if record._subprocess_exited.is_set():
                    await asyncio.sleep(0.15)
                    try:
                        conn = open_pipeline_db(db_path)
                        try:
                            final_rows = read_state_log(
                                conn, after_id=record._log_last_id
                            )
                        finally:
                            conn.close()
                    except sqlite3.Error:
                        final_rows = []
                    for row_id, _ts, payload_str in final_rows:
                        try:
                            payload = json.loads(payload_str)
                        except json.JSONDecodeError:
                            record._log_last_id = max(record._log_last_id, row_id)
                            continue
                        if not isinstance(payload, dict):
                            record._log_last_id = max(record._log_last_id, row_id)
                            continue
                        self._append(
                            record,
                            TranscriptEvent(
                                run_id=record.handle.run_id, payload=payload
                            ),
                        )
                        record._log_last_id = max(record._log_last_id, row_id)
                    return

                await asyncio.sleep(0.1)
        except Exception:  # noqa: BLE001 — tailer failures must not break SSE.
            logger.exception(
                "apply_state_log tailer crashed for run_id=%r slug=%r",
                record.handle.run_id,
                record.handle.slug,
            )

    def _append(self, record: _RunRecord, item: LogLine | SynthPhaseEvent | TranscriptEvent) -> None:
        """Append ``item`` to the buffer (capped) and broadcast to queues."""
        buf = record.buffer
        buf.append(item)
        # Trim from the left when over cap.
        while len(buf) > self._max_buffered_lines:
            buf.popleft()
        for q in record.subscribers:
            # Queues are unbounded; put_nowait cannot fail.
            q.put_nowait(item)

    async def _wait(self, record: _RunRecord) -> None:
        """Wait for the subprocess to exit, finalise the handle, notify subs."""
        process = record.process
        if process is None:
            return

        exit_code = await process.wait()
        # Signal the transcript tailer that it can do its final read pass.
        # MUST happen BEFORE awaiting drain_tasks (otherwise the tailer would
        # block forever on a status flip that doesn't happen until after the
        # tailer exits — bug-0e13706c integration deadlock).
        record._subprocess_exited.set()

        # Drain coroutines may still be flushing the last few bytes.  Wait
        # for them so the buffer is complete before we mark the handle as
        # done — otherwise a fast consumer would miss the tail.
        for task in record.drain_tasks:
            if task is asyncio.current_task():
                continue
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # If kill() already finalised, do not overwrite its 'killed' state.
        if record.handle.status == "running":
            record.handle.exit_code = exit_code
            record.handle.status = "done" if exit_code == 0 else "failed"
            record.handle.finished_at = _now_iso()
            self._active_by_slug.pop(record.handle.slug, None)

        # Terminal-phase guard (feat-438090af): when the subprocess exits
        # non-zero and no terminal phase event was written to the transcript,
        # synthesise one and broadcast it before the end-of-stream sentinel.
        if exit_code != 0 and record.transcript_path is not None:
            try:
                payload = synth_terminal_phase_failed(
                    transcript_path=record.transcript_path,
                    returncode=exit_code,
                    last_stderr_lines=list(record._stderr_tail),
                )
                if payload is not None:
                    synth = SynthPhaseEvent(
                        run_id=record.handle.run_id,
                        status=payload["status"],
                        last_phase=payload["last_phase"],
                        error_excerpt=payload["error_excerpt"],
                    )
                    self._append(record, synth)
            except Exception:  # noqa: BLE001 — synth must never break the stream
                logger.exception(
                    "terminal phase synth failed for run_id=%r slug=%r",
                    record.handle.run_id,
                    record.handle.slug,
                )

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
