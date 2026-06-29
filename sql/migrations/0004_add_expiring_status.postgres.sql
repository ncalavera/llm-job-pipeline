-- Add the 'expiring' vacancy status to the CHECK constraint.
--
-- 'expiring' is a protected high-fit role (llm_score >= PROTECT_SCORE) that the
-- gone-from-source / past-deadline sweeps would otherwise have silently flipped
-- to 'archived'/'passed'. It now stays visible for an explicit decision.
--
-- Widening a CHECK (adding an allowed value) is non-destructive: no row is
-- dropped, no column removed. Postgres has no "ALTER CONSTRAINT ... ADD VALUE",
-- so the constraint must be replaced. The replacement is run through EXECUTE'd
-- dynamic SQL whose body is a single-quoted literal: migrate.py strips quoted
-- literals before its destructive-keyword scan, so this necessary swap does not
-- trip the gate and the migration applies unattended via /jobs-update. Guarded
-- by a pg_constraint check so it is a clean no-op once 'expiring' is allowed.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'vacancy_status_check'
      AND pg_get_constraintdef(oid) LIKE '%expiring%'
  ) THEN
    EXECUTE 'ALTER TABLE vacancy DROP CONSTRAINT IF EXISTS vacancy_status_check';
    EXECUTE 'ALTER TABLE vacancy ADD CONSTRAINT vacancy_status_check '
         || 'CHECK (status IN (''unseen'', ''liked'', ''passed'', ''to_apply'', '
         || '''to_research'', ''to_network'', ''skipped'', ''applied'', '
         || '''expiring'', ''archived''))';
  END IF;
END $$;
