-- 0023_report_table — research reports, stored and readable on the dashboard.
--
-- Every research report written for this search lives as a markdown file in a
-- private repo: sector research, grant write-ups, company dossiers, the
-- research done for one application. That means the work is only reachable
-- from the laptop it was written on. The dashboard already holds the roles and
-- the applications; the reading behind them belonged there too, next to what
-- it was written for, and readable from a phone.
--
-- One table, deliberately flat. A report has no lifecycle, no status, no
-- relations — it is a document with a name and a kind. `slug` is the identity
-- (unique), so re-importing an edited file UPDATES the report rather than
-- forking a second copy of it; `source_path` records where the markdown came
-- from so a stored report can always be traced back to its file.
--
-- Numbered 0023: 0022 is the applications-table migration.
--
-- Fully additive — a new table and its indexes. Nothing here drops, truncates
-- or rewrites anything, so migrate.py's destructive gate has nothing to stop.

CREATE TABLE IF NOT EXISTS report (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Identity. Derived from the source filename, so the same file re-imported
    -- lands on the same row.
    slug        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,

    -- What kind of reading this is — the one axis the list groups by. A closed
    -- vocabulary (statuses.REPORT_KINDS is its twin): a typo would silently
    -- create a group of one.
    kind        TEXT NOT NULL DEFAULT 'other'
                CHECK (kind IN ('research', 'grant', 'company', 'sector', 'other')),

    -- The report itself, as markdown. Rendered in the browser, never stored as
    -- HTML: the source stays the thing that was written, and the renderer stays
    -- free to improve without a re-import.
    body_md     TEXT NOT NULL,

    -- Where the markdown came from, e.g. "research/sectors/ea-funding.md".
    -- Provenance only — nothing reads the file at serve time.
    source_path TEXT,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The list view groups by kind and orders newest first; these are its two axes.
CREATE INDEX IF NOT EXISTS idx_report_kind       ON report (kind);
CREATE INDEX IF NOT EXISTS idx_report_updated_at ON report (updated_at DESC);
