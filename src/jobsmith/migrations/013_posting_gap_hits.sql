-- Migration 013: add gap_hits_json to postings (feat-d20ff292)
--
-- gap_hits_json stores the known-gaps badge payload for each posting:
--   [{gap: <short label>, term: <matched term>}, ...]
--
-- NULL means "not yet evaluated" (empty JD, crawl not yet run, or DB upgrade pending).
-- [] (empty JSON array) means "evaluated but no gap matches found".
-- Advisory UI hint only — never used as a default filter.
ALTER TABLE postings ADD COLUMN gap_hits_json TEXT;
