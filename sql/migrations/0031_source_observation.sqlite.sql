CREATE TABLE IF NOT EXISTS source_observation (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    run_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_url TEXT,
    external_id TEXT,
    title TEXT,
    organization TEXT,
    listing_url TEXT, canonical_id TEXT,
    outcome TEXT NOT NULL,
    reason TEXT,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, source_key, external_id)
);
CREATE INDEX IF NOT EXISTS idx_source_observation_lookup
    ON source_observation (source_key, external_id, observed_at);
CREATE TABLE IF NOT EXISTS source_fetch_run (
    run_id TEXT NOT NULL, source_key TEXT NOT NULL, source_url TEXT,
    raw_count INTEGER NOT NULL DEFAULT 0, accepted_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0,
    error TEXT, completed_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, source_key)
);
