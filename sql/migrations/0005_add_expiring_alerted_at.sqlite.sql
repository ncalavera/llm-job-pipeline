-- Timestamp of the loud "expiring" Telegram alert (see postgres variant).
-- SQLite has no ADD COLUMN IF NOT EXISTS; the runner applies each file once.
ALTER TABLE vacancy ADD COLUMN expiring_alerted_at TEXT;
