-- 0016_company_coverage — SQLite variant (see .postgres.sql for the rationale).
-- The frozen baseline does not declare `coverage`, so a bare ADD COLUMN applies
-- cleanly on a fresh install.
ALTER TABLE company ADD COLUMN coverage TEXT;
