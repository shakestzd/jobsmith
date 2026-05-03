-- Slice 6 stored only (section, op, value) for amendments, but the
-- AMEND grammar already carries (index, field) — e.g. AMEND work[0].bullet[2]
-- needs index=0 and field='bullet[2]' to round-trip into finalize.py.
-- Without these columns Finalize reconstructs Amendment(index=None, field=None)
-- and the YAML appliers reject every row (roborev #921 HIGH).
--
-- Both columns are nullable so cover-letter-style amendments (no index,
-- field=opening etc.) and append-form amendments (no index) keep working.

ALTER TABLE amendments ADD COLUMN target_index INTEGER;
ALTER TABLE amendments ADD COLUMN target_field TEXT;
