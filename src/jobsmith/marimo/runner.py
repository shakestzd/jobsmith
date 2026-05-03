"""Queue bridge between run_phase_iter() and a marimo reactive cell.

Marimo has no built-in async-generator cell support.  This module provides
:class:`NotebookRunner` — a threading.Thread + queue.Queue bridge that:

1. Runs :func:`jobsmith.apply.run_phase_iter` in a background thread.
2. Forwards every :class:`~jobsmith.apply.PipelineEvent` to ``events_queue``.
3. Writes an ``apply_runs`` DB row (status=running → done/cancelled/failed).
4. Calls :func:`~jobsmith.db_ingest.ingest_phase_outputs` after each
   ``phase_complete`` event.
5. Puts a :class:`_Done` sentinel on the queue when the thread exits.

The reactive marimo cell polls ``events_queue`` with ``queue.Queue.get_nowait``
(or ``get(timeout=…)``) and updates a progress bar per event.

Design
------
- Pure logic: **no marimo import** — fully testable with pytest.
- No duplicate SQL: reuse ``jobsmith.db`` helpers.
- cancel_event is a threading.Event threaded through to run_phase_iter.
"""
from __future__ import annotations

import contextlib
import json
import queue
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from jobsmith.apply import derive_slug, phase_for_specialist, run_phase_iter
from jobsmith.db import (
    insert_apply_run,
    open_pipeline_db,
)
from jobsmith.db_ingest import ingest_phase_outputs

if TYPE_CHECKING:
    pass


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _reset_specialist_in_manifest(state_dir: Path, specialist_name: str) -> None:
    """Drop *specialist_name*'s entry from manifest.invocations.

    This forces the orchestrator agent to re-run the specialist on the next
    phase invocation (Option A: full phase re-run, ingestor picks up the new
    artifact). No-op when the manifest is absent or the specialist is not
    found.

    Parameters
    ----------
    state_dir:
        Absolute path to the ``.apply-state/`` directory.
    specialist_name:
        The specialist slug (e.g. ``"apply-fit-scorer"``).
    """
    manifest_path = state_dir / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    invocations = manifest.get("invocations", [])
    manifest["invocations"] = [
        inv for inv in invocations
        if inv.get("specialist") != specialist_name
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2))


@dataclass
class _Done:
    """Sentinel placed on events_queue when the runner thread exits.

    Attributes
    ----------
    status:
        One of ``"done"``, ``"cancelled"``, or ``"failed"``.
    """

    status: str  # "done" | "cancelled" | "failed"


class NotebookRunner:
    """Thread + queue bridge for driving run_phase_iter from a marimo cell.

    Parameters
    ----------
    db_path:
        Absolute path to ``private/jobsmith.db``.
    applications_dir:
        Absolute path to ``private/applications/`` (resolved from config by
        the notebook cell before constructing the runner).

    Public API
    ----------
    start(url, cwd) -> str
        Spawn the background thread; return run_id.
        Raises :exc:`RuntimeError` if already running.
    cancel() -> None
        Set the cancel event; runner thread stops after current phase.
    is_running() -> bool
        True while the background thread is alive.
    events_queue : queue.Queue[PipelineEvent | _Done]
        Consumer reads from here.
    """

    def __init__(self, db_path: Path, applications_dir: Path) -> None:
        self.db_path = db_path
        self.applications_dir = applications_dir
        self.events_queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._run_id: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """Return True while the background thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self, url: str, cwd: Path) -> str:
        """Spawn the background thread and return the run_id.

        Raises
        ------
        RuntimeError
            If the runner is already running (re-entry guard).
        """
        if self.is_running():
            raise RuntimeError(
                "NotebookRunner is already running. "
                "Call cancel() and wait for _Done before starting again."
            )
        # Reset cancel event and drain stale queue items from prior run
        self._cancel_event.clear()
        while not self.events_queue.empty():
            try:
                self.events_queue.get_nowait()
            except queue.Empty:
                break

        self._run_id = str(uuid.uuid4())
        self._thread = threading.Thread(
            target=self._run,
            args=(url, cwd),
            daemon=True,
        )
        self._thread.start()
        return self._run_id

    def run_specialist(
        self,
        *,
        url: str,
        specialist_name: str,
        cwd: Path,
    ) -> str:
        """Re-run a single specialist via Option A (full phase + manifest reset).

        Raises :exc:`RuntimeError` if the runner is already in flight.
        Raises :exc:`ValueError` if *specialist_name* is unknown.

        Parameters
        ----------
        url:
            Job description URL (used to derive the slug).
        specialist_name:
            The specialist to re-run (e.g. ``"apply-fit-scorer"``).
        cwd:
            Working directory (project root).

        Returns
        -------
        str
            The new ``run_id`` created for this re-run.
        """
        # Resolve phase first so we raise ValueError before touching anything
        phase = phase_for_specialist(specialist_name)

        if self.is_running():
            raise RuntimeError(
                "NotebookRunner is already running. "
                "Call cancel() and wait for _Done before starting again."
            )

        # Reset manifest entry so the agent re-runs this specialist
        slug = derive_slug(url)
        state_dir = self.applications_dir / slug / ".apply-state"
        _reset_specialist_in_manifest(state_dir, specialist_name)

        # Reset cancel event and drain stale queue items
        self._cancel_event.clear()
        while not self.events_queue.empty():
            try:
                self.events_queue.get_nowait()
            except queue.Empty:
                break

        self._run_id = str(uuid.uuid4())
        self._thread = threading.Thread(
            target=self._run,
            args=(url, cwd),
            kwargs={"phases": [phase]},
            daemon=True,
        )
        self._thread.start()
        return self._run_id

    def cancel(self) -> None:
        """Signal the runner to stop after the current phase."""
        self._cancel_event.set()

    # ------------------------------------------------------------------
    # Internal — thread target
    # ------------------------------------------------------------------

    def _run(
        self,
        url: str,
        cwd: Path,
        *,
        phases: list[str] | None = None,
    ) -> None:
        """Background thread target: drive run_phase_iter, write DB, ingest.

        Parameters
        ----------
        url:
            Job description URL.
        cwd:
            Working directory (project root).
        phases:
            When ``None`` (default), all phases are run (normal full pipeline).
            When a list, only the listed phases are run; ``run_phase_iter`` is
            called with ``force=True`` so it does not skip completed phases.
            Used by :meth:`run_specialist` for single-phase re-runs.
        """
        run_id = self._run_id
        assert run_id is not None  # set in start()/run_specialist() before thread creation

        slug = derive_slug(url)
        started_at = _now_iso()
        final_status = "failed"
        phase_label = phases[0] if phases and len(phases) == 1 else "unknown"

        # Open a dedicated connection for this thread
        conn = open_pipeline_db(self.db_path)
        try:
            # Insert the apply_runs row with status=running
            insert_apply_run(
                conn,
                run_id=run_id,
                slug=slug,
                phase=phase_label,
                started_at=started_at,
                finished_at=None,
                status="running",
            )
            conn.commit()

            try:
                iter_kwargs: dict = {
                    "cwd": cwd,
                    "skip_confirm": True,
                    "cancel_event": self._cancel_event,
                }
                # For single-phase re-runs force=True so run_phase_iter does
                # not skip the (already-completed) phase.
                if phases is not None:
                    iter_kwargs["force"] = True

                stop_after_phases = set(phases) if phases is not None else None

                for event in run_phase_iter(url, **iter_kwargs):
                    # Forward every event to the consumer queue
                    self.events_queue.put(event)

                    # Track slug changes for accurate DB slug recording
                    if event.kind == "slug_changed":
                        new_slug = event.payload.get("new_slug")
                        if new_slug:
                            slug = new_slug

                    # Post-phase ingest after each phase_complete.
                    # Ingest failure must not abort the pipeline — a single
                    # broken artifact should not lose the rest of the run.
                    if event.kind == "phase_complete":
                        state_dir = (
                            self.applications_dir / slug / ".apply-state"
                        )
                        with contextlib.suppress(Exception):
                            ingest_phase_outputs(
                                conn,
                                slug=slug,
                                run_id=run_id,
                                phase=event.phase,
                                state_dir=state_dir,
                            )
                        # For single-phase re-runs: stop after the target phase
                        if (
                            stop_after_phases is not None
                            and event.phase in stop_after_phases
                        ):
                            final_status = "done"
                            break

                    # Cancelled event from the generator
                    if event.kind == "cancelled":
                        final_status = "cancelled"
                        break

                else:
                    if final_status != "done":
                        final_status = (
                            "cancelled" if self._cancel_event.is_set() else "done"
                        )

            except Exception:  # noqa: BLE001
                final_status = "failed"

            # Update apply_runs row with final status
            conn.execute(
                "UPDATE apply_runs "
                "SET status=?, finished_at=?, slug=? "
                "WHERE run_id=?",
                (final_status, _now_iso(), slug, run_id),
            )
            conn.commit()

        finally:
            conn.close()

        # Sentinel on the queue — consumer detects completion
        self.events_queue.put(_Done(status=final_status))


_RUNNER_SINGLETON: NotebookRunner | None = None
_SINGLETON_LOCK = threading.Lock()


def get_runner(*, db_path: Path, applications_dir: Path) -> NotebookRunner:
    """Return the process-wide :class:`NotebookRunner` singleton.

    The marimo dispatch cell re-runs every time any of its inputs change
    (button click, slug-picker change, etc.). Constructing a new
    NotebookRunner per re-run loses the in-flight thread/queue/cancel_event
    references, breaking Stop and live progress. This accessor returns a
    single instance so the running thread survives reactive recomputation.

    The first call constructs the runner. Subsequent calls ignore the
    keyword arguments and return the existing instance — db_path and
    applications_dir are stable for the notebook session.
    """
    global _RUNNER_SINGLETON
    with _SINGLETON_LOCK:
        if _RUNNER_SINGLETON is None:
            _RUNNER_SINGLETON = NotebookRunner(
                db_path=db_path,
                applications_dir=applications_dir,
            )
        return _RUNNER_SINGLETON


def reset_runner() -> None:
    """Clear the singleton (testing helper / process-restart parity)."""
    global _RUNNER_SINGLETON
    with _SINGLETON_LOCK:
        _RUNNER_SINGLETON = None


__all__ = [
    "NotebookRunner",
    "_Done",
    "_reset_specialist_in_manifest",
    "get_runner",
    "reset_runner",
]
