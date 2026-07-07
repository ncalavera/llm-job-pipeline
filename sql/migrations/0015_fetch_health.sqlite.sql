-- 0015_fetch_health — SQLite variant (see .postgres.sql for the full rationale).
--
-- SQLite has no `ADD COLUMN IF NOT EXISTS`. The frozen baseline
-- (sql/schema.sqlite.sql) declares company.fetch_error but NOT last_success /
-- consecutive_failures, and the board table carries none of these. On a fresh
-- install the baseline runs first, so a bare ADD COLUMN for the new columns is
-- correct. company.fetch_error IS in the sqlite baseline, so it is NOT added
-- here (adding it would fail with "duplicate column name").

ALTER TABLE company ADD COLUMN last_success         TEXT;
ALTER TABLE company ADD COLUMN consecutive_failures INTEGER DEFAULT 0;

ALTER TABLE board ADD COLUMN vacancy_count        INTEGER;
ALTER TABLE board ADD COLUMN fetch_status         TEXT;
ALTER TABLE board ADD COLUMN last_error           TEXT;
ALTER TABLE board ADD COLUMN last_success         TEXT;
ALTER TABLE board ADD COLUMN consecutive_failures INTEGER DEFAULT 0;
