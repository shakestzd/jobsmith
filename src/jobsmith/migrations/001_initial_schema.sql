-- Pipeline DB schema for private/jobsmith.db
-- Applied once; tracked via schema_migrations table.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Migration tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Each apply run corresponds to one invocation of the pipeline for a slug.
-- A slug may have multiple runs (re-runs, partial runs).
CREATE TABLE IF NOT EXISTS apply_runs (
    run_id      TEXT PRIMARY KEY,
    slug        TEXT NOT NULL,
    phase       TEXT NOT NULL,           -- last completed phase, e.g. 'gather', 'draft'
    started_at  TEXT,                    -- ISO-8601 string; NULL = unknown (backfilled)
    finished_at TEXT,                    -- ISO-8601 string; NULL = in-flight
    status      TEXT NOT NULL            -- 'complete', 'in-progress', 'failed', 'backfilled'
);

CREATE INDEX IF NOT EXISTS idx_apply_runs_slug ON apply_runs(slug);

-- One row per specialist output within a run.
-- Composite uniqueness on (run_id, specialist, kind) — a specialist can only
-- produce one output of a given kind per run. This allows re-ingestion to be
-- detected and skipped cleanly.
--
-- output_json schema per kind:
--   jd-parsed         : { company, position, location, location_type, salary_range,
--                         req_id, apply_url, role_type, jd_text_clean,
--                         must_haves, nice_to_haves, top_keywords }
--   fit-score         : { score, score_raw, rationale, specialty, confidence,
--                         must_have_table, matched_evidence, concerns, pitch }
--   bullet-selection  : { positions, anchor_bullets_master, anchor_bullets_kept,
--                         anchor_bullets_dropped }
--   hm-snippet        : { detected, name, source, one_specific_signal, suggested_hook }
--   prose-draft       : { text }          -- full prose-draft text artifact
--   ai-tell-report    : { iterations }    -- list of {id, label, ...} audit iterations
--   ats-check         : { score, issues, suggestions }
--   company-research  : { text }          -- markdown text artifact
--   outreach-snippets : { text }          -- markdown text artifact
CREATE TABLE IF NOT EXISTS specialist_outputs (
    run_id          TEXT NOT NULL REFERENCES apply_runs(run_id) ON DELETE CASCADE,
    specialist      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    output_json     TEXT NOT NULL,        -- JSON-encoded typed payload (see above)
    transcript_ref  TEXT,                 -- optional path to transcript file
    finished_at     TEXT,                 -- ISO-8601; NULL = unknown (backfilled)
    PRIMARY KEY (run_id, specialist, kind)
);

CREATE INDEX IF NOT EXISTS idx_specialist_outputs_run_id ON specialist_outputs(run_id);
