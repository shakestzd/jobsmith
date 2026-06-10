-- Migration 010: postings + sourcing_runs tables
--
-- postings: one row per unique job posting discovered from any source.
--   - dedup_key: stable hash of normalized URL or external-id — used as the
--     upsert key so re-crawled/re-ingested postings update rather than duplicate.
--   - status: sourced | queued | dismissed | promoted | expired
--   - promoted_application_id: FK to apply_runs.run_id; set by promote().
--   - Re-sight semantics: last_seen_at is bumped on upsert, but a posting
--     whose status is dismissed/promoted/expired is NEVER reset to sourced.
--
-- sourcing_runs: one row per crawl/ingest cycle.
--   - Purge policy: keep last 90 runs (enforced in Python, not SQL trigger).
--   - Consumed by the scheduling/health slice (feat-80affa8a).

CREATE TABLE IF NOT EXISTS postings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT NOT NULL,          -- e.g. 'greenhouse/stripe', 'gmail/linkedin-alert'
    external_id             TEXT,                   -- ATS/board-specific ID (may be NULL for email alerts)
    url                     TEXT,
    title                   TEXT,
    company                 TEXT,
    location                TEXT,
    comp_text               TEXT,
    posted_date             TEXT,
    jd_text                 TEXT,
    fast_score              REAL,
    llm_score               REAL,
    specialty               TEXT,
    rationale               TEXT,
    evidence_json           TEXT,                   -- JSON array of evidence strings
    status                  TEXT NOT NULL DEFAULT 'sourced'
                                CHECK (status IN ('sourced','queued','dismissed','promoted','expired')),
    promoted_application_id TEXT REFERENCES apply_runs(run_id) ON DELETE SET NULL,
    dedup_key               TEXT NOT NULL UNIQUE,   -- SHA-256 of normalized URL or external-id
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_postings_status   ON postings(status);
CREATE INDEX IF NOT EXISTS idx_postings_source   ON postings(source);
CREATE INDEX IF NOT EXISTS idx_postings_dedup    ON postings(dedup_key);

-- sourcing_runs: bookkeeping for each crawl / ingest cycle.
CREATE TABLE IF NOT EXISTS sourcing_runs (
    run_id                  TEXT PRIMARY KEY,
    started_at              TEXT NOT NULL,
    finished_at             TEXT,
    status                  TEXT NOT NULL DEFAULT 'running'
                                CHECK (status IN ('running','done','failed','degraded')),
    new_count               INTEGER NOT NULL DEFAULT 0,
    updated_count           INTEGER NOT NULL DEFAULT 0,
    skipped_count           INTEGER NOT NULL DEFAULT 0,
    degraded_sources_json   TEXT,                   -- JSON list of source names that errored
    error                   TEXT
);

CREATE INDEX IF NOT EXISTS idx_sourcing_runs_started ON sourcing_runs(started_at);
