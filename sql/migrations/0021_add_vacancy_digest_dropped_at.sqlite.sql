-- 0021_add_vacancy_digest_dropped_at — SQLite variant (see .postgres.sql for
-- the rationale).
--
-- The frozen baseline (sql/schema.sqlite.sql) does not declare this column on
-- `vacancy`, so this ADD COLUMN applies cleanly on a fresh install (matches
-- 0020). SQLite has no ADD COLUMN IF NOT EXISTS; a bare ADD is correct here
-- because the baseline never carries the column. Timestamps are TEXT on
-- SQLite, like digest_sent_at.
ALTER TABLE vacancy ADD COLUMN digest_dropped_at TEXT;
