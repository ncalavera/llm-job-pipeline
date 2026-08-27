# Status — applications table + reports tab

Worktree: `/Users/nsolovev/Projects/personal/llm-job-pipeline-tracker`
Branch: `feat/applications-table` (stacked on `migrate/self-hosted-dashboard`)
Updated: 2026-08-27. Not pushed, not deployed.

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

## Not done

1. **Applications table is English while the shell is Russian.** Headers read
   `SENT ON`, `ORGANISATION`; the count strip says "7 sent · 3 waiting". The
   Reports tab has the same gap. Nikita's profile sets the dashboard to RU, so
   this is visible to him on every visit. No i18n keys exist for either.
2. **Stage column falls off a 390px screen.** The Applications table scrolls
   horizontally on a phone and Stage — the answer to the question the table
   exists to ask — is not in the first viewport.
3. **The scroll-to-top button floats over body text mid-scroll.** Global shell
   control, same on every tab; a report is just the first surface long enough
   to make it obvious. Only the end-of-report case is cleared.
4. **`sql/schema.sql` is inconsistent with itself.** It carries the new
   `accepted` status but not `applied_at`, `kind`, or the `report` table. A
   fresh install is fine, because migrations add them on top — but
   `migrate.py --baseline` against a fresh schema would leave a broken
   database. Someone should decide what that file is meant to be.
5. There is no lint configured in this repo (no eslint, no ruff config beyond
   the cache directory).

## Next steps

1. Decide whether the two new tabs get Russian strings before Nikita uses them.
2. Reorder or freeze the Applications table columns so Stage survives a phone.
3. Settle the `schema.sql` question in item 4 above.

## How to run the tests

```bash
cd /Users/nsolovev/Projects/personal/llm-job-pipeline-tracker
npm run test:js     # 529 pass   (note: "npm test" is not a script here)
python3 -m pytest -q # 1719 pass, 38 skipped   (python3, not python)
```

Both were green at commit `0498863`.

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

```bash
ssh forge-n
cd /srv/http/dashboard
git pull
python3 scripts/migrate.py          # applies 0022 + 0023
sudo systemctl restart dashboard    # only for server.js; public/ needs none
```

No `--allow-destructive` in production. This was tested, not assumed: on a
database pending only 0022 and 0023, plain `migrate.py` applies both and takes
its own backup first. The Postgres 0022 routes its CHECK swap through
`EXECUTE`'d dynamic SQL specifically so the destructive gate does not trip.
Re-running is a no-op ("Up to date").

Loading reports afterwards, from the laptop over the tunnel:

```bash
cd ~/Projects/personal/llm-job-pipeline
python3 scripts/vac.py report add <path/to/report.md>   # --kind and --title optional
python3 scripts/vac.py report list
```

`report add` keys on the slug, so re-importing an edited file updates the
stored report rather than forking a second copy.

## Screenshots

`.scratch/shots/` (gitignored) — 390px and 1280px, both colour schemes, plus
four of the real EAIF report.

Note on "both themes": the dashboard has exactly one. There is no
`prefers-color-scheme`, no `[data-theme]`, no toggle, and `--sky-*` is a light
palette. The light/dark pairs in `.scratch/shots/` are byte-identical — that
is the evidence, not an oversight. The "dark theme" comments in `health.js`
are stale.
