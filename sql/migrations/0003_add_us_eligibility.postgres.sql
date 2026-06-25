-- US work-eligibility verdict for a vacancy, orthogonal to the fit score:
-- outside_us_ok | us_only | unclear. Set by the scoring subagent; us_only rows
-- are auto-archived. IF NOT EXISTS keeps this safe on installs where the column
-- was already added out of band.
ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS us_eligibility TEXT;
