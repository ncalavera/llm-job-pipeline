-- Timestamp of the loud "expiring" Telegram alert (see postgres variant).
-- SQLite has no ADD COLUMN IF NOT EXISTS, and sql/schema.sqlite.sql (the
-- frozen baseline) already declares this column, so a fresh install hits
-- "duplicate column name" here. migrate.py's SQLite runner catches that
-- specific error and treats the migration as applied instead of aborting --
-- see _Sqlite.run() in scripts/migrate.py.
ALTER TABLE vacancy ADD COLUMN expiring_alerted_at TEXT;
