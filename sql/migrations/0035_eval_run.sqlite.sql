-- 0035_eval_run — see the .postgres.sql twin for the why.

CREATE TABLE IF NOT EXISTS eval_run (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at           TEXT NOT NULL,
    surface          TEXT NOT NULL,
    set_version      TEXT NOT NULL,
    arm              TEXT NOT NULL,
    model            TEXT NOT NULL,
    effort           TEXT,
    unit             TEXT,
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
