# Status — applications table, reports tab, networking tab

Worktree: `/Users/nsolovev/Projects/personal/llm-job-pipeline-tracker`
Branch: `feat/applications-table` (stacked on `migrate/self-hosted-dashboard`)
Updated: 2026-08-27. Not pushed. The Reports-tab commits through `426982f`/
`0498863` are deployed; everything after `6de1346` is not.

## The Reports tab is deployable

This was the priority. It works end to end and was verified against the real
report, not a fixture: `research/grants/eaif-close-grants-2026-08-27.md` —
465 lines, 299 list items, 21 headings, 9,825 words. It renders with no
console errors and no horizontal page scroll at 390px or 1280px.

## Done

1. Status `accepted`, migration 0022 (`accepted` CHECK, `vacancy.applied_at`,
   `vacancy.kind`), `applied_at` written once on the first move into the
   funnel and never overwritten.
2. Applications table — the Triage tab's second view, Board | Table toggle
   remembered in localStorage.
3. `vac.py add` for applications that never came from a job board.
4. Reports: table `report` (migration 0023), `GET`/`POST /api/reports`,
   `vac.py report add` / `report list`, markdown rendered in the browser.
5. The report body now uses the dashboard's own sky/cobalt tokens instead of
   falling back to the retired warm `.md-content` palette. Three classes the
   renderer emits had no CSS at all and now do: `.md-anchor` (a heading
   self-link that was printing a literal coral `#` welded to every heading),
   `.md-num`, `.md-jump`.
6. Table alignment is a property of the column, not the cell — the header
   moves with the figures it names, and one "not reported" no longer swings a
   money column back to the left.
7. Money is set in mono; prose is not. Found by rendering the real report,
   where bullets open as data and finish as a sentence.
8. Both new tabs are translated (~60 keys, both languages), the Applications
   table's Stage column survives a 390px screen, and `--baseline` now verifies
   the database instead of trusting a claim about it.
9. **Networking tab** — migration 0024 (`contact`), `GET/POST/PATCH
   /api/contacts`, `vac contact import|add|list`, a table with a group filter
   and a details drawer carrying the opener and a copy button. 39 real people
   imported across five lists. i18n in both languages from the start.

## The contact data lives in the PRIVATE repo

`llm-job-pipeline` is public. The importer is here; the people are not, and
must not be committed here. The three source files live in
`~/Projects/personal/job-search-2026/profiles/`:

| file | rows | group | status |
| --- | --- | --- | --- |
| `ea-network-people-2026-08-27.csv` | 27 | derived: ea-georgia / ea-turkey / ea-russian | planned |
| `ea-forum-open-contacts-2026-08.csv` | 7 | ea-forum-open | planned (1 contacted) |
| `network-wave-2026-07.csv` | 5 | network-2026-07 | contacted (1 replied) |

The last two were written by hand from
`profiles/ea-forum-open-contacts-2026-08.md` and
`docs/network-drafts-2026-07.md`; each row's `source_path` points back at the
markdown it was read from. The July file is mostly message drafts rather than a
people list, so a parser would have been guesswork — five people were lifted
out of it by hand.

Import them with:

```bash
cd ~/Projects/personal/llm-job-pipeline
P=~/Projects/personal/job-search-2026/profiles
python3 scripts/vac.py contact import $P/ea-network-people-2026-08-27.csv \
    --group ea-russian --derive-region --source profiles/ea-network-people-2026-08-27.csv
python3 scripts/vac.py contact import $P/ea-forum-open-contacts-2026-08.csv \
    --group ea-forum-open --source profiles/ea-forum-open-contacts-2026-08.md
python3 scripts/vac.py contact import $P/network-wave-2026-07.csv \
    --group network-2026-07 --source docs/network-drafts-2026-07.md
```

Re-running any of them updates those rows rather than forking the list, and a
re-import never overwrites a status changed in the UI.

## Not done

1. **The Health tab is untranslated** — 26 keys referenced by `health.js` and
   `index.html` (plus `tab_health`) exist in neither language. Also
   `archive_restore` in archive.js and `triage_source_link` in pipeline.js.
   These are pre-existing gaps on the deployed branch, outside the two tabs
   this round covered. `tests/test_i18n_coverage.py` currently scans only
   `applications.js` and `reports.js`; widening it to every module is the
   natural follow-up, and would fail until those keys are added.
2. **The scroll-to-top button floats over body text mid-scroll.** Global shell
   control, same on every tab; a report is just the first surface long enough
   to make it obvious. Only the end-of-report case is cleared. Left alone by
   instruction.
3. **`sql/schema.sql` is far from current** — it is missing six tables
   (`application`, `board`, `company_evidence`, `learning_log`,
   `pipeline_run`, `report`) and eight columns, not just this branch's three.
   It is the documented frozen baseline, so it was deliberately NOT topped up;
   `--baseline` now verifies the database instead, which is what actually made
   it safe. Whether that file should become a true current-state dump is still
   an open decision.
4. There is no lint configured in this repo (no eslint, no ruff config beyond
   the cache directory).

## Next steps

1. Translate the Health tab (26 keys) and widen `test_i18n_coverage.py` to
   scan every module rather than the two new tabs.
2. Settle what `sql/schema.sql` is meant to be — frozen baseline, or a
   current-state dump that `--baseline` can trust on its own.

## A trap worth knowing

`unset SUPABASE_DB_URL` does NOT select SQLite in this repo. `db_backend`
loads the repo `.env` at import with `setdefault`, so an unset variable is
immediately refilled from the file and the code targets the FORGE PRODUCTION
database through the tunnel on 127.0.0.1:15432. To force SQLite, use the
documented escape hatch:

```bash
LLM_PIPELINE_DISABLE_DOTENV=1 JOBSEARCH_DB_PATH=/tmp/x.db python3 scripts/migrate.py
```

Otherwise always set `SUPABASE_DB_URL` explicitly to a throwaway. Check the
connection banner every script prints — it names the database it actually
reached.

## How to run the tests

```bash
cd /Users/nsolovev/Projects/personal/llm-job-pipeline-tracker
npm run test:js     # 578 pass   (note: "npm test" is not a script here)
python3 -m pytest -q # 1831 pass, 38 skipped   (python3, not python)
```

Both were green at commit `849aafe`.

## How to see it locally

The Node server needs Postgres (`DATABASE_URL`); it has no SQLite path. Never
point it at `127.0.0.1:15432` — that is the tunnel to the live forge database.
Stand up a throwaway instead:

```bash
initdb -D /tmp/shots-pg -U shotuser --auth=trust
pg_ctl -D /tmp/shots-pg -o "-p 15499 -k /tmp/pgshots -c listen_addresses=127.0.0.1" -l /tmp/pg.log start
createdb -h 127.0.0.1 -p 15499 -U shotuser jobsearch_shots

export SUPABASE_DB_URL="postgresql://shotuser@127.0.0.1:15499/jobsearch_shots"
psql -q "$SUPABASE_DB_URL" -f sql/schema.sql
python3 scripts/migrate.py --allow-destructive   # fresh DB only; see note below

python3 scripts/vac.py report add ~/Projects/personal/job-search-2026/research/grants/eaif-close-grants-2026-08-27.md
DATABASE_URL="$SUPABASE_DB_URL" PORT=8799 node server.js
```

The snapshot that feeds `/api/vacancies` is built separately, and two guards
will stop you: the shared-snapshot profile guard (set `USER_PROFILE_PATH` to
the main checkout's `config/user_profile.md`) and the prod-write guard (set
`JOBSEARCH_ALLOW_PROD_WRITE=1`). Both are correct to fire — satisfy them
against the throwaway, never by pointing anything at forge.

`--allow-destructive` above is only needed because migration 0018 is in the
chain on a from-scratch database. It is NOT needed in production; see below.

## Deploying to forge

Per `~/.claude/skills/levelsio/SKILL.md`: the repo lives on the server, there
is no CI, and the dashboard is `dashboard.service` on 127.0.0.1:3001 with its
code at `/srv/http/dashboard`.

**`scripts/migrate.py` does NOT run on forge.** The box has no psycopg2, and
`jobsearch_app` does not own the tables, so the runner cannot connect and could
not ALTER them if it did. Migrations go in by hand as the `postgres` superuser,
and the three steps below are one unit — a migration applied without its ledger
row will be offered again on the next run, and a table created by `postgres`
without the ownership hand-off is unwritable by the app.

```bash
# 1. code
rsync -a --delete <local>/ root@forge:/srv/http/dashboard/     # as root
ssh forge 'chown -R dashboard:dashboard /srv/http/dashboard'   # hand it back

# 2. schema, as the superuser
ssh forge
sudo -u postgres psql -d jobsearch -v ON_ERROR_STOP=1 \
  -f /srv/http/dashboard/sql/migrations/0024_contact_table.postgres.sql

# 3. record it, and hand any NEW table to the app user
sudo -u postgres psql -d jobsearch -c "INSERT INTO schema_migrations (version) VALUES ('0024');"
sudo -u postgres psql -d jobsearch -c "ALTER TABLE contact OWNER TO jobsearch_app;"

# 4. restart (only for server.js / scripts; public/ needs none)
sudo systemctl restart dashboard
```

Check the result with `python3 scripts/migrate.py --status` FROM THE LAPTOP
over the tunnel — that is the one place the runner can reach the database. It
should report `0 pending`. If it reports the migration as pending, step 3 was
missed; if `--baseline` looks like the fix, it is not — it now refuses a
database that is not actually current, and the right move is to apply what is
missing.

Loading reports and applications afterwards, from the laptop over the tunnel:

```bash
cd ~/Projects/personal/llm-job-pipeline
python3 scripts/vac.py report add <path/to/report.md>   # --kind and --title optional
python3 scripts/vac.py report list
python3 scripts/vac.py add --company "…" --title "…" --kind grant --status applied
```

`report add` keys on the slug, so re-importing an edited file updates the
stored report rather than forking a second copy. `vac add` keys on company +
title, so re-running it edits the application rather than adding a second one —
and it now sets the company active, because an application must not be hidden
by a company status set before he applied.

## Screenshots

`.scratch/shots/` (gitignored) — 390px and 1280px, both colour schemes, plus
four of the real EAIF report.

Note on "both themes": the dashboard has exactly one. There is no
`prefers-color-scheme`, no `[data-theme]`, no toggle, and `--sky-*` is a light
palette. The light/dark pairs in `.scratch/shots/` are byte-identical — that
is the evidence, not an oversight. The "dark theme" comments in `health.js`
are stale.
