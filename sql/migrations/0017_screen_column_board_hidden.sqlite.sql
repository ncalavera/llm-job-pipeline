-- 0017_screen_column_board_hidden — SQLite variant (see .postgres.sql for the
-- full rationale). SQLite has no ADD COLUMN IF NOT EXISTS.
--
-- The frozen baseline (sql/schema.sqlite.sql) already declares company.description,
-- so a fresh install has it; this migration only matters for a drifted DB. To stay
-- idempotent without the guarded IF NOT EXISTS, this version is registered as a
-- single-statement ADD COLUMN whose "duplicate column name" is tolerated by the
-- runner (see scripts/migrate.py _Sqlite.run). board.hidden is NOT in the baseline,
-- so its bare ADD COLUMN applies cleanly on a fresh install.

ALTER TABLE board ADD COLUMN hidden INTEGER DEFAULT 0;
