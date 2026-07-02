-- 0011_board_enabled — SQLite variant (see .postgres.sql for the rationale).
--
-- The frozen baseline (sql/schema.sqlite.sql) does not declare the board table
-- at all -- it is created by 0002 -- so this ADD COLUMN applies cleanly on a
-- fresh install, with no baseline duplicate-column overlap to special-case
-- (unlike 0003/0005). Stored as INTEGER 0/1: the db_backend translation layer
-- (de)serializes Python bools to 1/0, and `WHERE enabled` reads a non-zero row
-- as true, matching the Postgres BOOLEAN.
ALTER TABLE board ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0;
