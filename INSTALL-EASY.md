# Install Guide — Easy Mode

The no-account, no-server path. Everything runs on your own machine against a
local database file. No Supabase, no Vercel, no SQL editor, no cloud signups.

If you are comfortable with databases and want multi-device sync and a daily
phone digest, read [INSTALL.md](INSTALL.md) (the hardcore path) instead. You can
upgrade from easy to hardcore later without losing anything.

## What you get

- A local **SQLite** database (`data/jobsearch.db`) created automatically — no
  database account, no schema to run by hand.
- Scripts that fetch vacancies from company career pages (Greenhouse, Lever,
  Ashby, Workable, Workday and more).
- LLM scoring of every vacancy against *your* profile by your coding agent
  subagents (uses your existing Claude subscription — no API key).
- A dashboard you open on **`localhost`** — no Vercel, no password wall.

## What you give up vs. hardcore (so you choose honestly)

| | Easy (this guide) | Hardcore ([INSTALL.md](INSTALL.md)) |
|---|---|---|
| Accounts to create | **none** | Supabase (free), Vercel (free) |
| Database | local SQLite file | hosted Postgres (Supabase) |
| Dashboard | `localhost`, while your terminal is open | always-on URL, any device |
| Use from your phone | no | yes (the hosted dashboard) |
| Daily Telegram digest with 👍/👎 | no (needs a server) | yes |
| Multi-device / share with a friend | no | yes |
| Setup time | **~5 min, no signups** | ~15 min, mostly signups |
| Your data leaves your machine | no | yes (lives in Supabase) |

Everything else — fetching, filtering, company review, scoring quality, the
dashboard UI — is identical. A board/ATS-discovered company lands in the same
review gate on both (config default: `candidate`, approved via `/jobs-review`
or the same opt-in auto-approve threshold) — easy mode does not skip it.

## 1. Prerequisites

- **Python 3.11+** with `pip`
- **A coding agent** — [Claude Code](https://claude.com/claude-code) recommended,
  Codex and others work too (see AGENTS.md) — used both for setup and as
  the scoring engine

That's it. No database account, no API keys required to start.

## 2. Clone and install

```bash
git clone https://github.com/ncalavera/llm-job-pipeline
cd llm-job-pipeline
python3 -m pip install requests beautifulsoup4 python-dateutil
./scripts/install-hooks.sh   # one-time: blocks committing private data to this public repo
```

If that errors with **`externally-managed-environment`** (PEP 668, common on
recent macOS/Linux Python), use a virtual environment instead:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install requests beautifulsoup4 python-dateutil
```

(Or, if you prefer not to use a venv, `pip install --user requests beautifulsoup4 python-dateutil`.)
With the venv active, run every later `python3 ...` command from the same shell.

Those three packages cover fetching and the local database. (You do **not** need
`psycopg2-binary` — that is only for the Supabase path. Installing the full
`requirements.txt` also works and does no harm.)

If you want to run the test suite (`python3 -m pytest tests/ -q`), also install
`pytest`: `pip install pytest`.

You do not create or configure a database. The first time anything connects, the
pipeline creates `data/jobsearch.db` and its tables for you.

> **⚠️ Already have `SUPABASE_DB_URL` set for another project?**
> Simple mode picks SQLite *only when `SUPABASE_DB_URL` is unset*. If your shell
> exports it (from another project, a `.zshrc` line, etc.), this repo will
> silently write into that other Postgres instead of the local file. Unset it
> for this repo's shell:
> ```bash
> unset SUPABASE_DB_URL SUPABASE_DIRECT_URL
> ```
> Every fetch/score/filter run prints which backend it used —
> `Backend: local SQLite (...)` is what you want here. If you see
> `Backend: Postgres (Supabase)`, stop and unset the variable.

## 2b. Apply schema migrations

The first time the pipeline runs it creates the database automatically. After
that — and on every upgrade — apply any pending schema changes:

```bash
python3 scripts/migrate.py
```

This takes an automatic backup before touching anything and is safe to re-run
(already-applied migrations are skipped). If nothing is pending, it prints
"Up to date" and exits cleanly.

Skip this on a brand-new clone — `/jobs-new` (next step) will do it for you.

## 3. One command: `/jobs-new`

Open your agent in the repo and run (non-Claude agents: follow
`.claude/commands/jobs-new.md`):

```
/jobs-new
```

It will:

1. Confirm you are on the local SQLite backend.
2. Set up `config/user_profile.md` (it copies the example and asks you to
   describe yourself — field, target roles, locations, visa status). This file
   is the single biggest lever on scoring quality.
3. Web-search 10–15 real companies that match your field and geography,
   validate each one's careers page, and show you the shortlist for a yes/no.
4. Add the approved companies, then run the first fetch → filter → score.
5. Launch the local dashboard and open it in your browser.

That is the whole onboarding. No copy-pasting connection strings, no SQL.

Without an agent? You can do step 2 by hand — copy the example profile and edit
it — then add companies with `/jobs-add` (or its runbook `.claude/commands/jobs-add.md`):

```bash
cp config/user_profile.example.md config/user_profile.md
# then edit config/user_profile.md to describe yourself
```

## 4. Every day: `/jobs-new`

```
/jobs-new
```

One command does the daily loop: fetch new vacancies → filter junk → score the
new ones → show you the top fresh matches in chat → capture your like/pass
straight into the database → refresh the dashboard.

Prefer clicking through cards? Run the dashboard any time:

```bash
python3 scripts/dashboard_local.py
```

It serves the dashboard at `http://127.0.0.1:8000/` against your local database.
The like/pass and company approve/reject buttons save locally. Keep the terminal
open while you review; press Ctrl+C to stop.

## Adding more companies later

```
/jobs-add Khan Academy
```

Auto-detects the ATS, adds it, runs a test fetch — same as hardcore mode.

## Optional: job boards

The pipeline can also pull from six free job boards. They are **off by
default** because each is niche and noisy outside its sector. Pick the ones
that match your search and turn them on per run with `JOB_BOARDS`:

| Board | Relevant if you search in | Extra env |
| --- | --- | --- |
| `80k_hours` | effective altruism / AI safety / policy | — |
| `reliefweb` | humanitarian / development NGOs | — |
| `arbeitnow` | European tech, visa sponsorship | `ARBEITNOW_VISA_ONLY=1` keeps only sponsorship jobs |
| `remotive` | remote-first roles | `REMOTIVE_CATEGORIES=product,marketing` narrows by category |
| `weworkremotely` | remote product / business roles | `WWR_CATEGORIES=product,marketing` picks the RSS feeds |
| `hn_whoishiring` | startups (monthly Hacker News thread) | — |

```bash
JOB_BOARDS=arbeitnow,remotive python3 scripts/fetch_vacancies.py
# or JOB_BOARDS=all to enable every defined board
```

Leave `JOB_BOARDS` unset and only your tracked companies are fetched.

**Boards behind logins (LinkedIn, Devex, …):** the pipeline deliberately ships
no importers for them; if you accept the terms-of-service risk, ask your agent
to write a personal importer that feeds `save_vacancies()` — keep it out of
public forks.

## Upgrading to hardcore later

Point `.env` at Supabase and the exact same scripts talk to Postgres instead of
SQLite — no code changes. Two things easy mode skipped are required now:

1. **Install the Postgres driver.** Easy mode never installed `psycopg2`, so the
   full-mode scripts would fail with a driver error. Install everything:
   ```bash
   python3 -m pip install -r requirements.txt
   ```
   (Skip this and you get a clear "psycopg2 is not installed — pip install -r
   requirements.txt" error, not a crash.)
2. **Fill `.env` with Supabase.** Set `SUPABASE_DB_URL` (and the dashboard env
   vars). The scripts auto-load the repo-root `.env` — no manual `export`. Every
   fetch/score/filter run prints its backend; confirm it says
   `Backend: Postgres (Supabase)`.

Then follow [INSTALL.md](INSTALL.md) from step 3 (create the Supabase project and
schema), and re-add your companies (or migrate the SQLite rows). Your commands
(`/jobs-new`, `/jobs-review`, `/jobs-add`, …) keep working.

## Troubleshooting

- **"Run `--report-only` first" warning when starting the dashboard** — the
  dashboard needs `public/data.js`. Run
  `python3 scripts/fetch_vacancies.py --report-only` (or just run `/jobs-new` once).
- **Fetch returns 0 jobs for a company** — its ATS may be unsupported. Run
  `python3 scripts/discover_ats.py --company "Name"` to re-detect.
- **Scoring feels off** — sharpen `config/user_profile.md`. The prompt only
  knows what you wrote there; add explicit "not a target" lines.
- **Where is my data?** — one file: `data/jobsearch.db`. Back it up by copying
  it. Delete it to start over (it rebuilds on next run).
