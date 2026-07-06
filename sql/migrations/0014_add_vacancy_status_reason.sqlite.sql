-- 0014_add_vacancy_status_reason — SQLite variant (see .postgres.sql for the
-- rationale).
--
-- The frozen baseline (sql/schema.sqlite.sql) does not declare this column on
-- `vacancy`, so this ADD COLUMN applies cleanly on a fresh install -- no
-- baseline duplicate-column overlap to special-case (matches 0009/0013).
-- SQLite has no ADD COLUMN IF NOT EXISTS; a bare ADD is correct here because
-- the baseline never carries the column.
ALTER TABLE vacancy ADD COLUMN status_reason TEXT;
