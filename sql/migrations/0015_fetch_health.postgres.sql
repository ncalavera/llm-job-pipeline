-- 0015_fetch_health — persist fetch telemetry so failures are observable.
--
-- Problem this fixes: today a company/board fetch records only `last_fetched`
-- (last ATTEMPT) and a coarse `fetch_status`. You cannot tell "broken" from
-- "genuinely empty" over time, and the `board` table has NO count/status/error
-- at all — boards are completely blind. `company.fetch_error` is even declared
-- in the frozen baseline (sql/schema.sql) but was never added to production
-- (schema drift), so error detail is silently dropped.
--
-- This migration is purely additive (ADD COLUMN IF NOT EXISTS), so replaying it
-- against a DB that already has any of these columns is a clean no-op.
--
--   last_success         — last time the fetch actually succeeded (ok / empty),
--                          distinct from last_fetched (last attempt). Lets a
--                          monitor spot silent degradation: attempted daily but
--                          not succeeded for a week.
--   consecutive_failures — streak of real errors; turns a one-off blip into an
--                          alert only after it persists.
--   fetch_error          — human-readable reason string for the last failure
--                          (aligns prod with the checked-in baseline schema).

-- company: align with baseline + add streak/last-success telemetry.
ALTER TABLE company ADD COLUMN IF NOT EXISTS fetch_error          TEXT;
ALTER TABLE company ADD COLUMN IF NOT EXISTS last_success         TIMESTAMPTZ;
ALTER TABLE company ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0;

-- board: was totally blind (only last_fetched + enabled). Give it the same
-- health surface as companies so "do the boards actually work?" is answerable.
ALTER TABLE board ADD COLUMN IF NOT EXISTS vacancy_count        INTEGER;
ALTER TABLE board ADD COLUMN IF NOT EXISTS fetch_status         TEXT;
ALTER TABLE board ADD COLUMN IF NOT EXISTS last_error           TEXT;
ALTER TABLE board ADD COLUMN IF NOT EXISTS last_success         TIMESTAMPTZ;
ALTER TABLE board ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0;
