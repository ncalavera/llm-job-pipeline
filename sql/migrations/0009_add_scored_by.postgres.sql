-- Score provenance: which model tier last wrote vacancy.llm_score.
-- Two-pass scoring (run_daily.py _h_vacancy_scoring) writes this at BOTH
-- passes -- the cheap screen model first, the strong model overwriting it on
-- escalation -- so a kept-cheap score is never byte-identical in the DB to a
-- confirmed strong-model score. Nullable: NULL means "scored before this
-- column existed" (or by a caller that doesn't report a model); the
-- dashboard treats NULL as "no provenance, no badge" rather than crashing.
ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS scored_by TEXT;
