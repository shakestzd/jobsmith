-- Migration 009: reuse store tables
--
-- Creates four tables for the apply-pipeline reuse/caching layer.
-- All tables are idempotent (CREATE TABLE IF NOT EXISTS) and designed for
-- INSERT OR REPLACE writes under WAL journal mode.
--
-- Rows are keyed by content hash so they survive source-application deletion
-- without causing integrity errors (orphaned rows are unreachable, not crashing).
--
-- NOTE: company research reuses the existing file cache (slice 3); no company
-- table is created here.  llm_cache (migration 008) is NOT replaced — this
-- layer coexists alongside it.

-- canonical_requirements: parsed/canonicalized outputs keyed by input hash.
-- Consumers: canonicalization/match (slice 2), planner warm-start (slice 7).
CREATE TABLE IF NOT EXISTS canonical_requirements (
    content_hash  TEXT PRIMARY KEY,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- requirement_evidence_map: traceability between requirements and supporting
-- evidence extracted from master YAML.  Keyed by (requirement_hash, evidence_key).
-- Consumers: evidence map (slice 4).
CREATE TABLE IF NOT EXISTS requirement_evidence_map (
    requirement_hash  TEXT NOT NULL,
    evidence_key      TEXT NOT NULL,
    evidence_text     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (requirement_hash, evidence_key)
);

-- application_fingerprints: per-slug content hash for change detection.
-- Allows downstream slices to skip re-processing unchanged applications.
-- Consumers: JD dedup (slice 5), re-gate backstop (slice 8).
CREATE TABLE IF NOT EXISTS application_fingerprints (
    slug          TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_fingerprints_hash
    ON application_fingerprints(content_hash);

-- run_metrics: per-run scalar metrics for AB testing and monitoring.
-- Keyed by (slug, metric_key); metric_value stored as TEXT for flexibility.
-- Consumers: metrics/AB (slice 9).
CREATE TABLE IF NOT EXISTS run_metrics (
    slug          TEXT NOT NULL,
    metric_key    TEXT NOT NULL,
    metric_value  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (slug, metric_key)
);
CREATE INDEX IF NOT EXISTS idx_run_metrics_slug
    ON run_metrics(slug);
