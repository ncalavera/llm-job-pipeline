---
title: Data-safe schema migrations for a dual-backend (SQLite + Postgres) app
category: database-issues
date: 2026-06-14
tags: [migrations, sqlite, postgres, supabase, data-safety, schema, backup]
---

## Problem

A self-hosted app where each user owns their database (local SQLite in simple
mode, hosted Supabase Postgres in full mode) had no migration mechanism — `git
pull` shipped new code that expected schema changes the user's DB didn't have,
breaking silently. The fix had to guarantee a user can **never lose data** while
applying schema deltas, across two SQL dialects.

## Root cause

`git pull` updates code but not schema. The two backends apply the same logical
schema through different SQL (`JSONB` vs `TEXT`, `TIMESTAMPTZ` vs `TEXT`, index
syntax), and SQLite DDL is **not transactional** — a multi-statement migration
that fails halfway leaves a partial, corrupt schema.

## Solution

A forward-only runner (`scripts/migrate.py`) with a ledger + safety net:

- **Frozen-baseline contract.** `sql/schema.sql` / `schema.sqlite.sql` are
  frozen at "today" and only build a brand-new DB. Every later change is a
  numbered migration in `sql/migrations/` (`0001_label.sql`, or dialect-specific
  `0001_label.sqlite.sql` / `.postgres.sql`). Fresh install = baseline + all
  migrations; existing install = only the new ones. Both converge, no
  double-apply. Documented loudly so nobody edits the frozen files.
- **Per-DB ledger** `schema_migrations(version, applied_at)` → idempotency.
- **Automatic pre-run backup.** SQLite via the online-backup API (WAL-safe);
  Postgres via `pg_dump` when available. Rotate, keep last N.
- **SQLite auto-restore on failure.** Wrap the whole run; on any exception,
  copy the backup back over the live file and `PRAGMA integrity_check`. A failed
  run becomes a clean no-op (Postgres gets this free — DDL is transactional, so
  migration + ledger row commit together).
- **Destructive-statement gate.** Block `DROP (TABLE/COLUMN/VIEW/INDEX/SCHEMA/
  DATABASE/…)` / `TRUNCATE` / `DELETE FROM` / `ALTER ... DROP` unless
  `--allow-destructive`. Crucial subtleties learned in review:
  - Strip comments and string literals **before** scanning, and anchor matches
    to statement starts (`(?:^|;)\s*`), or a column named `dropped_at` or a
    `-- DROP ...` comment trips the gate (false positive → trains users to pass
    the unsafe flag).
  - Do **not** treat `UPDATE ... SET` as destructive — column backfill is the
    normal additive pattern.
  - Cover `DROP SCHEMA/DATABASE/CONSTRAINT`, not just `DROP TABLE/COLUMN`, or a
    catastrophic statement slips through (false negative).
- **`--baseline`** records all pending migrations as applied without running
  them, to adopt a DB that predates the runner (avoids "permanently stuck
  replaying" for legacy installs).
- **Reuse the app's own DB connector** (`db_backend._connect_supabase`) so
  migrations inherit keepalives/timeouts/retry instead of a bare
  `psycopg2.connect` that hangs on a flaky pooler.

Read each migration file once and reuse the text for both the destructive scan
and the apply loop.

## Prevention

Add regression tests that lock the safety contract, not just the happy path:
fresh-baseline bootstrap, idempotent re-run, automatic backup created,
**failed-migration auto-restore leaves the DB byte-identical**, destructive gate
over/under-match cases (UPDATE allowed, comments ignored, `DROP SCHEMA` caught),
and `--baseline`. The auto-restore test is the one that proves "you cannot lose
data" — assert the would-be-added column is absent and the ledger unchanged
after a deliberately-broken migration.
