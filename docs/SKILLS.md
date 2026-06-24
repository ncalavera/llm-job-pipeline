# Slash commands

The repo ships a set of slash commands for Claude Code (the
`.claude/commands/` folder). Launch Claude Code from the project root and
the commands are picked up automatically. Each file is a plain instruction
list — what to do and in what order.

The list, briefly:

## The commands

Day to day you need only `/jobs-new` (first-run + daily) and `/jobs-review`
(weekly). `/jobs-new` chains fetch → filter → score → verdict capture, so the
pipeline stages run automatically.

## `/jobs-new` — first-run setup, daily fetch + score

Covers first-time setup, the daily pipeline, resuming a partial run, and
deploying the dashboard.

**First run:** reads your profile, web-searches 10–15 matching companies,
validates each careers page, shows a yes/no shortlist, adds approved ones,
then runs the first fetch → filter → score.

**Daily:** fetches fresh vacancies from all tracked companies → filters junk
→ scores new ones → shows top matches in chat → captures your like/pass
verdicts.

Options passed through to the underlying scripts:

- `--force-all` — ignore TTL, pull every company.
- `--companies "A,B,C"` — only the listed ones.
- `--tier S` — companies with tier S.
- `--dedup` — enable fuzzy title deduplication (0.85 threshold).
- `--dry-run` — show what filter would delete without touching anything.

Scoring is **pure fit** (prompt v4.0): geography and visa considerations
are excluded — they're handled by the pre-score filter. Score-threshold
auto-archive is opt-in only.

## `/jobs-review` — review liked vacancies, archive, terminal triage

Covers three intents that all happen in a triage session:

**Deep review of liked vacancies:**

1. Groups liked vacancies by company.
2. One vacancy at a time: asks 3–5 questions about interest, fit, and
   risks.
3. Records the decision: `apply` / `skip` / `research`.
4. Creates a tracker issue when the decision is `apply`.
5. Compares decisions against `llm_score` at session end and suggests
   prompt updates if they diverge.

**Archive low-scoring postings:**

1. Shows a preview: how many candidates per score bracket
   (0–10, 10–20, 20–30).
2. Flags borderline cases (near the threshold, blind-scored).
3. Waits for explicit confirmation.
4. Sets the status to `archived` — moves to the read-only Archive tab and
   tombstones the `dedup_hash` in `archived_hash`.

Previously archived vacancies can be restored with
`python3 scripts/vac.py mark <id> unseen`.

**Terminal triage (no browser):**

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
`cis`, `other`, `unknown`.

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

## `/jobs-profile` — update scoring rules and candidate profile

Updates the filtering rules and candidate profile that drive scoring:

1. Shows the current `GLOBAL_BLACKLIST`, `HARD_FILTERS`, and profile
   sections.
2. Lets you add/remove title blacklist entries, geography exclusions, and
   `EXCLUDE_PATTERNS`.
3. Saves changes to `config/user_profile.md`.
4. Optionally resets `llm_score` / `llm_scored_at` so `/jobs-new`
   re-scores with the updated rules.

## How Claude Code finds these commands

Commands live in `.claude/commands/<name>.md` at the repo root. When you
launch Claude Code from this folder, it scans `.claude/` and adds the
commands to the available slash list. The files themselves are just
human-readable step-by-step instructions.

Want different behavior? Edit the `.md` file.
