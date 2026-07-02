-- SQLite mirror of 0006_company_evidence.postgres.sql. Filed under 0007, not
-- 0006: the Postgres file has shipped on main since PR #14, so any SQLite
-- database that already ran migrate.py has 0006 recorded as "n/a (other
-- dialect)" -- reusing that number here would make this migration invisible
-- forever on those databases. Take the next free number instead whenever a
-- dialect sibling ships later than its pair.
-- Raw evidence store for company profiling. One row per (company, source) fetch.
-- Sources: 'website' | 'careers' | 'exa' | 'exa_offices' | 'manual_url' | ...
-- JSONB -> JSON TEXT, UUID -> TEXT, TIMESTAMPTZ -> TEXT (adapter boundary).
CREATE TABLE IF NOT EXISTS company_evidence (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    company_id  TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    url         TEXT,
    content     TEXT,
    meta        TEXT,                         -- JSON object
    fetched_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_company_evidence_company_source
    ON company_evidence (company_id, source);
