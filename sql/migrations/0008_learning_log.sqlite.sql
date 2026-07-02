-- SQLite mirror of 0008_learning_log.postgres.sql. Same version number: both
-- dialect halves ship together in this PR, so 0008 is free in both trees (the
-- highest shipped version is 0007). JSONB -> JSON TEXT, UUID -> TEXT,
-- TIMESTAMPTZ -> TEXT (the adapter boundary).
--
-- Learning-cycle ledger. Append-only record that closes the feedback loop
-- (STRATEGY guardrail 8). One row per event, three kinds:
--   'garbage'  — a verdict flagged as a filter hole. ref = vacancy id;
--                detail = {title, source, score}. Feeds filter-word proposals.
--   'reviewed' — a COMPLETED learning-review cycle. detail = {agreement, ...}.
--                created_at is the rollover cursor; a SKIPPED review writes no
--                row, so its verdicts roll over untouched.
--   'applied'  — an approved change was applied. detail = {kind, before, after}.
CREATE TABLE IF NOT EXISTS learning_log (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    kind        TEXT NOT NULL,
    ref         TEXT,
    detail      TEXT,                         -- JSON object
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_learning_log_kind_created
    ON learning_log (kind, created_at);
