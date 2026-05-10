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
_PIPELINE_MIGRATIONS = [
    ("001_initial_schema", _MIGRATIONS_DIR / "001_initial_schema.sql"),
    ("003_artifact_versioning", _MIGRATIONS_DIR / "003_artifact_versioning.sql"),
    ("004_master_content", _MIGRATIONS_DIR / "004_master_content.sql"),
    ("005_apply_state", _MIGRATIONS_DIR / "005_apply_state.sql"),
    ("006_apply_state_log_run_id", _MIGRATIONS_DIR / "006_apply_state_log_run_id.sql"),
    ("007_users", _MIGRATIONS_DIR / "007_users.sql"),
    ("008_llm_cache", _MIGRATIONS_DIR / "008_llm_cache.sql"),
]
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


# ---------------------------------------------------------------------------
# apply_state helpers (trk-60217f9f / 0.8.4 — pipeline state DB-backed)
# ---------------------------------------------------------------------------
#
# Python-API counterparts to the ``jobsmith db get-state / put-state /
# list-state / reset-state`` CLI commands. apply.py uses these directly to
# avoid subprocess overhead; orchestrator + specialist agents use the CLI.
# Both paths target the same ``apply_state`` row set, so disk vs DB is no
# longer a question — the DB is the source of truth.


def put_state(
    conn: sqlite3.Connection,
    *,
    slug: str,
    kind: str,
    content_blob: str,
) -> None:
    """Upsert ``(slug, kind) -> content_blob`` into ``apply_state``.

    Replaces ``Write(applications/{slug}/.apply-state/{kind}.json, ...)``.
    """
    conn.execute(
        "INSERT OR REPLACE INTO apply_state (slug, kind, content_blob, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (slug, kind, content_blob, datetime.now(tz=timezone.utc).isoformat()),
    )
    conn.commit()


def get_state(
    conn: sqlite3.Connection, *, slug: str, kind: str
) -> str | None:
    """Return the ``content_blob`` for ``(slug, kind)`` or ``None`` if missing.

    Replaces ``Read(applications/{slug}/.apply-state/{kind}.json)``.
    """
    row = conn.execute(
        "SELECT content_blob FROM apply_state WHERE slug = ? AND kind = ?",
        (slug, kind),
    ).fetchone()
    return None if row is None else row["content_blob"]


def list_state(
    conn: sqlite3.Connection, *, slug: str
) -> list[tuple[str, str]]:
    """List ``(kind, updated_at)`` pairs for *slug*, alphabetical by kind."""
    rows = conn.execute(
        "SELECT kind, updated_at FROM apply_state WHERE slug = ? ORDER BY kind",
        (slug,),
    ).fetchall()
    return [(row["kind"], row["updated_at"]) for row in rows]


def append_state_log(
    conn: sqlite3.Connection,
    *,
    slug: str,
    payload: str,
    run_id: str | None = None,
) -> int:
    """Append one event row to ``apply_state_log`` and return its rowid.

    Replaces the per-line append into ``.apply-state/transcript.jsonl``
    (trk-60217f9f Pass 4). The supervisor's transcript tailer polls this
    table by ``id`` cursor instead of byte offset, so each row is delivered
    exactly once even across reconnects.

    *run_id* (optional, NULL for legacy callers) is the per-supervisor-run
    discriminator the tailer filters on (migration 006). New writes from
    the apply pipeline always populate it.
    """
    cursor = conn.execute(
        "INSERT INTO apply_state_log (slug, ts, payload, run_id) "
        "VALUES (?, ?, ?, ?)",
        (slug, datetime.now(tz=timezone.utc).isoformat(), payload, run_id),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def read_state_log(
    conn: sqlite3.Connection,
    *,
    slug: str | None = None,
    run_id: str | None = None,
    after_id: int = 0,
) -> list[tuple[int, str, str]]:
    """Return ``(id, ts, payload)`` rows with ``id > after_id``.

    Discriminators (apply in this order of preference):

    - *run_id* — the per-supervisor-run identifier (migration 006). When
      provided, only rows with matching ``run_id`` are returned. This is
      the supervisor's tailer path: ``run_id`` survives the orchestrator's
      ``rekey-slug`` step (which only mutates ``slug``) so the tailer
      keeps streaming through canonical-slug rekeys without mixing
      concurrent runs or replaying historical rows from prior
      applications (closes roborev job 954 HIGH).
    - *slug* — legacy filter for callers that have not yet adopted
      ``run_id`` (e.g. tools inspecting a single application).
    - both — intersection (rare; mostly tests).
    - neither — every row after the cursor (debug only).

    The supervisor's tailer calls this in a loop with the highest ``id``
    seen so far so each row is forwarded exactly once.
    """
    where = ["id > ?"]
    params: list[object] = [after_id]
    if run_id is not None:
        where.append("run_id = ?")
        params.append(run_id)
    if slug is not None:
        where.append("slug = ?")
        params.append(slug)
    sql = (
        "SELECT id, ts, payload FROM apply_state_log "
        f"WHERE {' AND '.join(where)} ORDER BY id"
    )
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [(int(row["id"]), row["ts"], row["payload"]) for row in rows]


def rekey_slug(
    conn: sqlite3.Connection, *, from_slug: str, to_slug: str
) -> tuple[int, int]:
    """Atomically move every ``apply_state`` and ``apply_state_log`` row from
    *from_slug* to *to_slug*. Returns ``(state_rows_moved, log_rows_moved)``.

    Used by the orchestrator after the JD parser derives the canonical slug
    (e.g. ``job-boards-7445224-2026-05`` → ``reddit-senior-analytics-engineer``)
    so subsequent reads under the canonical slug — including ``_phase_completed``
    in apply.py and ``ingest_phase_outputs`` in db_ingest — find the manifest,
    specs, and result envelopes the orchestrator already wrote (trk-60217f9f).

    On a primary-key collision (target slug already has the same kind) the
    target row wins and the source row is discarded; the caller is expected
    to invoke this exactly once at the slug-derivation point. Idempotent
    when ``from_slug == to_slug``: no-op, returns (0, 0).
    """
    if from_slug == to_slug:
        return (0, 0)
    with conn:  # implicit transaction so a partial move can roll back
        n_state = conn.execute(
            "SELECT COUNT(*) FROM apply_state WHERE slug = ?", (from_slug,)
        ).fetchone()[0]
        n_log = conn.execute(
            "SELECT COUNT(*) FROM apply_state_log WHERE slug = ?", (from_slug,)
        ).fetchone()[0]
        # Move rows one kind at a time so a target collision falls back to
        # "keep target, discard source" rather than aborting the whole move.
        rows = conn.execute(
            "SELECT kind, content_blob, updated_at FROM apply_state "
            "WHERE slug = ?",
            (from_slug,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO apply_state "
                "(slug, kind, content_blob, updated_at) VALUES (?, ?, ?, ?)",
                (to_slug, row["kind"], row["content_blob"], row["updated_at"]),
            )
        conn.execute("DELETE FROM apply_state WHERE slug = ?", (from_slug,))
        # Log rows have no PK collision risk (id is autoincrement).
        conn.execute(
            "UPDATE apply_state_log SET slug = ? WHERE slug = ?",
            (to_slug, from_slug),
        )
    return (int(n_state), int(n_log))


def reset_state(conn: sqlite3.Connection, *, slug: str) -> tuple[int, int]:
    """Delete every ``apply_state`` and ``apply_state_log`` row for *slug*.

    Returns ``(state_rows_deleted, log_rows_deleted)``. Idempotent.
    """
    n_state = conn.execute(
        "SELECT COUNT(*) FROM apply_state WHERE slug = ?", (slug,)
    ).fetchone()[0]
    n_log = conn.execute(
        "SELECT COUNT(*) FROM apply_state_log WHERE slug = ?", (slug,)
    ).fetchone()[0]
    conn.execute("DELETE FROM apply_state WHERE slug = ?", (slug,))
    conn.execute("DELETE FROM apply_state_log WHERE slug = ?", (slug,))
    conn.commit()
    return (n_state, n_log)


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


def get_latest_outputs_by_kind(
    conn: sqlite3.Connection, slug: str
) -> list[sqlite3.Row]:
    """Return the most-recent ``specialist_outputs`` row per ``kind`` for *slug*.

    Joins ``specialist_outputs`` to ``apply_runs`` and returns the best row
    per output ``kind``, where "best" is:

    1. Status priority: ``done`` > ``failed`` (a 'done' producer always
       beats a 'failed' producer for the same kind).
    2. Within the same status, latest ``finished_at`` wins (ties broken
       by ``started_at``).

    Status filter
    -------------
    ``running`` and ``cancelled`` runs are excluded — their outputs may be
    mid-write or partial. ``done`` and ``failed`` runs are both eligible,
    so a pipeline that fails late (e.g. render fails after gather + draft
    succeeded) still surfaces its valid early outputs in the review UI.

    Why this shape
    --------------
    A single-specialist re-run (slice 8) inserts a fresh ``apply_runs`` row
    with exactly one ``specialist_outputs`` entry. Reading outputs only
    from the most-recent run would erase every other section card. The
    per-kind aggregation keeps unchanged sections visible while surfacing
    the freshly re-run output (roborev #923 HIGH 1).

    The ``done`` > ``failed`` priority — instead of a hard ``status='done'``
    filter — handles two cases:

    * A newer failed re-run does not mask an older successful output for
      that kind (roborev #924 MEDIUM).
    * A run that fails partway through still shows the outputs of the
      phases that completed before the failure point (roborev #926
      partial-failure regression).
    """
    return conn.execute(
        """
        SELECT so.*
        FROM specialist_outputs so
        JOIN apply_runs ar ON ar.run_id = so.run_id
        WHERE ar.slug = ?
          AND ar.status IN ('done', 'failed')
          AND so.run_id = (
              SELECT so2.run_id
              FROM specialist_outputs so2
              JOIN apply_runs ar2 ON ar2.run_id = so2.run_id
              WHERE ar2.slug = ?
                AND ar2.status IN ('done', 'failed')
                AND so2.kind = so.kind
              ORDER BY
                  CASE ar2.status WHEN 'done' THEN 0 ELSE 1 END ASC,
                  COALESCE(ar2.finished_at, ar2.started_at) DESC,
                  ar2.started_at DESC
              LIMIT 1
          )
        """,
        (slug, slug),
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
    "get_latest_outputs_by_kind",
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
