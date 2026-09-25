-- 0034_source_fetch_attempt — see the .postgres.sql twin for the why.

ALTER TABLE vacancy ADD COLUMN source_fetch_attempted_at TEXT;
