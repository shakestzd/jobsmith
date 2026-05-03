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

from jobsmith._state_readers import ARTIFACT_READERS

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


def _phase_specialists(manifest: dict | None, phase: str) -> dict:
    """Walk ``manifest['phases'][phase]['specialists']`` defensively."""
    if not manifest:
        return {}
    phases = manifest.get("phases") or {}
    phase_data = phases.get(phase) or {}
    return phase_data.get("specialists") or {}


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

    Idempotent via INSERT OR IGNORE on (run_id, specialist, kind). Missing
    artifacts and unrecognised filenames are skipped silently. Reader errors
    are also swallowed so a single broken artifact does not block the phase.

    Returns the number of *new* rows inserted (skipped duplicates excluded).
    """
    specialists = _phase_specialists(_load_manifest(state_dir), phase)
    if not specialists:
        return 0

    inserted = 0
    finished_at = _now_iso()

    with conn:
        for specialist_name, spec_info in specialists.items():
            output_file = (
                spec_info.get("output") if isinstance(spec_info, dict) else None
            )
            reader_entry = ARTIFACT_READERS.get(output_file) if output_file else None
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

    phases = manifest.get("phases") or {}
    completed = [
        name
        for name, data in phases.items()
        if isinstance(data, dict) and data.get("status") == "complete"
    ]
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

    Returns the number of specialist_output rows inserted (0 if already
    backfilled, since the deterministic run_id + INSERT OR IGNORE makes
    re-runs no-ops).
    """
    state_dir = applications_dir / slug / ".apply-state"
    if not state_dir.is_dir():
        return 0

    run_id = _backfill_run_id(slug)
    started_at, finished_at = _state_timestamps(state_dir)
    phase = _last_completed_phase(state_dir)

    conn.execute(
        "INSERT OR IGNORE INTO apply_runs "
        "(run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, phase, started_at, finished_at, _BACKFILL_STATUS),
    )
    conn.commit()

    existing = conn.execute(
        "SELECT COUNT(*) FROM specialist_outputs WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    if existing:
        return 0

    return ingest_phase_outputs(
        conn,
        slug=slug,
        run_id=run_id,
        phase=phase,
        state_dir=state_dir,
    )


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
