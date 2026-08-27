-- 0024_contact_table — the people side of the search.
--
-- The dashboard tracks roles (vacancy), applications (vacancy in an application
-- status) and the reading behind them (report). The one thing it did not hold
-- was people: who to write to, why they matter, which channel actually reaches
-- them, and whether anything has happened yet. That lived in markdown files and
-- a spreadsheet, which means it was never counted and never followed up on a
-- schedule.
--
-- One flat table, like `report`. A contact has a lifecycle but no relations:
-- linking a person to a company would be wrong more often than right (the
-- interesting ones are interesting DESPITE where they work, and several are
-- between jobs), and linking to a vacancy would be wrong always.
--
-- Identity is (name, "group"). Not name alone: the same person can legitimately
-- appear in two lists — a Forum sweep and a referee list — and those are two
-- different reasons to contact them, with different openers and different
-- states. Not email either: most of these people have no email on file, which
-- is the whole reason `channels` exists.
--
-- Fully additive: a new table and its indexes. Nothing here drops, truncates or
-- rewrites anything, so migrate.py's destructive gate has nothing to stop.

CREATE TABLE IF NOT EXISTS contact (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    name        TEXT NOT NULL,
    -- The name as they write it themselves, when that is not the Latin form:
    -- Georgian, Russian, Turkish. Shown next to the Latin name rather than
    -- instead of it, so a message can be addressed the way they spell it.
    name_local  TEXT,

    city        TEXT,
    org         TEXT,
    role        TEXT,

    -- One sentence: why this person, and not the next one. The list is only
    -- useful if it can be re-read cold in three months.
    why_matters TEXT,

    -- Where they can actually be reached, as {channel: handle-or-url}. A JSON
    -- object rather than eight columns because the set is open (a Calendly
    -- appears, a Bluesky will) and because most people have two or three of
    -- them — eight columns would be mostly NULL, and adding the ninth would be
    -- a migration.
    channels    JSONB NOT NULL DEFAULT '{}',

    -- Which list this person came from. A closed vocabulary is deliberately NOT
    -- enforced here: the groups are working sets that come and go with each
    -- sweep, and a CHECK would turn "I made a new list today" into a migration.
    -- statuses.py CONTACT_GROUPS holds the known ones for the UI filter.
    "group"     TEXT NOT NULL DEFAULT 'other',

    -- Where it stands. Closed, because this one IS the funnel and a typo would
    -- silently drop someone out of every count.
    --   planned   — on the list, nothing sent
    --   contacted — a message went out, no answer yet
    --   replied   — they answered; the ball is with him
    --   met       — a call or a coffee happened
    --   declined  — they said no, or made clear this is not a fit
    --   stale     — sent long enough ago that it is not coming back
    status      TEXT NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned', 'contacted', 'replied',
                                  'met', 'declined', 'stale')),
    status_at   TIMESTAMPTZ,

    -- Their last visible activity (a Forum post, a job change). Free text, not
    -- a date: the sources say "2024-11-18 Forum comment" or "listed on the team
    -- page 2026-08-27", and flattening that to a timestamp throws away which
    -- signal it was — the difference between "posts here" and "still employed".
    last_active TEXT,

    -- The first line to send them. Written per person, and the single most
    -- expensive thing to reproduce, so it is stored rather than regenerated.
    opener      TEXT,
    notes       TEXT,

    -- The markdown or CSV this contact was read out of, so any row can be
    -- traced back to the sweep that produced it.
    source_path TEXT,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The identity the importer upserts on. A unique index rather than a composite
-- primary key so the surrogate id stays stable across re-imports.
CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_identity ON contact (name, "group");

-- The list view's two axes: filter by group, count and sort by status.
CREATE INDEX IF NOT EXISTS idx_contact_group  ON contact ("group");
CREATE INDEX IF NOT EXISTS idx_contact_status ON contact (status);
