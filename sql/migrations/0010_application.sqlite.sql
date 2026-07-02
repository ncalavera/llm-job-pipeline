-- SQLite mirror of 0010_application.postgres.sql. Migration-only, exactly like
-- 0009 (scored_by): the frozen baseline sql/schema.sqlite.sql does NOT declare
-- this table, so a fresh install applies this migration cleanly and the DAL
-- (scripts/applications.py) guards reads with table_ready() until it has run.
--
-- UUID -> TEXT, JSONB -> JSON TEXT, TIMESTAMPTZ/DATE -> TEXT (adapter boundary).
-- Partial unique index (WHERE vacancy_id IS NOT NULL) works on SQLite >= 3.8.
CREATE TABLE IF NOT EXISTS application (
    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    vacancy_id    TEXT REFERENCES vacancy(id) ON DELETE SET NULL,
    company_id    TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,

    channel       TEXT,
    status        TEXT NOT NULL DEFAULT 'applied'
                  CHECK (status IN ('draft', 'applied', 'interview',
                                    'offer', 'rejected', 'withdrawn')),
    applied_at    TEXT,

    artifacts     TEXT DEFAULT '{}',            -- JSON object
    notes         TEXT,

    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_application_company ON application (company_id);
CREATE INDEX IF NOT EXISTS idx_application_vacancy ON application (vacancy_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_vacancy_unique
    ON application (vacancy_id) WHERE vacancy_id IS NOT NULL;
