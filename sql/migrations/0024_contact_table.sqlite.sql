-- 0024_contact_table — SQLite variant (see .postgres.sql for the rationale).
--
-- Same shape, dialect differences only: TEXT ids generated the way the SQLite
-- baseline generates every other id, TEXT timestamps (db_backend decodes
-- created_at / updated_at back to datetimes on read), and channels stored as a
-- JSON string rather than JSONB — the DAL serialises on write and parses on
-- read, so callers see a dict on both backends.
--
-- "group" is quoted here for the same reason as in Postgres: it is a reserved
-- word in both dialects.
--
-- Fully additive.

CREATE TABLE IF NOT EXISTS contact (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),

    name        TEXT NOT NULL,
    name_local  TEXT,

    city        TEXT,
    org         TEXT,
    role        TEXT,

    why_matters TEXT,

    channels    TEXT NOT NULL DEFAULT '{}',      -- JSON object

    "group"     TEXT NOT NULL DEFAULT 'other',

    status      TEXT NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned', 'contacted', 'replied',
                                  'met', 'declined', 'stale')),
    status_at   TEXT,

    last_active TEXT,

    opener      TEXT,
    notes       TEXT,

    source_path TEXT,

    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_identity ON contact (name, "group");

CREATE INDEX IF NOT EXISTS idx_contact_group  ON contact ("group");
CREATE INDEX IF NOT EXISTS idx_contact_status ON contact (status);
