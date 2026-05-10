CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    email      TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    hashed_pw  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS user_sessions (
    session_id         TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id            TEXT NOT NULL REFERENCES users(user_id),
    refresh_token_hash TEXT NOT NULL UNIQUE,
    issued_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at         TEXT NOT NULL,
    revoked            INTEGER NOT NULL DEFAULT 0
);
