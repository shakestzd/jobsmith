"""jobsmith.reuse.evidence_map — requirement→bullet mapping helpers.

Purpose
-------
This module populates and queries ``requirement_evidence_map`` to implement
bullet-selection reuse: when a new JD requirement resolves (via
``reuse.match``) to a prior canonical requirement, we can skip re-selection
and reuse the previously chosen master bullet — but ONLY if its current text
hash still matches the stored hash.

Column mapping (requirement_evidence_map table)
-----------------------------------------------
  requirement_hash  TEXT — content_hash of the canonical requirement payload
                          (same key used in ``canonical_requirements.content_hash``)
  evidence_key      TEXT — master_bullet_id: 12-char SHA-1 hex of bullet text
                          (mirrors ``guard._bullet_id`` / ``guard.Bullet.bullet_id``)
  evidence_text     TEXT — content_hash of the bullet's text at mapping time
                          (from ``store.content_hash``); acts as a version token.

Freshness / invalidation rule
------------------------------
A mapping row is VALID when::

    content_hash(current_master_bullet_text) == stored evidence_text

Any real content change to the bullet text produces a different hash, which
invalidates the mapping and forces regeneration (re-selection).  Cosmetic
whitespace or case changes produce the same hash and do NOT invalidate.

This is intentionally the same mechanism as ``store.is_fresh`` but without
the TTL component — bullet mappings are content-stable, not time-decayed.
Slice 7 (warm-start) reads the lookup helper; keep the signature stable.

Public API
----------
``populate_from_bullet_selection(conn, *, selection) -> int``
    Reads a parsed bullet-selection.json dict and writes one mapping row per
    bullet that has a ``matched_requirement_hash`` field AND ``included=True``.
    Idempotent via INSERT OR IGNORE.  Returns count of NEW rows inserted.

``lookup_mapped_bullet(conn, *, requirement_hash, current_bullet_texts) -> str | None``
    For a given canonical requirement hash, finds all mapping rows and returns
    the first ``evidence_key`` (master_bullet_id) whose stored ``evidence_text``
    still matches ``content_hash(current_bullet_texts[evidence_key])``.
    Returns ``None`` when: no row exists, no bullet_id in current_bullet_texts,
    or all stored hashes are stale (content changed → regenerate path).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from jobsmith.reuse.store import content_hash


def populate_from_bullet_selection(
    conn: sqlite3.Connection,
    *,
    selection: dict,
) -> int:
    """Populate ``requirement_evidence_map`` from a parsed bullet-selection dict.

    Parameters
    ----------
    conn:
        Open SQLite connection to the pipeline DB.
    selection:
        Parsed ``bullet-selection.json`` payload.  Expected shape::

            {
                "positions": [
                    {
                        "company": str,
                        "title": str,
                        "bullets": [
                            {
                                "master_bullet_id": str,       # 12-char SHA-1 hex
                                "text": str,                   # bullet text
                                "included": bool,
                                "matched_requirement_hash": str | absent,
                            },
                            ...
                        ],
                    },
                    ...
                ],
            }

        Bullets without ``matched_requirement_hash`` or with ``included=False``
        are silently skipped.

    Returns
    -------
    int
        Number of NEW rows inserted (0 when all rows already present —
        idempotent via INSERT OR IGNORE).
    """
    now = datetime.now(tz=timezone.utc).isoformat()
    inserted = 0

    with conn:
        for position in selection.get("positions", []):
            for bullet in position.get("bullets", []):
                req_hash = bullet.get("matched_requirement_hash")
                if not req_hash:
                    continue
                if not bullet.get("included", True):
                    continue

                bullet_id = bullet.get("master_bullet_id")
                bullet_text = bullet.get("text", "")
                if not bullet_id or not bullet_text:
                    continue

                bullet_hash = content_hash(bullet_text)

                cursor = conn.execute(
                    "INSERT OR IGNORE INTO requirement_evidence_map "
                    "(requirement_hash, evidence_key, evidence_text, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (req_hash, bullet_id, bullet_hash, now),
                )
                inserted += cursor.rowcount

    return inserted


def lookup_mapped_bullet(
    conn: sqlite3.Connection,
    *,
    requirement_hash: str,
    current_bullet_texts: dict[str, str],
) -> str | None:
    """Return a fresh master_bullet_id for *requirement_hash*, or None.

    Parameters
    ----------
    conn:
        Open SQLite connection to the pipeline DB.
    requirement_hash:
        content_hash of the canonical requirement payload — the same value
        stored in ``requirement_evidence_map.requirement_hash``.
    current_bullet_texts:
        Mapping of ``{master_bullet_id: current_text}`` for all bullets that
        are currently loaded from master YAML / DB.  Used to validate that
        the stored hash still matches the bullet's current content.

    Returns
    -------
    str | None
        The ``master_bullet_id`` (evidence_key) of the first stored mapping
        row whose ``evidence_text`` equals
        ``content_hash(current_bullet_texts[evidence_key])``.

        Returns ``None`` when:
        - No mapping row exists for *requirement_hash*.
        - The stored bullet_id is not in *current_bullet_texts* (bullet removed
          from master — treat as invalidated).
        - The stored ``evidence_text`` differs from the current bullet's hash
          (bullet was edited → regenerate path).

    Notes
    -----
    Slice 7 (warm-start delta computation) calls this function to identify
    which requirements already have fresh bullet mappings vs which need
    re-selection.  Keep the signature and semantics stable.
    """
    if not current_bullet_texts:
        return None

    rows = conn.execute(
        "SELECT evidence_key, evidence_text "
        "FROM requirement_evidence_map "
        "WHERE requirement_hash = ?",
        (requirement_hash,),
    ).fetchall()

    for row in rows:
        bullet_id = row["evidence_key"] if isinstance(row, sqlite3.Row) else row[0]
        stored_hash = row["evidence_text"] if isinstance(row, sqlite3.Row) else row[1]

        current_text = current_bullet_texts.get(bullet_id)
        if current_text is None:
            continue

        if content_hash(current_text) == stored_hash:
            return bullet_id

    return None


__all__ = [
    "lookup_mapped_bullet",
    "populate_from_bullet_selection",
]
