"""jobsmith.core.manifest — manifest helpers (feat-55152c31, Slice 2d).

Pure helpers with no Rich/Click/Typer dependencies. Relocated from apply.py.
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Phase → required-specialist mapping
# ---------------------------------------------------------------------------

# Specialists whose successful invocation marks each phase as complete.
# A specialist is considered "done" when manifest.json.invocations contains an
# entry with that ``specialist`` name and ``status`` == "ok".
PHASE_REQUIRED_SPECIALISTS: dict[str, tuple[str, ...]] = {
    "gather": (
        "apply-jd-parser",
        "apply-fit-scorer",
        "apply-hm-enricher",
        "apply-bullet-selector",
        "apply-company-research",
    ),
    "draft": (
        "apply-prose-writer",
        "apply-prose-qa",
    ),
    "render": (
        "apply-resume-renderer",
        "apply-cover-letter-writer",
        "apply-index-writer",
    ),
}


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def load_manifest(app_dir: Path, cwd: Path) -> dict | None:
    """Read the manifest blob for ``app_dir.name`` from ``apply_state`` (DB).

    Pass 2 of trk-60217f9f made the DB the source of truth, but pre-0.8.4
    applications still have only ``app_dir/.apply-state/manifest.json``
    on disk. Roborev job 962 MEDIUM caught the regression: those apps
    would no longer be recognised as resumable and would silently rerun
    from scratch when a user re-applied to the same URL.

    Read order:

    1. ``apply_state`` row, ``slug = app_dir.name``, ``kind = "manifest"``.
    2. Disk fallback at ``app_dir/.apply-state/manifest.json`` when the DB
       row is missing. The disk file is treated as authoritative input
       only (the orchestrator writes new manifests to the DB exclusively
       via Pass 2's prompts), so reads here cover the migration window.

    Returns ``None`` when neither source has a usable dict.
    """
    from jobsmith.core.paths import pipeline_db_path
    from jobsmith.db import get_state, open_pipeline_db

    db_path = pipeline_db_path(cwd)
    blob: str | None = None
    if db_path is not None and db_path.exists():
        slug = app_dir.name
        conn = open_pipeline_db(db_path)
        try:
            blob = get_state(conn, slug=slug, kind="manifest")
        finally:
            conn.close()
    if not blob:
        # Disk fallback for pre-0.8.4 applications (no DB-backed manifest
        # was ever written). Returns None on missing or malformed file.
        manifest_path = app_dir / ".apply-state" / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            blob = manifest_path.read_text(encoding="utf-8")
        except OSError:
            return None
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Phase completion check
# ---------------------------------------------------------------------------


def inject_skipped_specialists(
    manifest: dict,
    specialists: list[str],
) -> dict:
    """Append synthetic ``status=ok, action=skipped`` invocations to *manifest*.

    Used by the pipeline orchestrator when cover-letter generation is disabled:
    the two CL-only specialists (``apply-company-research``,
    ``apply-cover-letter-writer``) are never dispatched, but the manifest still
    needs ``status=ok`` entries for them so :func:`phase_completed` returns
    ``True`` and the phase loop does not try to re-run them.

    Idempotent — if an entry for a specialist already exists in
    ``invocations``, no duplicate is added.

    Parameters
    ----------
    manifest:
        The manifest dict to mutate/augment.  If it has no ``"invocations"``
        key one is created.
    specialists:
        Names of the specialists to inject skipped entries for.

    Returns
    -------
    dict
        The same *manifest* dict with the new invocations appended.
    """
    if "invocations" not in manifest or not isinstance(manifest.get("invocations"), list):
        manifest["invocations"] = []
    existing = {
        inv.get("specialist")
        for inv in manifest["invocations"]
        if isinstance(inv, dict) and inv.get("status") == "ok"
    }
    for name in specialists:
        if name not in existing:
            manifest["invocations"].append(
                {"specialist": name, "status": "ok", "action": "skipped"}
            )
    return manifest


def phase_completed(manifest: dict | None, phase_name: str) -> bool:
    """Return True iff every required specialist for *phase_name* is done.

    "Done" means the manifest's ``invocations`` list contains at least one
    entry per required specialist with ``status == "ok"``.  Missing manifest
    or malformed invocations always return False — callers re-run the phase.
    """
    if not manifest:
        return False
    required = PHASE_REQUIRED_SPECIALISTS.get(phase_name, ())
    if not required:
        return False
    invocations = manifest.get("invocations")
    if not isinstance(invocations, list):
        return False
    completed_specialists = {
        inv.get("specialist")
        for inv in invocations
        if isinstance(inv, dict) and inv.get("status") == "ok"
    }
    return all(spec in completed_specialists for spec in required)
