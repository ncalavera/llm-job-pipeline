# Schema migrations

Forward-only schema deltas applied by `scripts/migrate.py`. Run them with:

```bash
python3 scripts/migrate.py            # apply everything pending
python3 scripts/migrate.py --status   # list applied / pending
python3 scripts/migrate.py --baseline # mark all pending as applied WITHOUT
                                      # running them (adopt an already-current DB)
```

Use `--baseline` once when adopting a database that already matches the current
schema (e.g. one created before the migration system existed), so its migrations
are recorded as applied instead of being replayed.

`/jobs-update` runs this automatically after pulling.

## The one rule

**Never edit `sql/schema.sql` or `sql/schema.sqlite.sql` to describe a new
change.** Those two files are the *frozen baseline* — they only build a
brand-new database. Every change after the baseline is a new migration here.

This keeps both paths consistent:

- **New install** — baseline schema builds the DB, then `migrate.py` runs the
  full migration chain on top.
- **Existing install** — `migrate.py` runs only the migrations it hasn't seen.

Both converge on the same schema, with no double-apply.

## Naming

```
0001_add_remote_flag.sql           # portable SQL — runs on both backends
0002_company_index.postgres.sql    # Postgres-only variant
0002_company_index.sqlite.sql      # SQLite-only variant
```

- Four-digit zero-padded number, then a short snake_case label.
- Write portable SQL in `NNNN_label.sql` when you can. When the dialects differ
  (e.g. `JSONB` vs `TEXT`, `TIMESTAMPTZ` vs `TEXT`, index syntax), ship a
  `.postgres.sql` and a `.sqlite.sql` for the same number. The dialect-specific
  file wins over the generic one for the active backend.
- If a change applies to only one backend, ship just that dialect's file — the
  runner records the version as a no-op on the other backend so it never lingers
  as "pending".

## Data safety — you cannot lose data through this runner

- **Automatic backup before every run** with pending work. SQLite is copied with
  the online-backup API (WAL-safe) into `data/backups/`; Postgres is dumped with
  `pg_dump` when that binary is present. Last 10 backups are kept.
- **SQLite auto-restore on failure.** SQLite DDL is not transactional, so a
  migration that fails halfway *would* leave a partial schema — but if anything
  throws, the live database is restored byte-for-byte from the pre-run backup and
  an integrity check confirms it. A failed run is a clean no-op.
- **Postgres is transactional per migration** — migration + ledger row commit
  together, so a failure rolls that one back; earlier migrations stay applied.
- **Destructive statements are blocked by default.** `DROP` (TABLE / COLUMN /
  VIEW / INDEX / SCHEMA / DATABASE / …) / `TRUNCATE` / `DELETE FROM` /
  `ALTER ... DROP` abort the run unless you pass `--allow-destructive`, so an
  accidental data-dropping migration never runs silently. A column backfill
  (`UPDATE ... SET`) is *not* treated as destructive — it's the normal
  additive pattern. The scan ignores keywords inside comments and string
  literals and only matches at statement boundaries.

## Writing safe migrations

- Still prefer small, additive migrations with `IF NOT EXISTS` / `IF EXISTS`
  guards — the auto-restore is a safety net, not an excuse for risky SQL.
- If a migration genuinely must drop or rewrite data, run it with
  `--allow-destructive` and double-check the backup landed first.
- Applied versions live in the `schema_migrations` table on each backend.
