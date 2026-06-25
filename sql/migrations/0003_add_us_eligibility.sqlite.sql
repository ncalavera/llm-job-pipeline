-- US work-eligibility verdict for a vacancy, orthogonal to the fit score:
-- outside_us_ok | us_only | unclear. SQLite has no ADD COLUMN IF NOT EXISTS;
-- the migration runner applies each file once, so a plain ALTER is safe.
ALTER TABLE vacancy ADD COLUMN us_eligibility TEXT;
