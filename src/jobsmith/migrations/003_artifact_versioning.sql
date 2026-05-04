-- Adds a version counter to specialist_outputs for optimistic concurrency control.
-- Each PUT must supply If-Match: <current-version>; a mismatch returns 409 Conflict.
-- On first write (INSERT) version starts at 1; each overwrite increments by 1.
--
-- SQLite does not support ADD COLUMN ... NOT NULL without a DEFAULT when the
-- table already contains rows, so DEFAULT 1 is required.

ALTER TABLE specialist_outputs ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
