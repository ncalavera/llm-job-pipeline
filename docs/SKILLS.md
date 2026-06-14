# Slash commands

The repo ships a set of slash commands for Claude Code (the
`.claude/commands/` folder). Launch Claude Code from the project root and
the commands are picked up automatically. Each file is a plain instruction
list — what to do and in what order.

The list, briefly:

## The three you actually use

Day to day you need only `/jobs-start` (once), `/jobs` (daily) and `/jobs-apply`
(weekly). `/jobs` chains fetch → filter → score → verdict capture, so the
stage commands below are for fine control and debugging, not the daily
routine.

## `/jobs-fetch` — fetch vacancies

Interactive fetching:

1. Shows the status of every source (when it was last refreshed).
2. Asks what to fetch: everything / active companies only / a selection.
3. Runs `python3 scripts/fetch_vacancies.py` with the right flags.
4. Reports how many new vacancies arrived.

During the merge, every description passes the `quality.py` gate (cookie
walls, error pages, and nav chrome are never saved). For direct-ATS
strategies that return the complete listing, unseen vacancies missing from
a fresh fetch are auto-archived as `gone_from_source`; JS-rendered shell
pages are marked `js_required` instead of silently returning zero.

Options:

- `--force-all` — ignore TTL, pull every company.
- `--companies "A,B,C"` — only the listed ones.
- `--tier S` — companies with tier S.
- `--auto-score` — run filter + score right after the fetch.

## `/jobs-filter` — clean out junk

The quality gate between fetching and scoring:

1. Loads unscored vacancies.
2. Classifies by title blacklist, locations, descriptions.
3. Geography exclusion via profile hard filters: vacancies whose every
   location falls in a country listed under `## HARD_FILTERS →
   exclude_countries` in your profile are deleted before scoring. No
   country is hardcoded — the list is empty by default (nothing dropped).
4. Marks duplicates (exact and fuzzy by title).
5. Deletes junk, leaves a ready set for scoring.
6. If it spots recurring patterns, it suggests adding them to
   `GLOBAL_BLACKLIST`.

Options:

- `--dedup` — enable fuzzy title comparison (0.85 threshold).
- `--dry-run` — show what would be deleted without touching anything.

## `/jobs-score` — score with Claude

Launches an Opus subagent per vacancy (1 vacancy = 1 request). Default
parallelism is 5 concurrent subagents.

1. Pulls unscored vacancies (20 at a time by default). By default it also
   rescues a capped batch of strong vacancies from unreviewed *candidate*
   companies (`--no-candidates` disables this).
2. Runs a subagent per vacancy, waits for the answers.
3. Saves `llm_score`, `reasoning`, `tags`, `hard_requirements`,
   `summary`, `deadline`.
4. Prints a session report with score distribution and junk flags.

Scoring is **pure fit** (prompt v4.0): geography and visa considerations
are excluded — they're handled by `/jobs-filter`. Score-threshold auto-archive
is currently paused under pure-fit scoring (opt-in only via
`archive_vacancies(force=True)`).

## `/jobs-archive` — clean out old postings

Interactive archival of low-scoring unreviewed vacancies:

1. Shows a preview: how many candidates per score bracket
   (0–10, 10–20, 20–30).
2. Flags borderline cases (near the threshold, blind-scored).
3. Waits for explicit confirmation.
4. Sets the status to `archived` — the vacancy moves to the read-only
   Archive tab on the dashboard, and its `dedup_hash` is tombstoned in
   `archived_hash` so boards can't re-import it.

Previously archived vacancies can be restored with
`python3 scripts/vac.py mark <id> unseen`.

## `/jobs-apply` — deep review of liked vacancies

A structured interview over every liked vacancy:

1. Groups them by company.
2. One vacancy at a time: asks 3–5 questions about your interest, fit, and
   risks — including whether the role offers enough complexity for real
   growth.
3. Records the decision: `apply` / `skip` / `research`.
4. Creates a tracker issue with acceptance criteria when the decision is
   `apply`.
5. At the end of the session compares decisions against `llm_score` — if
   they diverge, suggests a prompt update.

## `/jobs-vac` — terminal triage

A KISS CLI for day-to-day work from the terminal, no browser:

```bash
python3 scripts/vac.py list                  # top 20 by score
python3 scripts/vac.py list --status liked   # liked only
python3 scripts/vac.py list --geo uk,europe  # filter by geo buckets
python3 scripts/vac.py show <id>             # full description
python3 scripts/vac.py mark <id> liked       # change status (incl. archived)
python3 scripts/vac.py open <id>             # open the URL in the browser
python3 scripts/vac.py companies             # company summary
```

`--geo` accepts the `geo.py` buckets: `uk`, `germany`, `europe`, `us`,
`cis`, `other`, `unknown`. Use it when you don't feel like opening the
dashboard.

## `/jobs-digest` — Telegram digest

Drives `scripts/telegram_digest.py`:

- `send` — posts the top fresh scored vacancies to a Telegram chat as
  separate messages with inline 👍/👎 buttons and stamps `digest_sent_at`.
  Strong vacancies at unreviewed candidate companies go into a separate
  buttons-free section. Run from cron/scheduler once a day.
- `poll` — long-polls for button taps and writes `liked`/`passed` back to
  `vacancy.status`. Run `poll --loop` as a long-lived daemon. Only one
  process per bot token may call getUpdates.

Configuration is env-driven: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
and the database URL (see `.env.example`). `SUPABASE_DB_URL` selects the
Postgres backend; leave it unset to target the local SQLite file instead.

## `/jobs-add` — a new company

Adds a company to monitoring:

1. Takes a careers-page URL or a company name.
2. Auto-detects the ATS (Greenhouse, Lever, Ashby, Workable, Workday) via
   `discover_ats.py`.
3. Runs a test fetch — a vacancy or two to validate the parser.
4. If the result looks right, adds it to `company`; otherwise shows what
   went wrong.

## `/jobs-finish` — close the session

Finalizes the work:

1. Regenerates `public/data.js` (`fetch_vacancies.py --report-only`) —
   including the Archive tab data.
2. Commits the changes with a meaningful message.
3. Pushes to the repo — Vercel redeploys the dashboard automatically.

Use it after any batch of changes: a new company, a reworked prompt, a
triaged batch of vacancies.

## How Claude Code finds these commands

Commands live in `.claude/commands/<name>.md` at the repo root. When you
launch Claude Code from this folder, it scans `.claude/` and adds the
commands to the available slash list. The files themselves are just
human-readable step-by-step instructions.

Want different behavior? Edit the `.md` file.
