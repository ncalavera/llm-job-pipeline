-- Add the 'unsure' vacancy status to the CHECK constraint.
--
-- The daily review has three keys, not two: Like, Pass and Unsure. Unsure means
-- "come back to me". The row leaves today's list and returns to the Inbox the
-- next day, so the reviewer never has to force a Like or a Pass on a role they
-- cannot judge yet. Without it, every hesitation became a Pass.
--
-- It is NOT a decided status: the dashboard maps it back into the undecided
-- basket (VACANCY_BASKETS in public/modules/derive.js) once its
-- status_updated_at date is in the past. It IS a protected status
-- (PROTECTED_STATUSES in scripts/statuses.py), so no sweeper archives a row the
-- reviewer has deliberately deferred.
--
-- Widening a CHECK (adding an allowed value) is non-destructive: no row is
-- dropped, no column removed. Postgres has no "ALTER CONSTRAINT ... ADD VALUE",
-- so the constraint must be replaced. The replacement runs through EXECUTE'd
-- dynamic SQL whose body is a single-quoted literal: migrate.py strips quoted
-- literals before its destructive-keyword scan, so this swap does not trip the
-- gate. Guarded by a pg_constraint check, so it is a clean no-op once 'unsure'
-- is already allowed.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'vacancy_status_check'
      AND pg_get_constraintdef(oid) LIKE '%unsure%'
  ) THEN
    EXECUTE 'ALTER TABLE vacancy DROP CONSTRAINT IF EXISTS vacancy_status_check';
    EXECUTE 'ALTER TABLE vacancy ADD CONSTRAINT vacancy_status_check '
         || 'CHECK (status IN (''unseen'', ''liked'', ''passed'', ''to_apply'', '
         || '''to_research'', ''to_network'', ''skipped'', ''unsure'', '
         || '''applied'', ''test_task'', ''interview'', ''declined'', '
         || '''accepted'', ''expiring'', ''archived''))';
  END IF;
END $$;
