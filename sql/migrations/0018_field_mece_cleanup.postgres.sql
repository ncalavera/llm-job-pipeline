-- 0018_field_mece_cleanup — drop four dead columns (Postgres variant).
--
-- Field-MECE cleanup: remove columns that exist in the live database but have
-- NO reader anywhere in the codebase (scripts/, api/, public/). Each was traced
-- end-to-end before removal:
--
--   company.is_unverified  — boolean, 131 rows TRUE. Zero references in any
--                            Python, JS, SQL, or test file. No writer, no reader.
--   vacancy.mission_rescue — boolean, 0 rows TRUE (inert). Zero references.
--                            NOT part of the live geo filter: that path reads
--                            locations[].region + us_eligibility, never this.
--   vacancy.location_match — boolean, 189 rows TRUE (stale). No production
--                            reader; only appears as a leftover key in in-memory
--                            test fixtures.
--   vacancy.relevance_score— integer. Only reader was filter_vacancies'
--                            `ready_by_relevance` histogram, which was computed
--                            but never rendered (that dead stat is removed in the
--                            same change). Column carries no live signal.
--
-- These columns are NOT in the frozen baseline (sql/schema.sql / .sqlite.sql):
-- they are pre-baseline drift, so no schema.sql edit is needed. The top-level
-- vacancy.region column is deliberately KEPT — it still feeds the rendered
-- ready_by_region report stat and the geo legacy fallback.
--
-- DESTRUCTIVE: contains DROP COLUMN. `scripts/migrate.py` will flag it and ask
-- for confirmation. Data in these columns is discarded permanently.

ALTER TABLE company DROP COLUMN IF EXISTS is_unverified;
ALTER TABLE vacancy DROP COLUMN IF EXISTS mission_rescue;
ALTER TABLE vacancy DROP COLUMN IF EXISTS location_match;
ALTER TABLE vacancy DROP COLUMN IF EXISTS relevance_score;
