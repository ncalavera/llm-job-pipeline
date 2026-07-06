-- 0012_pipeline_run — durable, per-stage run history (DHA-434 / DHA-438 BUG-3).
--
-- The run_daily.py refactor dropped the write to this table: prod held only 2
-- rows (last 2026-06-16) and, even those, only an end-of-run counts summary with
-- no per-stage status or timing. This migration revives pipeline_run with the
-- shape run_daily.record() now writes on EVERY run: one row per run, updated as
-- each stage completes, so a finished OR killed run can be reviewed later — not
-- only watched live via run_card.py.
--
-- Idempotent BOTH ways. A fresh install runs the CREATE and gets the full shape.
-- The legacy prod table already exists, so the CREATE is a no-op there; the
-- ALTER ... ADD COLUMN IF NOT EXISTS block then upgrades it in place, adding the
-- per-stage/timing columns without touching its two historical rows (the old
-- `counts` / `errors` columns are reused; `run_at` is kept and set by the
-- writer so a legacy NOT NULL constraint is satisfied).
--
-- Postgres only: the SQLite demo never had this table, so its variant is a plain
-- CREATE (see .sqlite.sql) with no legacy to upgrade.
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id            TEXT PRIMARY KEY,
    run_at            TIMESTAMPTZ NOT NULL DEFAULT now(),  -- run start (legacy-compatible name)
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished          BOOLEAN NOT NULL DEFAULT FALSE,
    status            TEXT NOT NULL DEFAULT 'running',     -- running | done | gate | error | aborted
    boards            TEXT,
    new_vacancies     INTEGER,                             -- key health counter (DHA-415 range check)
    scored            INTEGER,
    companies_scored  INTEGER,
    stages            JSONB,                               -- [{name,status,note,started_at,finished_at}]
    counts            JSONB,                               -- run-level rollup {new_vacancies, ...}
    errors            JSONB
);

-- Upgrade a legacy pipeline_run (run_at / counts / archived_items / errors only)
-- to the shape above. Every add is guarded, so replaying against an already-new
-- table is a clean no-op.
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS run_id           TEXT;
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS run_at           TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS finished         BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS status           TEXT NOT NULL DEFAULT 'running';
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS boards           TEXT;
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS new_vacancies    INTEGER;
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS scored           INTEGER;
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS companies_scored INTEGER;
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS stages           JSONB;
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS counts           JSONB;
ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS errors           JSONB;

-- run_id is the writer's upsert key. The fresh CREATE makes it the PRIMARY KEY;
-- on the legacy table it is added as a plain column, so add a UNIQUE index
-- (guarded) to keep one row per run there too. The writer never relies on
-- ON CONFLICT (it UPDATEs, then INSERTs on rowcount 0), so this index is a
-- data-hygiene belt, not a correctness dependency.
CREATE UNIQUE INDEX IF NOT EXISTS pipeline_run_run_id_key ON pipeline_run (run_id);
