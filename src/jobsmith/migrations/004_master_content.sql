-- Master content cache table for DB-as-source-of-truth (feat-bf06bdea, S1).
--
-- Stores the raw YAML blob for each master section.  The pipeline DB is the
-- runtime authority; YAML files on disk are the input mechanism.
--
-- section      : canonical name ('work', 'skill', 'education', 'author')
-- content_blob : raw YAML text as loaded from disk
-- etag         : sha256(content_blob.encode('utf-8'))[:16] for cache validation
-- loaded_at    : ISO-8601 timestamp of the last successful load

CREATE TABLE IF NOT EXISTS master_content (
    section      TEXT PRIMARY KEY,
    content_blob TEXT NOT NULL,
    etag         TEXT,
    loaded_at    TEXT
);
