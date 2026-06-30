-- llm-job-pipeline — local SQLite schema (easy mode)
--
-- Functional mirror of sql/schema.sql for the no-Supabase install path.
-- Auto-applied by scripts/db_backend.py on first connection (zero manual
-- steps). Postgres types map to SQLite affinities; JSONB / TEXT[] columns are
-- stored as JSON TEXT and (de)serialized at the adapter boundary.
--
-- This schema is a SUPERSET of schema.sql: it also carries the extra company
-- columns the dashboard generator reads (product, offices, experience_match,
-- personal_interest, user_comments) and vacancy.updated_at, so the report path
-- runs unchanged locally.

-- ---------------------------------------------------------------------------
-- company — sole source of truth for organisations we monitor.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS company (
    id                TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    canonical_name    TEXT NOT NULL UNIQUE,
    aliases           TEXT DEFAULT '[]',            -- JSON array of strings

    status            TEXT NOT NULL DEFAULT 'candidate'
                      CHECK (status IN ('active', 'candidate', 'inactive')),
    status_reason     TEXT,

    tier              TEXT CHECK (tier IS NULL OR tier IN ('S', 'A', 'B', 'C')),
    category          TEXT,

    website           TEXT,

    fetch_strategy    TEXT,
    careers_url       TEXT,
    ats_slug          TEXT,
    ats_config        TEXT DEFAULT '{}',            -- JSON object

    last_fetched      TEXT,
    vacancy_count     INTEGER,
    fetch_status      TEXT,
    fetch_error       TEXT,

    -- Enrichment
    description       TEXT,
    product           TEXT,
    notes             TEXT,
    user_comments     TEXT,
    offices           TEXT,
    experience_match  INTEGER,
    personal_interest INTEGER,
    about             TEXT,                         -- JSON object
    mission_fit       TEXT,                         -- JSON object
    alignment_score   INTEGER,
    enriched_at       TEXT,

    created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_company_status ON company (status);
CREATE INDEX IF NOT EXISTS idx_company_tier   ON company (tier);

-- ---------------------------------------------------------------------------
-- vacancy — one row per job posting. Dedup via dedup_hash.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vacancy (
    id                    TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    dedup_hash            TEXT NOT NULL UNIQUE,
    company_id            TEXT REFERENCES company(id) ON DELETE CASCADE,

    title                 TEXT NOT NULL,
    snippet               TEXT,
    full_description      TEXT,
    compensation          TEXT,
    deadline              TEXT,
    department            TEXT,

    locations             TEXT DEFAULT '[]',        -- JSON array

    first_seen            TEXT NOT NULL,
    last_seen             TEXT NOT NULL,

    status                TEXT NOT NULL DEFAULT 'unseen'
                          CHECK (status IN ('unseen', 'liked', 'passed',
                                            'to_apply', 'to_research',
                                            'to_network', 'skipped', 'applied',
                                            'expiring', 'archived')),
    status_updated_at     TEXT,

    llm_score             INTEGER,
    llm_reasoning         TEXT,
    llm_summary           TEXT,
    llm_tags              TEXT DEFAULT '[]',        -- JSON array
    llm_hard_requirements TEXT DEFAULT '[]',        -- JSON array
    llm_scored_at         TEXT,
    us_eligibility        TEXT,                     -- outside_us_ok|us_only|unclear

    triage                TEXT DEFAULT '{}',        -- JSON object/array

    digest_sent_at        TEXT,
    expiring_alerted_at   TEXT,        -- loud expiring alert sent (once)

    created_at            TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at            TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vacancy_company    ON vacancy (company_id);
CREATE INDEX IF NOT EXISTS idx_vacancy_status     ON vacancy (status);
CREATE INDEX IF NOT EXISTS idx_vacancy_dedup_hash ON vacancy (dedup_hash);
CREATE INDEX IF NOT EXISTS idx_vacancy_llm_score  ON vacancy (llm_score);

-- ---------------------------------------------------------------------------
-- archived_hash — tombstones for archived/removed vacancies.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS archived_hash (
    dedup_hash   TEXT PRIMARY KEY,
    reason       TEXT,
    archived_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_archived_hash_at ON archived_hash (archived_at);
