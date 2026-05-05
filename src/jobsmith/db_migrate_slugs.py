"""One-shot migration: normalize pre-existing malformed slugs in the pipeline DB.

Background
----------
``feat-c63021d8`` added on-the-fly slug normalization on the *backfill* path
(see :func:`jobsmith.db_ingest.normalize_slug`). Rows that landed in
``apply_runs`` *before* that fix can still carry malformed slugs
(numeric-only, single-word, or a duplicated leading token).

This module rewrites those slugs in place. It is idempotent — running it
twice is a no-op on the second pass because already-clean slugs short-circuit
inside :func:`normalize_slug`.

Scope
-----
- Updates ``apply_runs.slug`` only. Foreign-key dependents
  (``specialist_outputs``, ``jd_records``) reference ``run_id``, not ``slug``,
  so they remain consistent automatically.
- Does **not** touch per-slug review databases at ``private/.review/<slug>.db``;
  those are separate files. Renaming review DBs is a filesystem operation
  outside the SQLite migration boundary.
- Does **not** rewrite ``private/url_index.json``. That file is regenerated
  on the next ``apply`` run.
"""

from __future__ import annotations

import sqlite3

from .db_ingest import normalize_slug


def find_malformed_slugs(conn: sqlite3.Connection) -> list[str]:
    """Return the distinct ``apply_runs`` slugs whose normalized form differs."""
    rows = conn.execute("SELECT DISTINCT slug FROM apply_runs").fetchall()
    out: list[str] = []
    for row in rows:
        slug = row[0]
        if not slug:
            continue
        normalized = normalize_slug(slug)
        if normalized != slug:
            out.append(slug)
    return out


def normalize_existing_slugs(conn: sqlite3.Connection) -> dict[str, str]:
    """Rewrite malformed slugs in ``apply_runs`` to their normalized form.

    Returns a mapping ``{old_slug: new_slug}`` of every slug that was rewritten.
    The returned map is empty when no malformed slugs are found, which makes
    re-running this function on the same database a no-op.

    The transformation is performed in a single transaction. On collision —
    where two malformed slugs normalize to the same target — the later row's
    slug stays as-is and is *not* included in the returned map.
    """
    malformed = find_malformed_slugs(conn)
    if not malformed:
        return {}

    rewritten: dict[str, str] = {}
    existing = {
        row[0]
        for row in conn.execute("SELECT DISTINCT slug FROM apply_runs").fetchall()
    }

    for old_slug in malformed:
        new_slug = normalize_slug(old_slug)
        if new_slug == old_slug:
            continue
        # Skip collisions — a clean slug already exists with this name.
        if new_slug in existing and new_slug not in rewritten.values():
            continue
        conn.execute(
            "UPDATE apply_runs SET slug = ? WHERE slug = ?",
            (new_slug, old_slug),
        )
        rewritten[old_slug] = new_slug
        existing.discard(old_slug)
        existing.add(new_slug)

    conn.commit()
    return rewritten


__all__ = ["find_malformed_slugs", "normalize_existing_slugs"]
