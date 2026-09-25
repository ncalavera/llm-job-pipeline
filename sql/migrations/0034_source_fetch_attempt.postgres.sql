-- 0034_source_fetch_attempt — when the source-text pass tried a row.
--
-- Every board row now gets exactly one full-posting fetch, the night it
-- arrives (enrich_blind_vacancies.py --source-text). Success sets
-- description_source='source_page'; any failure sets 'board_summary_final' and
-- the judge reads the board text as it is. This stamp records that attempt, so
-- the nightly report can count one night's failed downloads (2026-09-25
-- backlog report: 198 rows sat on board text, unjudged, with no warning).
--
-- Fully additive, guarded with IF NOT EXISTS like 0033.

ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS source_fetch_attempted_at TIMESTAMPTZ;
