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
    """Return (section_name, yml_path) pairs for each canonical section.

    Uses *content_dir* directly (e.g. assets/content/) to locate files by
    their conventional names rather than parsing .apply-config.yaml, so
    callers that already know the content directory can use this helper
    without needing a config file.
    """
    return [
        ("work", content_dir / "work.yml"),
        ("skill", content_dir / "skill.yml"),
        ("education", content_dir / "education.yml"),
        ("author", content_dir / "author.yml"),
    ]


def ingest_master_from_disk(
    conn: sqlite3.Connection,
    *,
    content_dir: Path,
    reload: bool = False,
) -> int:
    """Load master YAML files from *content_dir* into ``master_content``.

    Parameters
    ----------
    conn:
        Open pipeline DB connection (``master_content`` table must exist).
    content_dir:
        Directory containing ``work.yml``, ``skill.yml``, ``education.yml``,
        ``author.yml``.
    reload:
        When True, replace existing rows (``INSERT OR REPLACE``).
        When False (default), skip sections that already have a row.

    Returns
    -------
    int
        Number of sections written or updated.
    """
    written = 0
    loaded_at = _now_iso()

    for section, yml_path in _resolve_section_paths(content_dir):
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
    Resolves *content_dir* from the ``master.*_yml`` config paths relative
    to *repo_root*.  Uses the first YAML file's parent as the content dir
    (all four canonical files are expected to live in the same directory).

    Logs ``"Loaded N master sections from disk"`` at INFO when any rows
    are written; no-ops silently when the table is already populated.
    """
    conn = open_pipeline_db(db_path)
    try:
        # Check if table already has rows (skip load when populated, unless reload)
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

        # Resolve content_dir from config or use conventional default
        content_dir = _resolve_content_dir(repo_root)
        n = ingest_master_from_disk(conn, content_dir=content_dir, reload=reload)
        _log.info("Loaded %d master sections from disk", n)
    finally:
        conn.close()


def _resolve_content_dir(repo_root: Path) -> Path:
    """Return the content directory by reading .apply-config.yaml.

    Falls back to ``<repo_root>/assets/content/`` when no config is found
    or when config parsing fails.
    """
    config_path = find_config(repo_root)
    if config_path is None:
        # No config — use conventional default
        return repo_root / "assets" / "content"
    try:
        config = load_config(path=config_path)
        work_path = resolve(config.master.work_yml, repo_root)
        return work_path.parent
    except Exception:
        _log.debug("master_ingest: could not parse config, using default content dir", exc_info=True)
        return repo_root / "assets" / "content"


__all__ = [
    "ensure_master_loaded",
    "ingest_master_from_disk",
]
