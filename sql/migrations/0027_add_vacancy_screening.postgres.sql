-- 0027_add_vacancy_screening — the nightly screening preparation result
-- Structured facts and a profile comparison per vacancy, so
-- the user screens roles on evidence instead of a fit score.
--
-- One additive JSONB result plus the minimum freshness metadata:
--   screening            the validated result (posting_facts + profile_comparison),
--                        or {"failed": "<reason>"} when preparation failed.
--   screening_state      'ready' | 'failed'; NULL = never prepared.
--   screening_prepared_at when the stored result was written.
--   screening_fingerprint "<posting fp>:<prompt+profile fp>" — an unchanged
--                        fingerprint with state 'ready' is never re-prepared;
--                        a changed posting or profile invalidates the result.
--
-- Independent of the human verdict columns: preparation never writes a status.
-- Guarded with IF NOT EXISTS like 0025/0026.
ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS screening JSONB;
ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS screening_state TEXT;
ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS screening_prepared_at TIMESTAMPTZ;
ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS screening_fingerprint TEXT;
