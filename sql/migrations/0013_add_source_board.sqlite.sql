-- 0013_add_source_board — SQLite variant (see .postgres.sql for the rationale).
--
-- The frozen baseline (sql/schema.sqlite.sql) does not declare this column, so
-- this ADD COLUMN applies cleanly on a fresh install — no baseline
-- duplicate-column overlap to special-case (matches 0009_add_scored_by).
-- SQLite has no ADD COLUMN IF NOT EXISTS; a bare ADD is correct here because the
-- baseline never carries the column.
ALTER TABLE vacancy ADD COLUMN source_board TEXT;
