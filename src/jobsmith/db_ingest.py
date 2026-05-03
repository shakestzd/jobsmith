"""Post-phase ingestion + backfill for the apply pipeline DB.

Specialists write artifacts to ``.apply-state/`` from inside subprocesses;
the wrapper has zero visibility into those writes during the phase, so we
ingest after each ``phase_complete`` event.  Backfill applies the same
ingestion to historical app dirs so existing slugs appear in the DB
without re-running ``apply``.

The ingest is one-way: ``manifest.json`` (agent-authoritative for in-flight
state) → DB rows (canonical for completed phases).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jobsmith._state_readers import (
    ARTIFACT_READERS,
    PHASE_SPECIALISTS,
    SPECIALIST_TO_ARTIFACT,
)

_BACKFILL_STATUS = "backfilled"
_UNKNOWN_PHASE = "unknown"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _load_manifest(state_dir: Path) -> dict | None:
    """Return parsed manifest.json or None on missing/corrupt."""
    manifest_path = state_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _phase_invocations(manifest: dict | None, phase: str) -> list[dict]:
    """Return the OK invocations from manifest.invocations that belong to ``phase``.

    The real apply-pipeline manifest format (written by the agent) is:

        {"run_id": ..., "slug": ..., "started_at": ...,
         "invocations": [
             {"specialist": "apply-jd-parser", "status": "ok",
              "started_at": "...", "finished_at": "...", ...},
             ...
         ]}

    There is no "phases" key — invocations are flat at the top level. We
    filter to the specialists that belong to ``phase`` via
    ``PHASE_SPECIALISTS`` so each post-phase ingest only touches its own
    artifacts.
    """
    if not manifest:
        return []
    invocations = manifest.get("invocations")
    if not isinstance(invocations, list):
        return []
    phase_specialists = set(PHASE_SPECIALISTS.get(phase, ()))
    return [
        inv
        for inv in invocations
        if isinstance(inv, dict)
        and inv.get("specialist") in phase_specialists
        and inv.get("status") == "ok"
    ]


def _serialize_artifact(data: object) -> str:
    """JSON-encode a reader output. Wraps bare strings as {'text': ...}."""
    if isinstance(data, str):
        return json.dumps({"text": data})
    return json.dumps(data)


def ingest_phase_outputs(
    conn: sqlite3.Connection,
    *,
    slug: str,
    run_id: str,
    phase: str,
    state_dir: Path,
) -> int:
    """Read .apply-state/ artifacts for ``phase`` and insert specialist_outputs rows.

    Reads ``manifest.json.invocations`` (the real format written by the
    apply pipeline; see ``apply-agent.md``). Filters to specialists that
    belong to ``phase`` via :data:`PHASE_SPECIALISTS`, maps each specialist
    name to its expected artifact filename via :data:`SPECIALIST_TO_ARTIFACT`,
    and dispatches the matching reader from :data:`ARTIFACT_READERS`.

    Idempotent via INSERT OR IGNORE on (run_id, specialist, kind). Missing
    artifacts and reader errors are skipped silently so a single broken
    artifact does not block the phase.

    Returns the number of *new* rows inserted (skipped duplicates excluded).
    """
    invocations = _phase_invocations(_load_manifest(state_dir), phase)
    if not invocations:
        return 0

    inserted = 0
    finished_at = _now_iso()

    with conn:
        for inv in invocations:
            specialist_name = inv.get("specialist")
            output_file = SPECIALIST_TO_ARTIFACT.get(specialist_name)
            if output_file is None:
                # Render specialists (resume-renderer, etc.) write to documents/
                # rather than .apply-state/ — record nothing here.
                continue
            reader_entry = ARTIFACT_READERS.get(output_file)
            if reader_entry is None:
                continue
            kind, reader_fn = reader_entry

            try:
                data = reader_fn(state_dir)
            except Exception:  # noqa: BLE001 — one bad artifact must not abort the phase
                continue
            if data is None:
                continue

            cursor = conn.execute(
                "INSERT OR IGNORE INTO specialist_outputs "
                "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    specialist_name,
                    kind,
                    _serialize_artifact(data),
                    None,
                    finished_at,
                ),
            )
            inserted += cursor.rowcount
    return inserted


def _last_completed_phase(state_dir: Path) -> str:
    """Infer the last completed phase from manifest.json.

    Falls back to artifact-existence heuristics when manifest is absent
    so backfill still works on older app dirs.
    """
    manifest = _load_manifest(state_dir)
    if manifest is None:
        if (state_dir / "fit-score.json").exists():
            return "gather"
        if (state_dir / "jd-parsed.json").exists():
            return "parse"
        return _UNKNOWN_PHASE

    return _last_completed_phase_from_invocations(manifest)


def _completed_phases_from_invocations(manifest: dict) -> list[str]:
    """Return phases (gather → draft → render order) whose every required
    specialist has an ``ok`` row in ``manifest.invocations``.

    Real apply-pipeline manifests are flat ``invocations[]`` lists; the
    legacy ``manifest.phases`` shape never existed in production (roborev
    #921 MEDIUM regression for backfill).
    """
    # Local import: apply.py imports nothing from db_ingest, so the
    # boundary is one-way and no cycle exists.
    from jobsmith.apply import required_specialists_for_phase

    invocations = manifest.get("invocations")
    if not isinstance(invocations, list):
        return []
    ok_specialists = {
        inv.get("specialist")
        for inv in invocations
        if isinstance(inv, dict) and inv.get("status") == "ok"
    }
    completed: list[str] = []
    for phase in ("gather", "draft", "render"):
        required = required_specialists_for_phase(phase)
        if required and all(s in ok_specialists for s in required):
            completed.append(phase)
    return completed


def _last_completed_phase_from_invocations(manifest: dict) -> str:
    completed = _completed_phases_from_invocations(manifest)
    return completed[-1] if completed else _UNKNOWN_PHASE


def _backfill_run_id(slug: str) -> str:
    """Deterministic UUIDv5 so re-running backfill is a no-op."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"backfill:{slug}"))


def _state_timestamps(state_dir: Path) -> tuple[str | None, str | None]:
    try:
        dt = datetime.fromtimestamp(state_dir.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None, None
    iso = dt.isoformat()
    return iso, iso


def backfill_slug(
    conn: sqlite3.Connection,
    slug: str,
    applications_dir: Path,
) -> int:
    """Backfill one slug's .apply-state/ into the pipeline DB.

    Ingests EVERY completed phase (gather → draft → render) — earlier code
    only ingested the single ``last_completed_phase``, dropping all earlier
    phases' artifacts. Roborev #921 MEDIUM.

    Returns the total number of specialist_output rows inserted across all
    completed phases (0 if already backfilled).
    """
    state_dir = applications_dir / slug / ".apply-state"
    if not state_dir.is_dir():
        return 0

    run_id = _backfill_run_id(slug)
    started_at, finished_at = _state_timestamps(state_dir)

    manifest = _load_manifest(state_dir)
    if manifest is None:
        completed_phases = []
        last_phase = _last_completed_phase(state_dir)
    else:
        completed_phases = _completed_phases_from_invocations(manifest)
        last_phase = (
            completed_phases[-1] if completed_phases else _UNKNOWN_PHASE
        )

    conn.execute(
        "INSERT OR IGNORE INTO apply_runs "
        "(run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, last_phase, started_at, finished_at, _BACKFILL_STATUS),
    )
    conn.commit()

    existing = conn.execute(
        "SELECT COUNT(*) FROM specialist_outputs WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    if existing:
        return 0

    # Ingest every completed phase so earlier-phase artifacts also land.
    # When manifest is missing/legacy, fall back to the single-phase
    # heuristic so older app dirs still backfill something.
    phases_to_ingest = completed_phases or [last_phase]
    inserted = 0
    for phase in phases_to_ingest:
        if phase == _UNKNOWN_PHASE:
            continue
        inserted += ingest_phase_outputs(
            conn,
            slug=slug,
            run_id=run_id,
            phase=phase,
            state_dir=state_dir,
        )
    return inserted


def iter_backfillable_slugs(applications_dir: Path) -> list[str]:
    """List slugs under ``applications_dir`` that have an ``.apply-state/`` dir.

    Single source of truth for "what counts as a backfillable slug" — used
    by both ``backfill_all`` and dry-run preview.
    """
    if not applications_dir.is_dir():
        return []
    return [
        entry.name
        for entry in sorted(applications_dir.iterdir())
        if entry.is_dir()
        and not entry.name.startswith((".", "_"))
        and (entry / ".apply-state").is_dir()
    ]


def backfill_all(
    conn: sqlite3.Connection,
    applications_dir: Path,
) -> dict[str, int]:
    """Backfill every slug under ``applications_dir``.

    O(n) over the listing — each slug is one stat + one fixed-cost ingest
    call; n = number of app dirs.
    """
    return {
        slug: backfill_slug(conn, slug, applications_dir)
        for slug in iter_backfillable_slugs(applications_dir)
    }


__all__ = [
    "backfill_all",
    "backfill_slug",
    "ingest_phase_outputs",
    "iter_backfillable_slugs",
]
