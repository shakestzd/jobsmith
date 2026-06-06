"""jobsmith.reuse.backfill — idempotent backfill of reuse tables from existing apps.

Purpose
-------
Populate the four reuse tables (application_fingerprints, run_metrics,
canonical_requirements, requirement_evidence_map) from applications that were
applied *before* the reuse layer existed.  Today those tables only self-populate
on new ``jobsmith apply`` runs via ``_persist_reuse_tables``; the existing corpus
is invisible to the planner so reuse cannot fire against prior work.

This module mirrors the 3-step flow in ``_cli_apply._persist_reuse_tables``:
  1. JD fingerprint → ``application_fingerprints`` + ``run_metrics``
       via ``dedup.write_jd_fingerprint``
  2. Canonical requirements → ``canonical_requirements``
       via ``db_ingest.ingest_canonical_requirements``
  3. Bullet evidence map → ``requirement_evidence_map``
       via ``evidence_map.populate_from_bullet_selection``

Idempotency
-----------
All three underlying writes use INSERT OR IGNORE on their natural unique keys:
  - ``application_fingerprints``: (slug,) — unique index
  - ``run_metrics``: (slug, metric_key,) — unique index
  - ``canonical_requirements``: (content_hash,) — primary key
  - ``requirement_evidence_map``: (requirement_hash, evidence_key,) — unique index

Re-running on an already-populated DB is therefore a safe no-op.

Public API
----------
``backfill_slug_reuse(conn, slug, applications_dir) -> int``
    Backfill one slug. Returns total new rows inserted across all 3 steps.

``backfill_all_reuse(conn, applications_dir) -> dict[str, int]``
    Backfill every eligible slug. Returns {slug: rows_inserted}.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def _iter_eligible_slugs(applications_dir: Path) -> list[str]:
    """Return slugs under *applications_dir* that have a .apply-state/ directory.

    Skips entries starting with '.' or '_' (hidden dirs / templates).
    Mirrors ``db_ingest.iter_backfillable_slugs`` — single source of truth is
    intentionally NOT shared here to avoid importing db_ingest transitively.
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


def backfill_slug_reuse(
    conn: sqlite3.Connection,
    slug: str,
    applications_dir: Path,
) -> int:
    """Backfill reuse tables for one *slug*.

    Parameters
    ----------
    conn:
        Open SQLite connection to the pipeline DB (migration 009 applied).
    slug:
        Application directory name under *applications_dir*.
    applications_dir:
        Root of the applications tree (e.g. ``private/applications``).

    Returns
    -------
    int
        Total new rows inserted across application_fingerprints / run_metrics /
        canonical_requirements / requirement_evidence_map.
        Returns 0 when the slug directory does not exist or has no artifacts
        (safe no-op — idempotent re-runs also return 0).
    """
    state_dir = applications_dir / slug / ".apply-state"
    if not state_dir.is_dir():
        logger.debug("reuse-backfill: %s — .apply-state not found, skipping", slug)
        return 0

    inserted = 0

    # --- 1. JD fingerprint (application_fingerprints + run_metrics) ---
    inserted += _backfill_jd_fingerprint(conn, slug=slug, state_dir=state_dir)

    # --- 2. Canonical requirements ---
    inserted += _backfill_canonical_requirements(conn, state_dir=state_dir)

    # --- 3. Bullet evidence map ---
    inserted += _backfill_evidence_map(conn, state_dir=state_dir)

    return inserted


def backfill_all_reuse(
    conn: sqlite3.Connection,
    applications_dir: Path,
) -> dict[str, int]:
    """Backfill reuse tables for every eligible slug under *applications_dir*.

    Parameters
    ----------
    conn:
        Open SQLite connection to the pipeline DB.
    applications_dir:
        Root of the applications tree.

    Returns
    -------
    dict[str, int]
        Mapping of slug → rows inserted.  Empty dict when no eligible slugs exist.
    """
    slugs = _iter_eligible_slugs(applications_dir)
    return {
        slug: backfill_slug_reuse(conn, slug, applications_dir)
        for slug in slugs
    }


# ---------------------------------------------------------------------------
# Internal step helpers — each independently guarded (best-effort, never raise)
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    """Read and parse a JSON file, returning {} on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("reuse-backfill: could not load %s: %s", path, exc)
        return {}


def _backfill_jd_fingerprint(
    conn: sqlite3.Connection,
    *,
    slug: str,
    state_dir: Path,
) -> int:
    """Write JD fingerprint rows; returns the number of new rows inserted.

    ``write_jd_fingerprint`` populates BOTH ``application_fingerprints`` and a
    ``run_metrics`` normalized-text row, so the count spans both tables.
    Counting only ``application_fingerprints`` would under-report (and return 0
    when a prior partial backfill left the fingerprint present but the metrics
    row missing).
    """
    try:
        from jobsmith.reuse.dedup import write_jd_fingerprint

        jd_path = state_dir / "jd-parsed.json"
        if not jd_path.exists():
            return 0

        jd_parsed = _load_json(jd_path)
        fp_text = jd_parsed.get("jd_text_clean", "")
        if not fp_text or not fp_text.strip():
            return 0

        # Capture row counts across BOTH tables write_jd_fingerprint touches.
        def _counts() -> int:
            fp = conn.execute(
                "SELECT COUNT(*) FROM application_fingerprints WHERE slug = ?", (slug,)
            ).fetchone()[0]
            rm = conn.execute(
                "SELECT COUNT(*) FROM run_metrics WHERE slug = ?", (slug,)
            ).fetchone()[0]
            return fp + rm

        before = _counts()
        write_jd_fingerprint(conn, slug=slug, jd_text=fp_text)
        after = _counts()
        return max(0, after - before)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reuse-backfill: JD fingerprint failed for %s: %s", slug, exc)
        return 0


def _backfill_canonical_requirements(
    conn: sqlite3.Connection,
    *,
    state_dir: Path,
) -> int:
    """Ingest canonical requirements from jd-parsed.json; returns new row count."""
    try:
        from jobsmith.db_ingest import ingest_canonical_requirements

        jd_path = state_dir / "jd-parsed.json"
        if not jd_path.exists():
            return 0

        jd_parsed = _load_json(jd_path)
        if not jd_parsed:
            return 0

        return ingest_canonical_requirements(conn, jd_parsed=jd_parsed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reuse-backfill: canonical requirements failed for %s: %s", state_dir, exc)
        return 0


def _backfill_evidence_map(
    conn: sqlite3.Connection,
    *,
    state_dir: Path,
) -> int:
    """Populate evidence map from bullet-selection.json; returns new row count."""
    try:
        from jobsmith.reuse.evidence_map import populate_from_bullet_selection

        sel_path = state_dir / "bullet-selection.json"
        if not sel_path.exists():
            return 0

        selection = _load_json(sel_path)
        if not selection:
            return 0

        return populate_from_bullet_selection(conn, selection=selection)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reuse-backfill: evidence map failed for %s: %s", state_dir, exc)
        return 0


__all__ = [
    "backfill_all_reuse",
    "backfill_slug_reuse",
]
