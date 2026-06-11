-- Migration 011: add filtered_count to sourcing_runs (feat-e32cde37)
--
-- Tracks how many postings were rejected by title filters / min_fast_score
-- before upsert. Backward compatible: existing rows get 0 (DEFAULT).
ALTER TABLE sourcing_runs ADD COLUMN filtered_count INTEGER NOT NULL DEFAULT 0;
