-- Per-slug pipeline state — replaces .apply-state/ on-disk files
-- (trk-eb70f385, eliminates file-system reads/writes from specialists).
--
-- Two tables:
--
-- 1. apply_state — keyed on (slug, kind). Holds the latest blob for each
--    artifact (manifest, spec, jd-parsed, fit-score, bullet-selection, ...).
--    Slug-keyed so it persists across re-runs of the same slug, supporting
--    resume semantics (orchestrator reads prior result envelopes to decide
--    which specialists to skip).
--
-- 2. apply_state_log — append-only event stream. Replaces transcript.jsonl.
--    Rows are read by the supervisor and forwarded as event=transcript SSE
--    messages (bug-0e13706c). Polled incrementally by max(rowid).
--
-- Naming: 'kind' is the canonical artifact name minus the file extension
-- (e.g. "manifest" not "manifest.json", "jd-parsed" not "jd-parsed.json").
--
-- Lifecycle: rows survive across runs; an explicit 'jobsmith db reset-state
-- --slug <s>' wipes them when the user wants a clean slate (equivalent to
-- the prior 'rm -rf applications/{slug}/.apply-state').

CREATE TABLE IF NOT EXISTS apply_state (
    slug         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    content_blob TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (slug, kind)
);

CREATE INDEX IF NOT EXISTS idx_apply_state_slug ON apply_state(slug);

CREATE TABLE IF NOT EXISTS apply_state_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    slug    TEXT NOT NULL,
    ts      TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_apply_state_log_slug_id
    ON apply_state_log(slug, id);
