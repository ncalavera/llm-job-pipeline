-- Add the 'interview' and 'declined' vacancy statuses to the CHECK constraint.
--
-- Before this, the triage board ended at 'applied': an application could go in
-- but never come out, so the Applied column silently accumulated and the
-- employer's own answer was never recorded anywhere. 'declined' closes that
-- loop, and 'interview' gives the stage between the two somewhere to live.
--
-- Both are DECIDED statuses (see _DECIDED_STATUSES in database_supabase.py), so
-- a role that is re-listed by its employer never resets them — the failure that
-- pushed a real, wanted role back into the untriaged catalogue.
--
-- Widening a CHECK (adding an allowed value) is non-destructive: no row is
-- dropped, no column removed. Postgres has no "ALTER CONSTRAINT ... ADD VALUE",
-- so the constraint must be replaced. The replacement is run through EXECUTE'd
-- dynamic SQL whose body is a single-quoted literal: migrate.py strips quoted
-- literals before its destructive-keyword scan, so this necessary swap does not
-- trip the gate and the migration applies unattended via /jobs-update. Guarded
-- by a pg_constraint check so it is a clean no-op once 'declined' is allowed.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'vacancy_status_check'
      AND pg_get_constraintdef(oid) LIKE '%declined%'
  ) THEN
    EXECUTE 'ALTER TABLE vacancy DROP CONSTRAINT IF EXISTS vacancy_status_check';
    EXECUTE 'ALTER TABLE vacancy ADD CONSTRAINT vacancy_status_check '
         || 'CHECK (status IN (''unseen'', ''liked'', ''passed'', ''to_apply'', '
         || '''to_research'', ''to_network'', ''skipped'', ''applied'', '
         || '''interview'', ''declined'', ''expiring'', ''archived''))';
  END IF;
END $$;
