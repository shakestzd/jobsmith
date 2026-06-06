"""jobsmith.reuse.store — typed read/write helpers for the four reuse tables.

Tables (created by migration 009_reuse_store):
  canonical_requirements     — parsed/canonicalized JD or master YAML outputs
  requirement_evidence_map   — traceability: which requirement maps to which evidence
  application_fingerprints   — per-slug content hash for change detection
  run_metrics                — per-run scalar metrics for AB testing / monitoring

Design decisions
----------------
- content_hash normalizes inputs before hashing so cosmetic whitespace/case
  edits to master YAML do NOT bust the cache; any real byte change does.
- is_fresh checks both the stored hash (content-based invalidation) and the
  row age against its TTL (time-based invalidation).
- All writes use INSERT OR REPLACE so the DB tolerates re-runs idempotently.
- Payloads are stored inline as TEXT (v1: single-user, low volume, atomic
  invalidation). Blob splitting is a future concern.
- Rows are keyed by content hash, so deleting the source application leaves
  orphaned rows that are unreachable but do not crash anything.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# content_hash — stable and sensitive
# ---------------------------------------------------------------------------


def _normalize_value(v: Any) -> Any:
    """Recursively strip and lowercase strings; recurse into dicts and lists."""
    if isinstance(v, str):
        return v.strip().lower()
    if isinstance(v, dict):
        return {_normalize_value(k): _normalize_value(val) for k, val in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_normalize_value(item) for item in v]
    return v


def content_hash(inputs: Any) -> str:
    """Return a stable hex SHA-256 over normalized *inputs*.

    Cosmetic whitespace (leading/trailing) and case changes produce the
    same hash.  Any real content change produces a different hash.
    """
    normalized = _normalize_value(inputs)
    serialized = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# is_fresh — hash + TTL check
# ---------------------------------------------------------------------------


def is_fresh(row: dict[str, Any] | sqlite3.Row, current_hash: str, ttl: timedelta) -> bool:
    """Return True when *row* is still valid for *current_hash* and *ttl*.

    A row is stale when either:
    - Its ``content_hash`` field differs from *current_hash* (real change), or
    - Its ``created_at`` timestamp is older than *ttl* ago.
    """
    stored_hash = row["content_hash"]
    if stored_hash != current_hash:
        return False

    created_raw = row["created_at"]
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError:
        return False

    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    age = datetime.now(tz=timezone.utc) - created
    return age <= ttl


# ---------------------------------------------------------------------------
# canonical_requirements
# ---------------------------------------------------------------------------


def upsert_canonical_requirement(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
    payload: str,
) -> None:
    """Insert or replace a row in ``canonical_requirements``."""
    conn.execute(
        "INSERT OR REPLACE INTO canonical_requirements "
        "(content_hash, payload, created_at) VALUES (?, ?, ?)",
        (content_hash, payload, datetime.now(tz=timezone.utc).isoformat()),
    )
    conn.commit()


def get_canonical_requirement(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
) -> sqlite3.Row | None:
    """Return the row for *content_hash* or None if not found."""
    return conn.execute(
        "SELECT * FROM canonical_requirements WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()


# ---------------------------------------------------------------------------
# requirement_evidence_map
# ---------------------------------------------------------------------------


def upsert_requirement_evidence(
    conn: sqlite3.Connection,
    *,
    requirement_hash: str,
    evidence_key: str,
    evidence_text: str,
) -> None:
    """Insert or replace a traceability row in ``requirement_evidence_map``."""
    conn.execute(
        "INSERT OR REPLACE INTO requirement_evidence_map "
        "(requirement_hash, evidence_key, evidence_text, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            requirement_hash,
            evidence_key,
            evidence_text,
            datetime.now(tz=timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def get_requirement_evidence(
    conn: sqlite3.Connection,
    *,
    requirement_hash: str,
    evidence_key: str,
) -> sqlite3.Row | None:
    """Return the evidence row or None if not found."""
    return conn.execute(
        "SELECT * FROM requirement_evidence_map "
        "WHERE requirement_hash = ? AND evidence_key = ?",
        (requirement_hash, evidence_key),
    ).fetchone()


# ---------------------------------------------------------------------------
# application_fingerprints
# ---------------------------------------------------------------------------


def upsert_application_fingerprint(
    conn: sqlite3.Connection,
    *,
    slug: str,
    content_hash: str,
) -> None:
    """Insert or replace the content fingerprint for *slug*."""
    conn.execute(
        "INSERT OR REPLACE INTO application_fingerprints "
        "(slug, content_hash, created_at) VALUES (?, ?, ?)",
        (slug, content_hash, datetime.now(tz=timezone.utc).isoformat()),
    )
    conn.commit()


def get_application_fingerprint(
    conn: sqlite3.Connection,
    *,
    slug: str,
) -> sqlite3.Row | None:
    """Return the fingerprint row for *slug* or None if not found."""
    return conn.execute(
        "SELECT * FROM application_fingerprints WHERE slug = ?",
        (slug,),
    ).fetchone()


# ---------------------------------------------------------------------------
# run_metrics
# ---------------------------------------------------------------------------


def upsert_run_metric(
    conn: sqlite3.Connection,
    *,
    slug: str,
    metric_key: str,
    metric_value: str,
) -> None:
    """Insert or replace a metric row for *slug* / *metric_key*."""
    conn.execute(
        "INSERT OR REPLACE INTO run_metrics "
        "(slug, metric_key, metric_value, created_at) VALUES (?, ?, ?, ?)",
        (slug, metric_key, metric_value, datetime.now(tz=timezone.utc).isoformat()),
    )
    conn.commit()


def get_run_metrics(
    conn: sqlite3.Connection,
    *,
    slug: str,
) -> list[sqlite3.Row]:
    """Return all metric rows for *slug*, ordered by metric_key."""
    return conn.execute(
        "SELECT * FROM run_metrics WHERE slug = ? ORDER BY metric_key",
        (slug,),
    ).fetchall()


__all__ = [
    "content_hash",
    "is_fresh",
    "upsert_canonical_requirement",
    "get_canonical_requirement",
    "upsert_requirement_evidence",
    "get_requirement_evidence",
    "upsert_application_fingerprint",
    "get_application_fingerprint",
    "upsert_run_metric",
    "get_run_metrics",
]
