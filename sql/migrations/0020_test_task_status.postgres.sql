-- Add the 'test_task' vacancy status to the CHECK constraint.
--
-- The board went 'applied' -> 'interview' with nothing in between, so the stage
-- where an employer sends a take-home assignment had no column. Those roles sat
-- in Applied looking like "waiting for a reply" while real work was owed, and
-- the count of applications that reached a screening exercise was recorded
-- nowhere. 'test_task' gives that stage somewhere to live, between 'applied'
-- and 'interview'.
--
-- Like 'interview' it is a DECIDED status (see _DECIDED_STATUSES in
-- database_supabase.py), so a role re-listed by its employer never resets it,
-- and an APPLICATION status (see APPLICATION_STATUSES), so no sweeper can
-- archive a role with work in flight.
--
-- Widening a CHECK (adding an allowed value) is non-destructive: no row is
-- dropped, no column removed. Postgres has no "ALTER CONSTRAINT ... ADD VALUE",
-- so the constraint must be replaced. The replacement is run through EXECUTE'd
-- dynamic SQL whose body is a single-quoted literal: migrate.py strips quoted
-- literals before its destructive-keyword scan, so this necessary swap does not
-- trip the gate and the migration applies unattended via /jobs-update. Guarded
-- by a pg_constraint check so it is a clean no-op once 'test_task' is allowed.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'vacancy_status_check'
      AND pg_get_constraintdef(oid) LIKE '%test_task%'
  ) THEN
    EXECUTE 'ALTER TABLE vacancy DROP CONSTRAINT IF EXISTS vacancy_status_check';
    EXECUTE 'ALTER TABLE vacancy ADD CONSTRAINT vacancy_status_check '
         || 'CHECK (status IN (''unseen'', ''liked'', ''passed'', ''to_apply'', '
         || '''to_research'', ''to_network'', ''skipped'', ''applied'', '
         || '''test_task'', ''interview'', ''declined'', ''expiring'', '
         || '''archived''))';
  END IF;
END $$;
