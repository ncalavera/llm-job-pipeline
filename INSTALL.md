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
```

## 3. Create the database

1. **[human]** Create a new Supabase project (any name, pick a region near you).
2. In the Supabase dashboard open **SQL Editor**, paste the contents of
   `sql/schema.sql`, run it. This creates the two tables (`company`,
   `vacancy`) and indexes.
3. Copy the connection string: **Project Settings → Database → Session pooler**.

## 4. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

- `SUPABASE_DB_URL` — the Session pooler connection string from step 3
- `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` — from **Project Settings → API**
  (only needed if you deploy the dashboard)
- `FIRECRAWL_API_KEY` — optional (see prerequisites)

The Python scripts read `.env` from the repo root. Never commit it.

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
`.claude/commands/add-source.md`):

```
/add-source Stripe
```

It auto-detects the company's ATS (Greenhouse, Lever, Ashby, …), adds it to
the database and runs a test fetch. Add 5–10 companies you actually care
about. You can also bulk-import from a CSV — see `examples/companies.example.csv`.

## 7. Run the pipeline

In your agent:

```
/fetch     # fetch vacancies from all your companies
/filter    # quality gate: junk removal, dedup, geography buckets
/score     # LLM-score each vacancy against your profile (1 subagent per vacancy)
```

Scoring runs inside your agent — one vacancy per request (see AGENTS.md), scored
0–100 against your profile, with reasoning, tags and a summary saved to the
database.

Then triage from the terminal:

```
/vac list           # top unseen vacancies by score
/vac show <id>      # full description + scoring reasoning
/vac mark <id> liked
```

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
3. Send manually: `python3 scripts/telegram_digest.py --send --limit 5`
4. Record button presses: run `python3 scripts/telegram_digest.py --poll`
   as a daemon (systemd, launchd, or a cron every few minutes). Buttons write
   `liked`/`passed` straight to the database.
5. Schedule `--send` daily via cron.

## Daily rhythm

Once set up, the loop is:

```
/fetch → /filter → /score        # morning, ~5 min, mostly automated
/vac list → like/pass            # over coffee, or via Telegram buttons
/triage                          # weekly: decide what to actually apply to
```

## Troubleshooting

- **`psycopg2` connection fails** — use the *Session pooler* URL, not the
  direct connection (direct is IPv6-only and fails in many networks).
- **Fetch returns 0 jobs for a company** — its ATS may be unsupported; run
  `python3 scripts/discover_ats.py --company "Name"` to re-detect, or check
  `careers_url` in the company table.
- **Scoring feels off** — sharpen `config/user_profile.md`: add explicit
  exclude patterns and "not a target" lines. The prompt only knows what you
  wrote there.
