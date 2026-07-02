-- US work-eligibility verdict for a vacancy, orthogonal to the fit score:
-- outside_us_ok | us_only | unclear. SQLite has no ADD COLUMN IF NOT EXISTS,
-- and sql/schema.sqlite.sql (the frozen baseline) already declares this
-- column, so a fresh install hits "duplicate column name" here. migrate.py's
-- SQLite runner catches that specific error and treats the migration as
-- applied instead of aborting -- see _Sqlite.run() in scripts/migrate.py.
ALTER TABLE vacancy ADD COLUMN us_eligibility TEXT;
