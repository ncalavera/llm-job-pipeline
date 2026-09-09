-- migrate:allow-destructive rebuilds vacancy to widen its status CHECK; every row is copied into the new table before the old one is dropped.
--
-- 0021_status_check_rebuild — SQLite variant. Widens vacancy.status's CHECK to
-- accept 'test_task', 'interview' and 'declined'.
--
-- Migrations 0019 (interview/declined) and 0020 (test_task) shipped as
-- Postgres-only files, so SQLite recorded both as "n/a (other dialect)" and
-- applied nothing. A SQLite database created BEFORE the frozen baseline was
-- updated therefore still carries the old ten-value CHECK: moving a role to
-- 'test_task' fails with "CHECK constraint failed", and the whole application
-- funnel is unreachable in simple mode. A database created after that edit
-- already has the wide CHECK — for it this rebuild is a no-op in effect, and
-- cheap (the table is small).
--
-- SQLite cannot ALTER a CHECK constraint, so the only way to widen one is the
-- documented table rebuild: create the new shape, copy every row, drop the old
-- table, rename. That DROP is why this file declares the destructive waiver on
-- its first line — the drop removes a table whose every row the INSERT above it
-- has already copied, and migrate.py still takes its automatic backup first and
-- restores byte-for-byte if anything here throws.
--
-- foreign_keys is turned OFF for the rebuild (SQLite's own 12-step procedure):
-- the `application` table references vacancy(id) ON DELETE SET NULL, so
-- dropping the old table with enforcement ON would blank every application's
-- vacancy_id. It is turned back ON at the end.
--
-- The column list is the shape of `vacancy` after the SQLite chain up to 0020:
-- the frozen baseline plus scored_by (0009), source_board (0013) and
-- status_reason (0014), all of which the runner applies before this file.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE vacancy_status_rebuild (
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
                                            'test_task', 'interview',
                                            'declined', 'expiring',
                                            'archived')),
    status_updated_at     TEXT,

    llm_score             INTEGER,
    llm_reasoning         TEXT,
    llm_summary           TEXT,
    llm_tags              TEXT DEFAULT '[]',        -- JSON array
    llm_hard_requirements TEXT DEFAULT '[]',        -- JSON array
    llm_scored_at         TEXT,
    us_eligibility        TEXT,

    triage                TEXT DEFAULT '{}',        -- JSON object/array

    digest_sent_at        TEXT,
    expiring_alerted_at   TEXT,

    created_at            TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at            TEXT DEFAULT CURRENT_TIMESTAMP,

    scored_by             TEXT,                     -- migration 0009
    source_board          TEXT,                     -- migration 0013
    status_reason         TEXT                      -- migration 0014
);

INSERT INTO vacancy_status_rebuild (
    id, dedup_hash, company_id, title, snippet, full_description, compensation,
    deadline, department, locations, first_seen, last_seen, status,
    status_updated_at, llm_score, llm_reasoning, llm_summary, llm_tags,
    llm_hard_requirements, llm_scored_at, us_eligibility, triage,
    digest_sent_at, expiring_alerted_at, created_at, updated_at,
    scored_by, source_board, status_reason
)
SELECT
    id, dedup_hash, company_id, title, snippet, full_description, compensation,
    deadline, department, locations, first_seen, last_seen, status,
    status_updated_at, llm_score, llm_reasoning, llm_summary, llm_tags,
    llm_hard_requirements, llm_scored_at, us_eligibility, triage,
    digest_sent_at, expiring_alerted_at, created_at, updated_at,
    scored_by, source_board, status_reason
FROM vacancy;

DROP TABLE vacancy;

ALTER TABLE vacancy_status_rebuild RENAME TO vacancy;

CREATE INDEX IF NOT EXISTS idx_vacancy_company    ON vacancy (company_id);
CREATE INDEX IF NOT EXISTS idx_vacancy_status     ON vacancy (status);
CREATE INDEX IF NOT EXISTS idx_vacancy_dedup_hash ON vacancy (dedup_hash);
CREATE INDEX IF NOT EXISTS idx_vacancy_llm_score  ON vacancy (llm_score);

COMMIT;

PRAGMA foreign_keys = ON;
