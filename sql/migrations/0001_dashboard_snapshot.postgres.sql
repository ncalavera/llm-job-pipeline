-- 0001_dashboard_snapshot — live-dashboard read source (full mode only).
--
-- Holds the latest assembled VACANCY_DATA payload as a single row so the
-- dashboard can read it live via /api/vacancies on every refresh, with no
-- redeploy. The pipeline's generate_dashboard() upserts this row instead of
-- baking public/data.js + vercel --prod.
--
-- Postgres only (.postgres.sql): simple/SQLite mode keeps writing and serving
-- public/data.js and never reads this table, so there is no SQLite variant —
-- the migration runner records this version as applied on SQLite without
-- running anything.
CREATE TABLE IF NOT EXISTS dashboard_snapshot (
    id          TEXT PRIMARY KEY DEFAULT 'current',
    payload     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
