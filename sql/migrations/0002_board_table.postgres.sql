-- 0002_board_table — job-board catalog + fetch status as a first-class table.
--
-- Until now a board's fetch state (last_fetched) was stored on a fake `company`
-- row whose canonical_name was the board id, which polluted the dashboard's
-- company list with board slugs (a16z, arbeitnow, …). Boards are not companies:
-- they get their own table. The catalog (name/strategy/tier/ttl_days/url) is
-- kept in sync from config (JOB_BOARDS) by sync_boards(); last_fetched is
-- written by mark_board_fetched().
--
-- The data move (backfill last_fetched from the old company rows, then delete
-- those rows) is config-dependent, so it runs once via
-- scripts/migrate_boards_out_of_company.py — not here.
CREATE TABLE IF NOT EXISTS board (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    strategy      TEXT,
    tier          TEXT CHECK (tier IS NULL OR tier IN ('S', 'A', 'B', 'C')),
    ttl_days      INTEGER,
    url           TEXT,
    last_fetched  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
