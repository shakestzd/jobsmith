"""Master YAML → DB ingest for the DB-as-source-of-truth slice (feat-bf06bdea, S1).

The ``master_content`` table stores raw YAML blobs for each master section so
the runtime API can query the DB without touching the filesystem.

Public API
----------
ingest_master_from_disk(conn, *, content_dir, reload=False) -> int
    Load YAML files from *content_dir* into the ``master_content`` table.
    Returns the number of sections written (skips already-present sections
    unless *reload=True*).

ensure_master_loaded(db_path, *, repo_root, reload=False) -> None
    Convenience wrapper for the API startup hook.  Opens the DB, checks
    whether ``master_content`` is empty, and calls
    ``ingest_master_from_disk`` only when needed (or always if *reload*).
    Logs ``"Loaded N master sections from disk"`` at INFO level.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db
from jobsmith.paths import resolve

_log = logging.getLogger(__name__)

# Canonical section names and their config attribute names.
_SECTION_CONFIG_ATTRS: list[tuple[str, str]] = [
    ("work", "work_yml"),
    ("skill", "skill_yml"),
    ("education", "education_yml"),
    ("author", "author_yml"),
]


def _compute_etag(content_blob: str) -> str:
    """Return sha256(content_blob.encode('utf-8')).hexdigest()[:16]."""
    return hashlib.sha256(content_blob.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _resolve_section_paths(content_dir: Path) -> list[tuple[str, Path]]:
    """Return (section_name, yml_path) pairs assuming canonical filenames.

    Used as a fallback when no config-resolved paths are supplied.  Callers
    that have a parsed config should pass ``section_paths`` to
    :func:`ingest_master_from_disk` instead — see ultrareview bug_009.
    """
    return [
        ("work", content_dir / "work.yml"),
        ("skill", content_dir / "skill.yml"),
        ("education", content_dir / "education.yml"),
        ("author", content_dir / "author.yml"),
    ]


def _resolve_section_paths_from_config(config: Any, repo_root: Path) -> dict[str, Path]:  # noqa: ANN401
    """Return {section: resolved Path} from ``config.master.*_yml``.

    Honors users who customize filenames (e.g.
    ``master.work_yml: assets/content/myresume-work.yml``).  Closes
    ultrareview bug_009.
    """
    return {
        section: resolve(getattr(config.master, attr), repo_root)
        for section, attr in _SECTION_CONFIG_ATTRS
    }


def ingest_master_from_disk(
    conn: sqlite3.Connection,
    *,
    content_dir: Path | None = None,
    section_paths: dict[str, Path] | None = None,
    reload: bool = False,
) -> int:
    """Load master YAML files into ``master_content``.

    Parameters
    ----------
    conn:
        Open pipeline DB connection (``master_content`` table must exist).
    content_dir:
        Legacy: directory containing canonical-named files
        (``work.yml``/``skill.yml``/etc.).  Used only when ``section_paths``
        is not supplied.
    section_paths:
        Preferred: ``{section: full path}`` mapping (typically derived from
        ``config.master.*_yml``).  Honors users who customize filenames.
    reload:
        When True, replace existing rows.  When False (default), skip
        sections that already have a row.

    Returns
    -------
    int
        Number of sections written or updated.
    """
    if section_paths is None:
        if content_dir is None:
            raise ValueError("ingest_master_from_disk needs section_paths or content_dir")
        sections_iter: list[tuple[str, Path]] = _resolve_section_paths(content_dir)
    else:
        sections_iter = list(section_paths.items())

    written = 0
    loaded_at = _now_iso()

    for section, yml_path in sections_iter:
        if not yml_path.exists():
            _log.debug("master_ingest: %s not found, skipping section %r", yml_path, section)
            continue

        # Without reload, skip sections that already have a row.
        if not reload:
            existing = conn.execute(
                "SELECT 1 FROM master_content WHERE section = ?",
                (section,),
            ).fetchone()
            if existing is not None:
                continue

        content_blob = yml_path.read_text(encoding="utf-8")
        etag = _compute_etag(content_blob)

        conn.execute(
            "INSERT OR REPLACE INTO master_content "
            "(section, content_blob, etag, loaded_at) VALUES (?, ?, ?, ?)",
            (section, content_blob, etag, loaded_at),
        )
        written += 1

    if written:
        conn.commit()

    return written


def ensure_master_loaded(
    db_path: Path,
    *,
    repo_root: Path,
    reload: bool = False,
) -> None:
    """Startup hook: populate ``master_content`` if empty (or if *reload*).

    Called by the FastAPI lifespan handler during ``jobsmith api serve``.
    Reads ``master.{work,skill,education,author}_yml`` from
    ``.apply-config.yaml`` so users who rename files (e.g. ``work_yml:
    assets/content/myresume-work.yml``) still get their content ingested.
    Falls back to canonical filenames in ``<repo_root>/assets/content/``
    only when no config is found or config parsing fails.

    Logs ``"Loaded N master sections from disk"`` at INFO when any rows
    are written.
    """
    conn = open_pipeline_db(db_path)
    try:
        if not reload:
            count = conn.execute(
                "SELECT COUNT(*) FROM master_content"
            ).fetchone()[0]
            if count > 0:
                _log.debug(
                    "master_ingest: master_content already has %d row(s), skipping load",
                    count,
                )
                return

        section_paths = _resolve_section_paths_from_config_or_default(repo_root)
        n = ingest_master_from_disk(conn, section_paths=section_paths, reload=reload)
        _log.info("Loaded %d master sections from disk", n)
    finally:
        conn.close()


def _resolve_section_paths_from_config_or_default(
    repo_root: Path,
) -> dict[str, Path]:
    """Resolve {section: path} from config; fall back to canonical defaults.

    Falls back to ``<repo_root>/assets/content/<section>.yml`` when no config
    is found or config parsing fails (the warning log makes the cause
    visible to operators).
    """
    config_path = find_config(repo_root)
    default_dir = repo_root / "assets" / "content"
    if config_path is None:
        return {
            section: default_dir / f"{section}.yml"
            for section, _ in _SECTION_CONFIG_ATTRS
        }
    try:
        config = load_config(path=config_path)
        return _resolve_section_paths_from_config(config, repo_root)
    except Exception:
        _log.warning(
            "master_ingest: could not parse config — falling back to canonical filenames in %s",
            default_dir,
            exc_info=True,
        )
        return {
            section: default_dir / f"{section}.yml"
            for section, _ in _SECTION_CONFIG_ATTRS
        }


__all__ = [
    "ensure_master_loaded",
    "ingest_master_from_disk",
]
