-- Per-slug review DB schema for private/.review/<slug>.db
-- Applied once; tracked via schema_migrations table.
-- This DB lives OUTSIDE the application directory so app-dir sharing
-- (e.g., git push, zip export) does not leak personal review notes.

PRAGMA journal_mode=WAL;

-- Migration tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Proposed edits to a specialist's output.
-- amendment_id is UUID4 (NOT Python hash() — avoids collision on 32-bit hash).
-- run_id is nullable: amendments can be created before a run_id is known.
CREATE TABLE IF NOT EXISTS amendments (
    amendment_id TEXT PRIMARY KEY,
    slug         TEXT NOT NULL,
    run_id       TEXT,               -- NULL until linked to a specific run
    section      TEXT NOT NULL,      -- e.g. 'summary', 'experience', 'skills'
    op           TEXT NOT NULL,      -- 'replace', 'append', 'delete', 'insert'
    value        TEXT NOT NULL,      -- new content for this amendment
    status       TEXT NOT NULL,      -- 'pending', 'applied', 'rejected'
    created_at   TEXT NOT NULL       -- ISO-8601 timestamp
);

CREATE INDEX IF NOT EXISTS idx_amendments_slug ON amendments(slug);
CREATE INDEX IF NOT EXISTS idx_amendments_run_id ON amendments(run_id);

-- Chat sessions scoped to a slug.
CREATE TABLE IF NOT EXISTS chat_sessions (
    slug         TEXT NOT NULL,
    session_uuid TEXT NOT NULL,
    PRIMARY KEY (slug, session_uuid)
);

-- Chat messages scoped to a slug.
-- No session FK: messages are associated by slug; session filtering done in app.
CREATE TABLE IF NOT EXISTS chat_messages (
    slug       TEXT NOT NULL,
    role       TEXT NOT NULL,      -- 'user', 'assistant', 'system'
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL       -- ISO-8601 timestamp
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_slug ON chat_messages(slug);
