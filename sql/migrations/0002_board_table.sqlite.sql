-- 0002_board_table — SQLite variant (see .postgres.sql for the rationale).
-- Timestamps are TEXT (the adapter (de)serializes), matching the company table.
CREATE TABLE IF NOT EXISTS board (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    strategy      TEXT,
    tier          TEXT CHECK (tier IS NULL OR tier IN ('S', 'A', 'B', 'C')),
    ttl_days      INTEGER,
    url           TEXT,
    last_fetched  TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
