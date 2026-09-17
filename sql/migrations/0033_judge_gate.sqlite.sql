-- 0033_judge_gate — SQLite variant (see .postgres.sql for the rationale).
--
-- Same shape, dialect differences only: no BIGSERIAL (INTEGER PRIMARY KEY
-- autoincrements natively), no REFERENCES enforcement quirks beyond SQLite's
-- own (foreign_keys pragma, set elsewhere), TEXT timestamps like every other
-- table here.

ALTER TABLE vacancy ADD COLUMN judge_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (judge_state IN ('pending', 'keep', 'unsure', 'killed'));
ALTER TABLE vacancy ADD COLUMN description_source TEXT;

CREATE TABLE IF NOT EXISTS judge_review (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vacancy_id    TEXT NOT NULL UNIQUE REFERENCES vacancy(id),

    source        TEXT NOT NULL CHECK (source IN ('review', 'today')),

    verdict       TEXT CHECK (verdict IN ('keep', 'unsure', 'remove')),
    comment       TEXT,

    judge_verdict TEXT,
    brief_version TEXT,

    draw          TEXT CHECK (draw IN ('random', 'hard', 'today')),
    split         TEXT CHECK (split IN ('lesson', 'exam')),

    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vacancy_judge_state ON vacancy (judge_state);
