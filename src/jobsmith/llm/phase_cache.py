"""Phase-level cache hooks called from :mod:`jobsmith.core.pipeline`.

These wrap :mod:`jobsmith.llm.sqlite_cache` with the bookkeeping required to
plug into the apply pipeline:
- read the jd-parsed.json artifact to derive a stable JD hash,
- replay cached specialist outputs into ``specialist_outputs`` on a hit,
- snapshot fresh outputs back into the cache after a successful phase.

All entry points degrade silently on errors so a misbehaving cache never
blocks an apply run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from jobsmith._state_readers import PHASE_SPECIALISTS, SPECIALIST_TO_ARTIFACT
from jobsmith.llm import sqlite_cache as _cache

logger = logging.getLogger(__name__)

# gather emits the jd-parsed artifact that downstream phases hash against —
# caching its own output would create a chicken-and-egg dependency.
_CACHEABLE_PHASES = {"draft", "render"}


def _read_jd_text(state_dir: Path) -> str | None:
    """Return the canonical JD body for hashing, or None when unavailable."""
    jd_file = state_dir / "jd-parsed.json"
    if not jd_file.exists():
        return None
    try:
        data = json.loads(jd_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    text = data.get("jd_text_clean") or data.get("jd_text") or ""
    return str(text)


def _phase_specialists(phase: str) -> list[str]:
    return list(PHASE_SPECIALISTS.get(phase, ()))


def try_replay_phase(
    db: sqlite3.Connection,
    *,
    run_id: str,
    phase: str,
    state_dir: Path,
) -> bool:
    """Replay cached specialist outputs into the DB if every specialist hits.

    Returns True when a full replay happened (caller should skip running the
    real phase). Returns False on any miss, missing JD, or DB error.
    """
    if phase not in _CACHEABLE_PHASES:
        return False
    specialists = _phase_specialists(phase)
    if not specialists:
        return False
    jd_text = _read_jd_text(state_dir)
    if jd_text is None:
        return False
    try:
        jd_hash_value = _cache.jd_hash(jd_text)
        master_etag = _cache.master_composite_etag(db)
        outputs = _cache.get_cached_phase(db, specialists, jd_hash_value, master_etag)
    except sqlite3.Error:
        logger.debug("llm_cache lookup failed", exc_info=True)
        return False
    if outputs is None:
        return False

    # Replay into specialist_outputs so downstream readers see the rows
    # immediately, mirroring what db_ingest would write.
    try:
        for specialist, output in outputs.items():
            kind_key = SPECIALIST_TO_ARTIFACT.get(specialist, "")
            kind = kind_key.split(".")[0] if kind_key else specialist
            db.execute(
                "INSERT OR IGNORE INTO specialist_outputs "
                "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
                "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (run_id, specialist, kind, json.dumps(output), None),
            )
        db.commit()
    except sqlite3.Error:
        logger.debug("llm_cache replay write failed", exc_info=True)
        return False
    return True


def save_phase_outputs(
    db: sqlite3.Connection,
    *,
    run_id: str,
    phase: str,
    state_dir: Path,
    model: str = "claude-sonnet",
) -> None:
    """After a successful phase, snapshot specialist_outputs into the cache."""
    if phase not in _CACHEABLE_PHASES:
        return
    specialists = _phase_specialists(phase)
    if not specialists:
        return
    jd_text = _read_jd_text(state_dir)
    if jd_text is None:
        return
    try:
        rows = db.execute(
            "SELECT specialist, output_json FROM specialist_outputs "
            "WHERE run_id = ? AND specialist IN ({}) ".format(  # noqa: S608
                ",".join("?" for _ in specialists)
            ),
            (run_id, *specialists),
        ).fetchall()
    except sqlite3.Error:
        logger.debug("llm_cache snapshot read failed", exc_info=True)
        return
    if not rows:
        return
    outputs: dict[str, Any] = {}
    for specialist, output_json in rows:
        try:
            outputs[specialist] = json.loads(output_json)
        except (TypeError, json.JSONDecodeError):
            continue
    if not outputs:
        return
    try:
        jd_hash_value = _cache.jd_hash(jd_text)
        master_etag = _cache.master_composite_etag(db)
        _cache.put_cached_phase(db, outputs, jd_hash_value, master_etag, model)
    except sqlite3.Error:
        logger.debug("llm_cache snapshot write failed", exc_info=True)


__all__ = ["save_phase_outputs", "try_replay_phase"]
