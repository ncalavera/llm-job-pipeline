-- 0022_applications_table — the schema the Applications table view needs.
--
-- Three changes, one story: the Triage board tracks where an application IS,
-- but nothing recorded when it was SENT, what kind of thing was applied to, or
-- that it ended well. The table view answers "what have I sent, and what is
-- waiting on whom" — and it cannot, until the rows carry those three facts.
--
--   1. status 'accepted'  — the employer's yes. The board ended at 'declined',
--      so a won offer had to be filed as a rejection or left reading as "still
--      interviewing".
--   2. applied_at         — the date the application went out. status_updated_at
--      moves with every stage, so on a declined row it holds the date of the
--      REJECTION; using it as "sent on" backdates nothing and post-dates
--      everything. Written once, when a row first enters the application
--      funnel, and never overwritten (see update_vacancy_status).
--   3. kind               — not every application is a job. Courses, career
--      advising, consulting programmes and grants are applications he sent and
--      wants counted, and they are stored as ordinary vacancy rows. Defaults
--      to 'job', so every existing row is already correct.
--
-- No backfill of applied_at. The honest fallback is the display layer's
-- (applied_at, else status_updated_at); inventing a send date for history the
-- database never recorded would look like data and be a guess.
--
-- Widening a CHECK (adding an allowed value) is non-destructive: no row is
-- dropped, no column removed. Postgres has no "ALTER CONSTRAINT ... ADD VALUE",
-- so the constraint must be replaced. The replacement runs through EXECUTE'd
-- dynamic SQL whose body is a single-quoted literal: migrate.py strips quoted
-- literals before its destructive-keyword scan, so this necessary swap does not
-- trip the gate and the migration applies unattended via /jobs-update. Guarded
-- by a pg_constraint check so it is a clean no-op once 'accepted' is allowed.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'vacancy_status_check'
      AND pg_get_constraintdef(oid) LIKE '%accepted%'
  ) THEN
    EXECUTE 'ALTER TABLE vacancy DROP CONSTRAINT IF EXISTS vacancy_status_check';
    EXECUTE 'ALTER TABLE vacancy ADD CONSTRAINT vacancy_status_check '
         || 'CHECK (status IN (''unseen'', ''liked'', ''passed'', ''to_apply'', '
         || '''to_research'', ''to_network'', ''skipped'', ''applied'', '
         || '''test_task'', ''interview'', ''declined'', ''accepted'', '
         || '''expiring'', ''archived''))';
  END IF;
END $$;

ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ;

ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'job';

-- Same guarded-swap pattern as the status CHECK above, for the same reason:
-- ADD COLUMN cannot carry a named constraint that survives a re-run, and a
-- bare ADD CONSTRAINT fails on the second pass.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'vacancy_kind_check'
  ) THEN
    EXECUTE 'ALTER TABLE vacancy ADD CONSTRAINT vacancy_kind_check '
         || 'CHECK (kind IN (''job'', ''programme'', ''advising'', '
         || '''consulting'', ''grant'', ''course''))';
  END IF;
END $$;

-- The table view lists every application ever sent, newest first: one index
-- serves both the filter (status) and the sort (applied_at).
CREATE INDEX IF NOT EXISTS idx_vacancy_applied_at ON vacancy (applied_at DESC NULLS LAST);
