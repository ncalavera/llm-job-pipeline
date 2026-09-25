-- 0035_eval_run — one row per scored eval run (a model or brief bake-off).
--
-- An eval runs an arm (model + effort + brief) over a frozen set of roles with
-- a known human verdict, and counts how often the arm agrees. The scorer in the
-- private eval repo inserts one row per run; the screener Runs tab reads the
-- table and shows the mean and range over repeats per arm. Counts are roles,
-- out of `n`: `lost` = a wanted role the arm removes (after the production
-- confidence + quote rule), `lost_raw` = the same on the raw verdict, `extra` =
-- an unwanted role it keeps, `missing` = no valid answer after retries.
--
-- Fully additive: one new table, guarded with IF NOT EXISTS like 0033.

CREATE TABLE IF NOT EXISTS eval_run (
    id               BIGSERIAL PRIMARY KEY,
    ran_at           TIMESTAMPTZ NOT NULL,
    surface          TEXT NOT NULL,          -- 'judge' | 'scorer'
    set_version      TEXT NOT NULL,
    arm              TEXT NOT NULL,
    model            TEXT NOT NULL,
    effort           TEXT,
    unit             TEXT,                   -- e.g. 'batch-15', 'single'
    prompt_version   TEXT,
    n                INTEGER NOT NULL,
    agreement        INTEGER NOT NULL,
    lost             INTEGER NOT NULL,
    lost_raw         INTEGER NOT NULL,
    extra            INTEGER NOT NULL,
    missing          INTEGER NOT NULL,
    tokens_per_role  INTEGER,
    seconds_per_role REAL,
    repeat_no        INTEGER NOT NULL,
    run_dir          TEXT NOT NULL UNIQUE,
    notes            TEXT
);
