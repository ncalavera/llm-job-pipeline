# Install Guide

Step-by-step setup. Written so a coding agent (Claude Code, Codex, or similar) can
execute it for you — paste the setup prompt from the
[onboarding page](https://ncalavera.github.io/llm-job-pipeline/) into your
agent, or follow the steps manually.

Steps marked **[human]** need you; everything else an agent can do.

## What you'll end up with

- A Supabase (Postgres) database with your companies and vacancies
- Scripts that fetch vacancies from company career pages (Greenhouse, Lever,
  Ashby, Workable, Workday and more — no scraping subscriptions needed for most)
- LLM scoring of every vacancy against *your* profile by your coding agent
  (uses the subscription you already have, no API key; see AGENTS.md)
- A dashboard to triage results (optional, deploys to Vercel)
- A daily Telegram digest with 👍/👎 buttons (optional)

Time: ~15 minutes of human attention, mostly account signups.

## 1. Prerequisites

- **Python 3.11+** with `pip`
- **A coding agent** — [Claude Code](https://claude.com/claude-code) recommended,
  Codex and others work too (see AGENTS.md) — used both for setup and
  as the scoring engine
- **[human]** A free [Supabase](https://supabase.com) account (free tier is plenty)
- Optional: a [Firecrawl](https://firecrawl.dev) API key — only needed to
  enrich descriptions from career pages without a parseable ATS
- Optional: a Telegram bot token (via [@BotFather](https://t.me/BotFather)) —
  only for the daily digest

## 2. Clone and install dependencies

```bash
git clone https://github.com/ncalavera/llm-job-pipeline
cd llm-job-pipeline
pip install -r requirements.txt
./scripts/install-hooks.sh   # activate the private-data pre-commit guard
```

`install-hooks.sh` points `core.hooksPath` at the tracked `hooks/`, so a
pre-commit guard blocks you from ever committing your profile, `.env`,
`public/data.js`, or other private files to this public repo. Run it once.

## 3. Create the database

1. **[human]** Create a new Supabase project (any name, pick a region near you).
2. In the Supabase dashboard open **SQL Editor**, paste the contents of
   `sql/schema.sql`, run it. This creates the two tables (`company`,
   `vacancy`) and indexes.
3. Copy the connection string: **Project Settings → Database → Session pooler**.

## 3b. Apply schema migrations

After creating the database (step 3), apply any pending schema changes:

```bash
python3 scripts/migrate.py
```

`migrate.py` auto-loads `SUPABASE_DB_URL` from the repo-root `.env` (or the
environment) to pick the right backend, so run it any time after you fill in
`.env` (step 4). It takes an automatic backup before any change and is safe to
re-run; already-applied migrations are skipped.

## 4. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

- `SUPABASE_DB_URL` — the Session pooler connection string from step 3
- `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` — from **Project Settings → API**
  (only needed if you deploy the dashboard)
- `FIRECRAWL_API_KEY` — optional (see prerequisites)

The Python scripts auto-load `.env` from the repo root — no manual `export`
needed. A variable you already exported in your shell takes priority over the
file. Never commit `.env`.

## 5. Create your profile

```bash
cp config/user_profile.example.md config/user_profile.md
```

Edit `config/user_profile.md` — this is the single most important file.
The scoring prompts substitute it into every LLM call, so the quality of
scoring directly depends on how honest and specific this file is.

If you came from the [onboarding page](https://ncalavera.github.io/llm-job-pipeline/),
paste the generated profile here instead.

## 6. Add your first companies

Open your agent in the repo and use the command (non-Claude agents: follow
`.claude/commands/jobs-add.md`):

```
/jobs-add Stripe
```

It auto-detects the company's ATS (Greenhouse, Lever, Ashby, …), adds it to
the database and runs a test fetch. Add 5–10 companies you actually care
about. You can also bulk-import from a CSV — see `examples/companies.example.csv`.

## 7. Run the pipeline

In your agent:

```
/jobs-new     # fetch → filter → score vacancies against your profile
```

Scoring runs inside your agent — one vacancy per request (see AGENTS.md), scored
0–100 against your profile, with reasoning, tags and a summary saved to the
database.

Then triage from the terminal:

```
python3 scripts/vac.py list           # top unseen vacancies by score
python3 scripts/vac.py show <id>      # full description + scoring reasoning
python3 scripts/vac.py mark <id> liked
```

(Inside your agent the same thin CLI is `/jobs-review vac list`, `… show <id>`,
`… mark <id> liked`.)

### Optional: job boards

Besides your tracked companies, a set of free job boards is built in — all
**off by default** (they are niche and noisy outside their sectors). The full
list with per-board audience is
[docs/job-boards-catalogue.md](docs/job-boards-catalogue.md); a few common
starters:

| Board | Fits | Extra env |
| --- | --- | --- |
| `80k_hours` | EA / AI safety / policy | — |
| `reliefweb` | humanitarian / development | — |
| `arbeitnow` | European tech, visa sponsorship | `ARBEITNOW_VISA_ONLY=1` |
| `remotive` | remote-first roles | `REMOTIVE_CATEGORIES=product,marketing` |
| `weworkremotely` | remote product / business roles | `WWR_CATEGORIES=product,marketing` |
| `hn_whoishiring` | startups (monthly HN thread) | — |

Enable one so it persists (`python3 scripts/sources.py enable-board <id>`), or
turn some on for a single run with the `JOB_BOARDS` env var:

```bash
JOB_BOARDS=arbeitnow,remotive python3 scripts/fetch_vacancies.py
```

**Boards behind logins (LinkedIn, Devex, …):** the pipeline deliberately ships
no importers for them; if you accept the terms-of-service risk, ask your agent
to write a personal importer that feeds `save_vacancies()` — keep it out of
public forks.

## 8. Dashboard (optional)

The `public/` folder is a static dashboard (vanilla JS) reading data baked
into `data.js` plus live statuses from the Vercel API routes.

```bash
npx vercel deploy   # from the repo root
```

Set env vars in Vercel: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`AUTH_USER`, `AUTH_PASS`. The middleware protects the whole dashboard with
HTTP Basic Auth — it shows your private job search, keep a password on it.

Regenerate dashboard data any time:

```bash
python3 scripts/fetch_vacancies.py --report-only
```

## 9. Telegram digest (optional)

A daily push of your top unscored vacancies with 👍/👎 inline buttons.

1. **[human]** Create a bot via [@BotFather](https://t.me/BotFather), copy the
   token. Message your bot once so it can reach you, get your chat id
   (e.g. via `https://api.telegram.org/bot<token>/getUpdates`).
2. Add to `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Send manually: `python3 scripts/telegram_digest.py send --limit 5`
4. Record button presses: run `python3 scripts/telegram_digest.py poll`
   as a daemon (systemd, launchd, or a cron every few minutes). Buttons write
   `liked`/`passed` straight to the database.
5. Schedule `send` daily via cron.

The message is tiered: top matches with buttons, mid scores as one-liners,
then every vacancy the filter dropped, one line each with its reason.

## 10. Server schedule (optional)

The whole daily cycle can run unattended every night on a server you control:
a systemd timer starts `scripts/nightly_run.py`, which runs the driver in
`--unattended` mode, answers the scoring gates with bounded headless Claude
Code sessions, and ends with the morning digest. Units, layout, secrets
handling and the runbook live in `deploy/forge/README.md`.

## Daily rhythm

Once set up, the loop is:

```
/jobs-new                            # morning, ~5 min, mostly automated
/jobs-review list → like/pass        # over coffee, or via Telegram buttons
/jobs-review                         # weekly: decide what to actually apply to
/jobs-eval                           # occasionally: is the scorer agreeing with you?
```

## Check the scoring quality (`/jobs-eval`)

The scoring prompt is the pipeline's main judging surface — so measure it.
`/jobs-eval` seeds a small **golden set** from the like/pass verdicts you have
already made (no upfront labelling), re-scores those vacancies with the current
prompt, and prints one agreement number plus precision/recall at the score
threshold and a list of disagreements to fix. The set is personal and lives in a
gitignored `evals/` folder — it is never committed. Full runbook:
`.claude/commands/jobs-eval.md`.

## Troubleshooting

- **`psycopg2` connection fails** — use the *Session pooler* URL, not the
  direct connection (direct is IPv6-only and fails in many networks).
- **Fetch returns 0 jobs for a company** — its ATS may be unsupported; run
  `python3 scripts/discover_ats.py --company "Name"` to re-detect, or check
  `careers_url` in the company table.
- **Scoring feels off** — sharpen `config/user_profile.md`: add explicit
  exclude patterns and "not a target" lines. The prompt only knows what you
  wrote there.
