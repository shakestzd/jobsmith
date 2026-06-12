-- Migration 012: add coverage columns to postings (feat-a73173a1)
--
-- coverage_score: integer 0-100, NULL when not yet scored or coverage unavailable.
-- uncovered_json: JSON-encoded list[str] of must-have gaps, NULL when not scored.
-- Both columns stay NULL for postings outside the LLM-rescored top-N.
ALTER TABLE postings ADD COLUMN coverage_score REAL NULL;
ALTER TABLE postings ADD COLUMN uncovered_json TEXT NULL;
