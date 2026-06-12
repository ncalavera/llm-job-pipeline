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
- LLM scoring of every vacancy against *your* profile via Claude Code
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

Everything else — fetching, filtering, scoring quality, the dashboard UI — is
identical.

## 1. Prerequisites

- **Python 3.11+** with `pip`
- **[Claude Code](https://claude.com/claude-code)** — used both for setup and as
  the scoring engine

That's it. No database account, no API keys required to start.

## 2. Clone and install

```bash
git clone https://github.com/ncalavera/llm-job-pipeline
cd llm-job-pipeline
pip install requests beautifulsoup4 python-dateutil
```

Those three packages cover fetching and the local database. (You do **not** need
`psycopg2-binary` — that is only for the Supabase path. Installing the full
`requirements.txt` also works and does no harm.)

You do not create or configure a database. The first time anything connects, the
pipeline creates `data/jobsearch.db` and its tables for you.

## 3. One command: `/start`

Open Claude Code in the repo and run:

```
/start
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

## 4. Every day: `/jobs`

```
/jobs
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
/add-source Khan Academy
```

Auto-detects the ATS, adds it, runs a test fetch — same as hardcore mode.

## Upgrading to hardcore later

Set `SUPABASE_DB_URL` (and the dashboard env vars) and the exact same scripts
talk to Supabase instead of SQLite — no code changes. Follow
[INSTALL.md](INSTALL.md) from step 3, then re-add your companies (or migrate the
SQLite rows). Your commands (`/fetch`, `/score`, `/triage`, …) keep working.

## Troubleshooting

- **"Run `--report-only` first" warning when starting the dashboard** — the
  dashboard needs `public/data.js`. Run
  `python3 scripts/fetch_vacancies.py --report-only` (or just run `/jobs` once).
- **Fetch returns 0 jobs for a company** — its ATS may be unsupported. Run
  `python3 scripts/discover_ats.py --company "Name"` to re-detect.
- **Scoring feels off** — sharpen `config/user_profile.md`. The prompt only
  knows what you wrote there; add explicit "not a target" lines.
- **Where is my data?** — one file: `data/jobsearch.db`. Back it up by copying
  it. Delete it to start over (it rebuilds on next run).
