"""SQLite persistence layer for the jobsmith apply pipeline.

Two DB scopes
-------------
Pipeline DB  (project-wide)   ``private/jobsmith.db``
    Tables: apply_runs, specialist_outputs
    Canonical record of completed pipeline phases.

Review DB    (per-slug)        ``private/.review/<slug>.db``
    Tables: amendments, chat_sessions, chat_messages
    Outside the application directory so app-dir sharing does not leak
    personal review notes.

This module owns connections, schema migrations, and small DML helpers.
Heavier concerns live next door:

- :mod:`jobsmith.db_models`  — Pydantic types for ``output_json`` rows.
- :mod:`jobsmith.db_ingest`  — post-phase hook + backfill (file → DB).

Design decisions
----------------
- WAL journal mode + 30s busy_timeout on every connection.
- amendment_id is UUID4 (not Python ``hash()`` — fixes moplan dedup bug).
- Specialist ingestion is post-phase, NOT concurrent dual-write — the
  wrapper has no visibility into specialist subprocess writes during the
  phase. ``manifest.json`` is agent-authoritative for in-flight state;
  the DB is canonical for completed phases.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# db_models is committed with this module; db_ingest is its own module
# and callers should import it directly (jobsmith.db_ingest).
from jobsmith.db_models import (
    KIND_MODELS,
    AITellReport,
    ATSCheck,
    BulletSelection,
    FitScore,
    HMSnippet,
    JDParsed,
    TextArtifact,
    deserialize_output,
)

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_PIPELINE_MIGRATIONS = [("001_initial_schema", _MIGRATIONS_DIR / "001_initial_schema.sql")]
_REVIEW_MIGRATIONS = [
    ("001_review_schema", _MIGRATIONS_DIR / "001_review_schema.sql"),
    ("002_amendment_target", _MIGRATIONS_DIR / "002_amendment_target.sql"),
]
_BUSY_TIMEOUT_S = 30


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _run_migrations(
    conn: sqlite3.Connection,
    migrations: list[tuple[str, Path]],
) -> None:
    """Apply migrations not yet recorded in ``schema_migrations``."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, sql_path in migrations:
        if version in applied:
            continue
        conn.executescript(sql_path.read_text())
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, _now_iso()),
        )
        conn.commit()


def _open_db(
    db_path: Path,
    migrations: list[tuple[str, Path]],
    *,
    foreign_keys: bool = False,
) -> sqlite3.Connection:
    """Common connection setup: WAL, row factory, migrations applied."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=_BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    _run_migrations(conn, migrations)
    return conn


def open_pipeline_db(db_path: Path) -> sqlite3.Connection:
    """Open the project-wide pipeline DB at ``db_path`` (created if missing)."""
    return _open_db(db_path, _PIPELINE_MIGRATIONS, foreign_keys=True)


def open_review_db(slug: str, review_dir: Path) -> sqlite3.Connection:
    """Open the per-slug review DB at ``review_dir/<slug>.db``."""
    return _open_db(review_dir / f"{slug}.db", _REVIEW_MIGRATIONS)


def insert_apply_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    slug: str,
    phase: str,
    started_at: str | None,
    finished_at: str | None,
    status: str,
) -> None:
    """Insert into ``apply_runs``. Raises ``IntegrityError`` on duplicate run_id."""
    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, phase, started_at, finished_at, status),
    )
    conn.commit()


def get_apply_run_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    """Return the most-recent ``apply_runs`` row for ``slug`` (or None)."""
    return conn.execute(
        "SELECT * FROM apply_runs WHERE slug=? ORDER BY started_at DESC LIMIT 1",
        (slug,),
    ).fetchone()


def get_specialist_outputs(
    conn: sqlite3.Connection, run_id: str
) -> list[sqlite3.Row]:
    """Return all ``specialist_outputs`` rows for a run."""
    return conn.execute(
        "SELECT * FROM specialist_outputs WHERE run_id=?",
        (run_id,),
    ).fetchall()


def insert_specialist_output(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    specialist: str,
    kind: str,
    output_json: str,
    transcript_ref: str | None,
    finished_at: str | None,
) -> None:
    """Insert into ``specialist_outputs`` (idempotent on the composite PK)."""
    conn.execute(
        "INSERT OR IGNORE INTO specialist_outputs "
        "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, specialist, kind, output_json, transcript_ref, finished_at),
    )
    conn.commit()


def insert_amendment(
    conn: sqlite3.Connection,
    *,
    amendment_id: str,
    slug: str,
    run_id: str | None,
    section: str,
    op: str,
    value: str,
    status: str,
    target_index: int | None = None,
    target_field: str | None = None,
    created_at: str,
) -> None:
    """Insert into ``amendments``. Raises ``IntegrityError`` on duplicate PK."""
    conn.execute(
        "INSERT INTO amendments "
        "(amendment_id, slug, run_id, section, op, value, status, "
        "target_index, target_field, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            amendment_id,
            slug,
            run_id,
            section,
            op,
            value,
            status,
            target_index,
            target_field,
            created_at,
        ),
    )
    conn.commit()


def insert_chat_session(
    conn: sqlite3.Connection, slug: str, session_uuid: str
) -> None:
    """Idempotent insert into ``chat_sessions``."""
    conn.execute(
        "INSERT OR IGNORE INTO chat_sessions (slug, session_uuid) VALUES (?, ?)",
        (slug, session_uuid),
    )
    conn.commit()


def insert_chat_message(
    conn: sqlite3.Connection,
    *,
    slug: str,
    role: str,
    text: str,
    created_at: str,
) -> None:
    """Append a ``chat_messages`` row."""
    conn.execute(
        "INSERT INTO chat_messages (slug, role, text, created_at) VALUES (?, ?, ?, ?)",
        (slug, role, text, created_at),
    )
    conn.commit()


__all__ = [
    # Connection helpers
    "open_pipeline_db",
    "open_review_db",
    # Pipeline DB writers
    "get_apply_run_by_slug",
    "get_specialist_outputs",
    "insert_apply_run",
    "insert_specialist_output",
    # Review DB writers
    "insert_amendment",
    "insert_chat_message",
    "insert_chat_session",
    # Re-exports from db_models
    "AITellReport",
    "ATSCheck",
    "BulletSelection",
    "FitScore",
    "HMSnippet",
    "JDParsed",
    "KIND_MODELS",
    "TextArtifact",
    "deserialize_output",
]
