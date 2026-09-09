-- 0023_report_table — SQLite variant (see .postgres.sql for the rationale).
--
-- Same shape, dialect differences only: TEXT ids generated the way the SQLite
-- baseline generates every other id, and TEXT timestamps (db_backend decodes
-- created_at / updated_at back to datetimes on read, so callers see the same
-- types on both backends).
--
-- Fully additive — a new table and its indexes.

CREATE TABLE IF NOT EXISTS report (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),

    slug        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,

    kind        TEXT NOT NULL DEFAULT 'other'
                CHECK (kind IN ('research', 'grant', 'company', 'sector', 'other')),

    body_md     TEXT NOT NULL,
    source_path TEXT,

    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_report_kind       ON report (kind);
CREATE INDEX IF NOT EXISTS idx_report_updated_at ON report (updated_at);
