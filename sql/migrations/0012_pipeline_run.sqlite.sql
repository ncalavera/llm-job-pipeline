-- 0012_pipeline_run — SQLite variant (see .postgres.sql for the rationale).
--
-- The SQLite demo never carried a pipeline_run table (it is not in the frozen
-- baseline sql/schema.sqlite.sql), so there is no legacy shape to upgrade — a
-- single guarded CREATE gives a fresh install the full shape. JSONB/BOOLEAN/
-- TIMESTAMPTZ map to TEXT/INTEGER/TEXT here; the db_backend translation layer
-- (de)serializes JSON via Json(...) and Python bools to 1/0, and run_daily
-- reads only the plain INTEGER `new_vacancies` counter for the range check, so
-- no JSON is parsed on the read path.
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id            TEXT PRIMARY KEY,
    run_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished          INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'running',
    boards            TEXT,
    new_vacancies     INTEGER,
    scored            INTEGER,
    companies_scored  INTEGER,
    stages            TEXT,
    counts            TEXT,
    errors            TEXT
);
