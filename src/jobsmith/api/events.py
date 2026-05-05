"""Server-Sent Events router for live pipeline state (feat-440324f1).

Endpoint
--------
``GET /applications/{slug}/events`` — long-lived SSE stream that emits one
event per new ``specialist_outputs`` row and one per new ``apply_runs`` row
(or status change) for the given slug.

Schema reality (read this before changing the queries)
------------------------------------------------------
The plan-level slice description mentioned a generic ``events`` table — that
table does not exist. The actual pipeline DB (see
``src/jobsmith/migrations/001_initial_schema.sql``) has only:

- ``apply_runs (run_id TEXT PK, slug, phase, started_at, finished_at, status)``
- ``specialist_outputs (run_id, specialist, kind, output_json,
  transcript_ref, finished_at, version)`` with composite PK
  ``(run_id, specialist, kind)``.

Neither table has an autoincrement ``id`` column. We use SQLite's implicit
``rowid`` as the monotonic cursor — ``rowid`` is unique per row and assigned
in insertion order, which is exactly what an SSE feed needs.

Event shapes
------------
``event: phase``::

    {"run_id": "...", "phase": "draft", "status": "running",
     "started_at": "...", "finished_at": null}

``event: specialist``::

    {"run_id": "...", "specialist": "apply-jd-parser", "kind": "jd-parsed",
     "kind_label": "JD parsed", "version": 1,
     "phase": "gather", "status": "running",
     "finished_at": "...", "transcript_ref": null}

Heartbeats
----------
We yield ``ServerSentEvent(comment="ping")`` on a fixed interval so proxies
do not idle out the connection (default 15s; ``events_heartbeat_interval_s``
in app config for tests).

Verbosity filter
----------------
``?verbosity=quiet|normal|verbose``

- ``quiet``    → phase events only.
- ``normal``   → phase + the "significant" specialist kinds
                  (jd-parsed, fit-score, prose-draft, ats-check).
- ``verbose``  → everything (default for the frontend buttons).

Idle close
----------
After ``events_idle_timeout_s`` (default 300s) of no DB activity AND no
heartbeats due, the stream closes cleanly. The frontend reconnects.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from jobsmith.api.events_poll import (
    _db_poll_once,
)
from jobsmith.api.supervisor import RunSupervisor, SynthPhaseEvent, get_supervisor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])


# ---------------------------------------------------------------------------
# Tunables (overridable via create_app kwargs for tests)
# ---------------------------------------------------------------------------

DEFAULT_POLL_INTERVAL_S = 0.25
DEFAULT_HEARTBEAT_INTERVAL_S = 15.0
DEFAULT_IDLE_TIMEOUT_S = 300.0

# Specialist kinds considered "significant" under verbosity=normal.
# Selected as the user-facing milestones — anything that lands an artifact
# the review UI surfaces in its own card.
_SIGNIFICANT_KINDS = frozenset(
    {
        "jd-parsed",
        "fit-score",
        "prose-draft",
        "ats-check",
        "bullet-selection",
    }
)

# Human-readable labels for artifact kinds surfaced in the frontend UI.
# Unknown kinds fall back to the raw kind string.
_KIND_LABELS: dict[str, str] = {
    "jd-parsed": "JD parsed",
    "fit-score": "Fit scored",
    "prose-draft": "Prose draft",
    "ats-check": "ATS check",
    "bullet-selection": "Bullets selected",
    "cover-draft": "Cover draft",
    "factcheck": "Fact-checked",
    "render": "Rendered",
}

Verbosity = Literal["quiet", "normal", "verbose"]


# ---------------------------------------------------------------------------
# Path / config helpers
# ---------------------------------------------------------------------------


def _resolve_applications_dir(request: Request) -> Path:
    """Mirror of the helper in :mod:`jobsmith.api.applications`."""
    override: Path | None = getattr(request.app.state, "applications_dir", None)
    if override is not None:
        return override

    config = getattr(request.app.state, "config", None)
    if config is not None:
        from jobsmith.paths import resolve

        return resolve(config.output.applications_dir)

    from jobsmith.config import load_config
    from jobsmith.paths import resolve

    cfg = load_config()
    return resolve(cfg.output.applications_dir)


def _resolve_pipeline_db_path(request: Request) -> Path | None:
    """Return the pipeline DB path, or ``None`` if not configured.

    Tests inject via ``app.state.pipeline_db_path``. Production reads from
    the loaded config's ``output.jobsmith_db`` field.
    """
    override: Path | None = getattr(request.app.state, "pipeline_db_path", None)
    if override is not None:
        return override

    config = getattr(request.app.state, "config", None)
    if config is not None:
        from jobsmith.paths import resolve

        return resolve(config.output.jobsmith_db)

    # Last-resort default — load the config lazily.
    try:
        from jobsmith.config import load_config
        from jobsmith.paths import resolve

        cfg = load_config()
        return resolve(cfg.output.jobsmith_db)
    except Exception:  # noqa: BLE001 — config absent = no live stream available
        return None


def _get_event_tunable(request: Request, name: str, default: float) -> float:
    return float(getattr(request.app.state, name, default))


def _resolve_supervisor(request: Request) -> RunSupervisor:
    """Return the run supervisor (test-injected ``app.state.run_supervisor``
    if present, otherwise the module-level singleton)."""
    override = getattr(request.app.state, "run_supervisor", None)
    if isinstance(override, RunSupervisor):
        return override
    return get_supervisor()


# ---------------------------------------------------------------------------
# Slug guard (consistent with the detail endpoint's contract)
# ---------------------------------------------------------------------------


def _validate_slug_or_404(apps_dir: Path, slug: str) -> Path:
    """Return the slug dir; raise 404 if missing or 400 if path-suspicious."""
    if not slug or "/" in slug or ".." in slug or slug.startswith("."):
        raise HTTPException(status_code=400, detail=f"Invalid slug: {slug!r}")

    slug_dir = apps_dir / slug
    try:
        resolved = slug_dir.resolve()
        apps_resolved = apps_dir.resolve()
        if not str(resolved).startswith(str(apps_resolved)):
            raise HTTPException(status_code=400, detail="Path traversal detected")
    except OSError as err:
        raise HTTPException(status_code=400, detail="Cannot resolve slug path") from err

    if not slug_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Slug not found: {slug}")
    return slug_dir


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def _allow_specialist(verbosity: Verbosity, kind: str) -> bool:
    if verbosity == "quiet":
        return False
    if verbosity == "normal":
        return kind in _SIGNIFICANT_KINDS
    return True  # verbose


def _allow_log(verbosity: Verbosity, _stream_name: str) -> bool:
    """Whether to forward a supervisor ``log`` event to the wire.

    quiet: drop all log lines (UI shows phase milestones only).
    normal/verbose: keep stdout + stderr (stderr often carries the most
    useful diagnostics for the user staring at a stalled pipeline).
    """
    return verbosity != "quiet"


# ---------------------------------------------------------------------------
# Stream generator
# ---------------------------------------------------------------------------


async def _supervisor_log_producer(
    supervisor: RunSupervisor,
    slug: str,
    queue: asyncio.Queue,
    poll_interval_s: float,
    stop_event: asyncio.Event,
) -> None:
    """Watch ``slug`` for active runs and forward each LogLine to ``queue``.

    The SSE endpoint is long-lived; a slug may transition through multiple
    runs while a single browser tab stays connected.  This producer loops:
    when there is no active run, it polls (cheaply) for one to appear;
    when there is, it consumes ``supervisor.stream(run_id)`` until that run
    ends, then loops again.
    """
    seen_run_ids: set[str] = set()
    while not stop_event.is_set():
        run_id = supervisor.get_active_for_slug(slug)
        if run_id is None or run_id in seen_run_ids:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_s)
            continue

        seen_run_ids.add(run_id)
        try:
            async for log_line in supervisor.stream(run_id):
                if stop_event.is_set():
                    return
                # Pair (kind, payload) to disambiguate from DB rows.
                await queue.put(("log", run_id, log_line))
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — never let the producer break the SSE
            # Log the failure so operators can see when the live-log path
            # silently degrades to DB-poll-only — without this, a bug in
            # supervisor.stream() (or any consumer) would have no signal.
            # The DB poll continues regardless.
            logger.exception(
                "supervisor log producer failed for slug=%r run_id=%r",
                slug,
                run_id,
            )
            continue


async def _stream(
    *,
    request: Request,
    slug: str,
    db_path: Path | None,
    supervisor: RunSupervisor,
    verbosity: Verbosity,
    since_run_rowid: int,
    since_specialist_rowid: int,
    poll_interval_s: float,
    heartbeat_interval_s: float,
    idle_timeout_s: float,
) -> AsyncIterator[ServerSentEvent]:
    """Yield SSE events until the client disconnects or the stream goes idle.

    Two event sources feed this generator:

    1. **DB poll** (synchronous SQLite reads via ``asyncio.to_thread``) for
       ``apply_runs`` / ``specialist_outputs`` rows — these survive process
       restarts and are the canonical source for phase + specialist events.
    2. **Supervisor log queue** — live stdout/stderr lines from any active
       subprocess for this slug, fed by a background producer task.

    The two are merged via a shared ``asyncio.Queue``: the producer pushes
    log items, while the DB poll loop drains the queue between (or after)
    each poll round.  Heartbeats and idle close are decided after every
    iteration regardless of source.
    """
    loop = asyncio.get_event_loop()
    last_activity = loop.time()
    last_heartbeat = loop.time()

    # Track last-emitted status per run_id so we can detect terminal-state
    # transitions: ``apply_runs.status`` is UPDATED in place by the pipeline,
    # so a rowid-only poll misses the in-progress → done|failed transition.
    last_status_by_run: dict[str, str] = {}

    # Spawn the supervisor producer.  Cleaned up in the finally block.
    log_queue: asyncio.Queue = asyncio.Queue()
    stop_event = asyncio.Event()
    producer_task = asyncio.create_task(
        _supervisor_log_producer(
            supervisor, slug, log_queue, poll_interval_s, stop_event
        ),
        name=f"events-supervisor-producer-{slug}",
    )

    # Emit a sentinel "open" comment so clients see the stream is alive.
    yield ServerSentEvent(comment="stream open")

    try:
        while True:
            if await request.is_disconnected():
                return

            now = loop.time()
            saw_activity = False

            # --- Drain the supervisor log queue (non-blocking) ---
            # Items are either LogLine (live stdout/stderr) or SynthPhaseEvent
            # (S6 terminal-phase guard, feat-438090af).  Discriminate before
            # accessing fields — SynthPhaseEvent has run_id/status/last_phase/
            # error_excerpt and no .stream/.line/.timestamp.  Closes
            # ultrareview bug_029.
            while True:
                try:
                    item = log_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                _kind, run_id, payload_obj = item
                if isinstance(payload_obj, SynthPhaseEvent):
                    # Include both ``phase`` (matches the canonical phase-event
                    # shape consumed by the frontend phase tracker) and
                    # ``last_phase`` (verbose context for failure diagnostics).
                    # Closes roborev branch-review MEDIUM (feat-90e70f1f).
                    yield ServerSentEvent(
                        event="phase",
                        data=json.dumps({
                            "run_id": payload_obj.run_id,
                            "phase": payload_obj.last_phase,
                            "status": payload_obj.status,
                            "last_phase": payload_obj.last_phase,
                            "error_excerpt": payload_obj.error_excerpt,
                        }),
                    )
                    saw_activity = True
                    continue
                log_line = payload_obj
                if not _allow_log(verbosity, log_line.stream):
                    saw_activity = True  # still resets idle even if filtered
                    continue
                payload = {
                    "run_id": run_id,
                    "stream": log_line.stream,
                    "line": log_line.line,
                    "timestamp": log_line.timestamp,
                }
                yield ServerSentEvent(
                    event="log",
                    data=json.dumps(payload),
                )
                saw_activity = True

            if db_path is not None:
                # One worker → one connection → all queries → close.
                # Avoids cross-thread sqlite3.Connection reuse.
                (
                    runs,
                    specs,
                    current_runs,
                    since_run_rowid,
                    since_specialist_rowid,
                ) = await asyncio.to_thread(
                    _db_poll_once,
                    db_path,
                    slug,
                    since_run_rowid,
                    since_specialist_rowid,
                )

                # Phase events from new rows.
                for r in runs:
                    since_run_rowid = max(since_run_rowid, int(r["rowid"]))
                    last_status_by_run[r["run_id"]] = r["status"]
                    payload: dict[str, Any] = {
                        "run_id": r["run_id"],
                        "phase": r["phase"],
                        "status": r["status"],
                        "started_at": r["started_at"],
                        "finished_at": r["finished_at"],
                        "rowid": int(r["rowid"]),
                    }
                    yield ServerSentEvent(
                        event="phase",
                        data=json.dumps(payload),
                        id=f"run-{int(r['rowid'])}",
                    )
                    saw_activity = True

                # Phase events from in-place status changes (no new rowid).
                for r in current_runs:
                    run_id = r["run_id"]
                    status = r["status"]
                    if last_status_by_run.get(run_id) == status:
                        continue
                    last_status_by_run[run_id] = status
                    payload = {
                        "run_id": run_id,
                        "phase": r["phase"],
                        "status": status,
                        "started_at": r["started_at"],
                        "finished_at": r["finished_at"],
                        "rowid": int(r["rowid"]),
                    }
                    yield ServerSentEvent(
                        event="phase",
                        data=json.dumps(payload),
                        id=f"run-{int(r['rowid'])}-{status}",
                    )
                    saw_activity = True

                # Specialist events (filtered by verbosity).
                for s in specs:
                    since_specialist_rowid = max(
                        since_specialist_rowid, int(s["rowid"])
                    )
                    if not _allow_specialist(verbosity, s["kind"]):
                        continue
                    kind = s["kind"]
                    # Include version (feat-f637b9d2) and a human-readable label.
                    col_names = s.keys()
                    version = s["version"] if "version" in col_names else 1
                    payload = {
                        "run_id": s["run_id"],
                        "specialist": s["specialist"],
                        "kind": kind,
                        "kind_label": _KIND_LABELS.get(kind, kind),
                        "version": version,
                        "phase": s["phase"],
                        "status": s["status"],
                        "finished_at": s["finished_at"],
                        "transcript_ref": s["transcript_ref"],
                        "rowid": int(s["rowid"]),
                    }
                    yield ServerSentEvent(
                        event="specialist",
                        data=json.dumps(payload),
                        id=f"so-{int(s['rowid'])}",
                    )
                    saw_activity = True

            if saw_activity:
                last_activity = now

            # Heartbeat
            if now - last_heartbeat >= heartbeat_interval_s:
                yield ServerSentEvent(comment="ping")
                last_heartbeat = now

            # Idle close
            if now - last_activity >= idle_timeout_s:
                yield ServerSentEvent(event="idle-close", data="{}")
                return

            await asyncio.sleep(poll_interval_s)
    finally:
        # Tear down the supervisor producer.  IMPORTANT: this does NOT
        # kill the underlying subprocess — the supervisor outlives the SSE
        # connection.  Only the per-connection subscriber queue closes.
        stop_event.set()
        producer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await producer_task
        # No connection to close — _db_poll_once owns its lifecycle.


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/applications/{slug}/events")
async def stream_events(
    slug: str,
    request: Request,
    verbosity: Verbosity = Query("verbose"),  # noqa: B008
    since_run: int = Query(-1, alias="since_run"),  # noqa: B008
    since_specialist: int = Query(-1, alias="since"),  # noqa: B008
) -> EventSourceResponse:
    """Open a long-lived SSE stream of pipeline events for *slug*.

    Query params
    ------------
    verbosity:
        ``quiet`` | ``normal`` | ``verbose`` (default ``verbose``).
    since:
        Resume from a specialist_outputs rowid (Last-Event-ID style).
        Defaults to the current MAX rowid → only NEW events are streamed.
    since_run:
        Resume from an apply_runs rowid.
    """
    apps_dir = _resolve_applications_dir(request)
    _validate_slug_or_404(apps_dir, slug)

    db_path = _resolve_pipeline_db_path(request)
    supervisor = _resolve_supervisor(request)

    poll_interval_s = _get_event_tunable(
        request, "events_poll_interval_s", DEFAULT_POLL_INTERVAL_S
    )
    heartbeat_interval_s = _get_event_tunable(
        request, "events_heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S
    )
    idle_timeout_s = _get_event_tunable(
        request, "events_idle_timeout_s", DEFAULT_IDLE_TIMEOUT_S
    )

    generator = _stream(
        request=request,
        slug=slug,
        db_path=db_path,
        supervisor=supervisor,
        verbosity=verbosity,
        since_run_rowid=since_run,
        since_specialist_rowid=since_specialist,
        poll_interval_s=poll_interval_s,
        heartbeat_interval_s=heartbeat_interval_s,
        idle_timeout_s=idle_timeout_s,
    )

    return EventSourceResponse(generator)


__all__ = ["router"]
