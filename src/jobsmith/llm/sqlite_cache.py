"""SQLite-backed LLM response cache (feat-ff4ccde2).

Caches per-specialist outputs keyed by ``(specialist, jd_hash, master_etag)``
so a re-run of an apply pipeline with unchanged JD + master content can skip
the expensive LLM phases. Cache reads/writes happen in
:mod:`jobsmith.core.pipeline` around each ``headless.run_phase`` call.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def jd_hash(jd_text: str) -> str:
    """Stable digest of normalized JD text used to key cache lookups."""
    return _sha256_hex((jd_text or "").strip())


def master_composite_etag(db: sqlite3.Connection) -> str:
    """sha256 over every ``master_content.content_blob`` row, ordered by section.

    Returns the empty-string etag when the table is empty so cache lookups
    short-circuit before any disk hit.
    """
    rows = db.execute(
        "SELECT section, content_blob FROM master_content ORDER BY section"
    ).fetchall()
    if not rows:
        return _sha256_hex("")
    h = hashlib.sha256()
    for section, blob in rows:
        h.update(section.encode("utf-8"))
        h.update(b"\x00")
        h.update((blob or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def cache_key(specialist: str, jd_hash_value: str, master_etag: str) -> str:
    """sha256 of ``"specialist:jd_hash:master_etag"`` — primary key for ``llm_cache``."""
    return _sha256_hex(f"{specialist}:{jd_hash_value}:{master_etag}")


def get_cached_phase(
    db: sqlite3.Connection,
    specialists: list[str],
    jd_hash_value: str,
    master_etag: str,
) -> dict[str, Any] | None:
    """Return ``{specialist: parsed_output_json}`` if every specialist hits.

    All-or-nothing: a single miss in *specialists* returns None and no
    counters are updated. On a full hit, ``hit_count`` is incremented and
    ``last_hit_at`` is bumped for every row touched.
    """
    if not specialists:
        return None
    keys = {sp: cache_key(sp, jd_hash_value, master_etag) for sp in specialists}
    placeholders = ",".join("?" for _ in keys)
    rows = db.execute(
        f"SELECT cache_key, specialist, output_json FROM llm_cache "  # noqa: S608
        f"WHERE cache_key IN ({placeholders})",
        list(keys.values()),
    ).fetchall()
    if len(rows) != len(specialists):
        return None
    by_key = {r[0]: r for r in rows}
    if any(k not in by_key for k in keys.values()):
        return None
    outputs: dict[str, Any] = {}
    now = _now_iso()
    for sp, key in keys.items():
        _, specialist, output_json = by_key[key]
        try:
            outputs[sp] = json.loads(output_json)
        except json.JSONDecodeError:
            return None
        db.execute(
            "UPDATE llm_cache SET hit_count = hit_count + 1, last_hit_at = ? "
            "WHERE cache_key = ?",
            (now, key),
        )
    db.commit()
    return outputs


def put_cached_phase(
    db: sqlite3.Connection,
    outputs: dict[str, Any],
    jd_hash_value: str,
    master_etag: str,
    model: str,
) -> None:
    """Upsert each ``specialist → output`` entry into ``llm_cache``.

    ``hit_count`` is preserved on conflict so freshness updates do not
    forget prior usage stats; ``last_hit_at`` is left untouched until the
    next read.
    """
    if not outputs:
        return
    now = _now_iso()
    for specialist, output in outputs.items():
        key = cache_key(specialist, jd_hash_value, master_etag)
        payload = json.dumps(output, sort_keys=True, default=str)
        db.execute(
            "INSERT INTO llm_cache "
            "(cache_key, specialist, jd_hash, master_etag, output_json, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "  output_json = excluded.output_json, "
            "  model       = excluded.model, "
            "  created_at  = excluded.created_at",
            (key, specialist, jd_hash_value, master_etag, payload, model, now),
        )
    db.commit()


def cache_stats(db: sqlite3.Connection) -> dict[str, int]:
    """Aggregate counters surfaced by the doctor/health endpoint."""
    row = db.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(hit_count), 0) AS hits FROM llm_cache"
    ).fetchone()
    if row is None:
        return {"total_entries": 0, "total_hits": 0}
    return {"total_entries": int(row[0]), "total_hits": int(row[1])}


def invalidate_all(db: sqlite3.Connection) -> int:
    """Delete every cache entry. Returns the number of rows removed."""
    cur = db.execute("DELETE FROM llm_cache")
    db.commit()
    return cur.rowcount or 0


__all__ = [
    "cache_key",
    "cache_stats",
    "get_cached_phase",
    "invalidate_all",
    "jd_hash",
    "master_composite_etag",
    "put_cached_phase",
]
