CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key    TEXT PRIMARY KEY,
    specialist   TEXT NOT NULL,
    jd_hash      TEXT NOT NULL,
    master_etag  TEXT NOT NULL,
    output_json  TEXT NOT NULL,
    model        TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    hit_count    INTEGER NOT NULL DEFAULT 0,
    last_hit_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_specialist ON llm_cache(specialist);
