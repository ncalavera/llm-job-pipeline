# Architecture

## Data flow

```
ATS / job boards ─┐
                  ├─► fetch_vacancies.py ─► Supabase (vacancy / company)
Firecrawl scrape ─┘            │   └─ quality.py gate on every description write
                               ▼
                       filter_vacancies.py (mark junk, dedup, geo buckets)
                               │
                               ▼
                       score_vacancies.py  (Claude Opus subagents, pure fit)
                               │
                               ▼
                       fetch_vacancies.py --report-only  ─► public/data.js
                                                                │
                                                                ▼
                                                       Vercel + Supabase API
                                                                │
                                                                ▼
                                                           Dashboard
```

All data lives in Supabase — the single source of truth. Nothing is stored
locally: the machine holds only the code plus the Firecrawl cache
(`.firecrawl/`, gitignored).

## Modules

```
db_conn.py              # Postgres connection singleton
        │
        ▼
company_registry.py     # company registry, alias resolution
        │
        ▼
database_supabase.py    # DAL: merge / load / score / archive
        │
        ▼
fetch_vacancies.py      # fetch orchestrator (reads fetchers.py)
filter_vacancies.py     # post-fetch cleanup (blacklist, USA/geo deletion)
score_vacancies.py      # LLM vacancy scoring
fetch_companies.py      # Firecrawl company data fetching
score_companies.py      # LLM company scoring

quality.py              # quality gate for every full_description write
geo.py                  # geography buckets (uk/germany/europe/us/cis/other)
telegram_digest.py      # daily Telegram digest with 👍/👎 buttons
```

`config.py` re-exports symbols from `company_registry.py` for backward
compatibility — older code that takes everything from the config keeps
working.

`quality.py` is dependency-free (stdlib only) and is called by every path
that persists a job description (ATS merge, board merge, blind
re-enrichment): it strips leading cookie/consent banners and rejects pages
that are pure boilerplate (cookie wall, HTTP-error page, navigation chrome)
so they never overwrite a real description.

## Tables

Described in [sql/schema.sql](../sql/schema.sql). In short:

**`company`** — one row per canonical name. Alternative spellings live in
the `aliases TEXT[]` array with a GIN index. Columns:

- Identity: `id`, `canonical_name`, `aliases`.
- Pipeline gate: `status` (`active` / `candidate` / `inactive`),
  `status_reason`. Only `active` companies feed scoring and the dashboard.
- Source: `fetch_strategy`, `ats_slug`, `careers_url`, `website`,
  `ats_config`.
- Fetch metadata: `last_fetched`, `vacancy_count`, `fetch_status`.
- Enrichment: `about`, `mission_fit`, `alignment_score`, `enriched_at`.

**`vacancy`** — one row per posting. Deduplication via
`dedup_hash` = `md5(lower(canonical_name|title))`. Columns:

- Identity: `id`, `dedup_hash`, `company_id` (FK).
- Content: `title`, `snippet`, `full_description`, `compensation`,
  `deadline`, `department`, `locations` (JSONB array).
- Triage: `status` (`unseen` / `liked` / `passed` / `to_apply` /
  `to_research` / `to_network` / `skipped` / `applied` / `archived`),
  `status_updated_at`.
- LLM: `llm_score`, `llm_reasoning`, `llm_summary`, `llm_tags`,
  `llm_hard_requirements`, `llm_scored_at`.
- Digest: `digest_sent_at` — set by `telegram_digest.py` so a vacancy is
  never pushed to Telegram twice.
- Notes: `triage` (JSONB) — where decisions and comments are saved.

**`archived_hash`** — tombstones for archived/removed vacancies. Stops a
lagging job board from re-importing a dead posting for a cooldown window.
The `reason` column distinguishes a source-side close (`gone_from_source`)
from other archival reasons: a direct ATS re-listing can resurrect a
`gone_from_source` hash; lagging boards cannot.

## Fetch strategies

`fetchers.py` supports the following sources:

- **Slug-based APIs:** Greenhouse, Lever, Ashby, Workable, Recruitee,
  Personio. Set `fetch_strategy = '<ats>'` and `ats_slug = '<slug>'` on the
  `company` row.
- **Workday:** needs `ats_config` with `tenant` and `board`.
- **BambooHR:** likewise, needs the company slug.
- **80,000 Hours Algolia search:** configured in `config.py`.
- **ReliefWeb REST API:** configured in `config.py`.
- **HTML scrape via Firecrawl:** `fetch_strategy = 'firecrawl_scrape'`,
  needs `careers_url`.
- **Teamtailor RSS:** `fetch_strategy = 'teamtailor_rss'` + `ats_slug`.

Adding a new ATS = a new branch in the `route()` function inside
`fetchers.py`. All parsers return the same dict shape, which is then merged
by `merge_vacancies()` or `merge_board_vacancies()` in the DAL.

**Gone-from-source detection:** for strategies that return the company's
complete current listing (Greenhouse, Lever, Ashby, Workable, Recruitee,
Teamtailor, BambooHR, Workday, UNOPS), an `unseen` vacancy absent from a
fresh successful fetch is automatically archived with the
`gone_from_source` reason — the company's own ATS is ground truth. Decided
statuses (`liked`, `to_apply`, `applied`, …) are never touched.

## Scoring

For one vacancy, scoring works like this:

1. `score_vacancies.py --local --limit N` — pulls the first `N` unscored
   vacancies from Supabase and prints them to stdout as JSON. By default it
   also rescues a capped batch of strong vacancies from *candidate*
   (not-yet-reviewed) companies, so a forgotten company's good role still
   gets scored (`--no-candidates` disables this).
2. The Claude Code orchestrator receives the JSON and launches one subagent
   per vacancy (1 vacancy = 1 Opus). Each subagent reads the same prompt
   template as every other backend (via `scripts/prompts.py`).
3. Each subagent returns `{score, reasoning, tags, hard_requirements,
   short_summary, deadline}`.
4. `score_vacancies.py --save` takes the results on stdin and writes the
   `vacancy.llm_*` columns.

One scoring prompt = `vacancy-scoring.md` + the substituted user profile.
Different backends (local subagents, remote CLI) see identical input — no
drift.

**Pure-fit scoring (prompt v4.0):** geography, relocation, and
visa/work-authorisation considerations are excluded from the LLM score
entirely. The score reflects only role fit, mission fit, and seniority fit.
Geography is enforced earlier, by the pre-score filter
(`filter_vacancies.py` deletes USA-only / CIS-in-person / rest-of-world
postings using the `geo.py` buckets).

## Dashboard

The frontend is static files on Vercel:

- `public/index.html` — five modes (`companies`, `catalog`, `pipeline`,
  `stats`, `archive`).
- `public/modules/*.js` — UI modules (catalog, companies, pipeline, stats,
  archive, helpers, api, state).
- `public/data.js` — a snapshot of all vacancies and companies, generated
  from Supabase (including `archived_groups` for the read-only Archive
  tab).
- `api/*.js` — Vercel serverless endpoints for real-time status updates.

On load the dashboard reads `data.js` (fast render), then fetches fresh
statuses via `/api/statuses` and `/api/company-statuses` and updates the
UI. Companies pending review that hide a strong vacancy (score ≥ 55) get a
🔥 badge and float to the top of Pending Review, with a ⏰ marker when the
application deadline is within 7 days.

## Telegram digest

`scripts/telegram_digest.py` pushes a daily digest of fresh scored
vacancies to a Telegram chat with inline 👍/👎 buttons (`send` mode), and a
long-polling listener writes the taps back to `vacancy.status` as
`liked`/`passed` (`poll` mode). Strong vacancies at unreviewed candidate
companies go into a separate buttons-free section so deadlines aren't
missed while the company waits for review. Run `poll --loop` as a daemon
and `send` from cron; see `.claude/commands/jobs-digest.md`.

## Architecture decisions

- **Why is `db_conn.py` separate from `database_supabase.py`?** To break an
  import cycle: `company_registry.py` uses `db_conn`, and
  `database_supabase.py` uses both.
- **Why is `dedup_hash` md5 and not a uuid?** Stability. The same title at
  the same company from different sources collapses into one record without
  user involvement.
- **Why Opus for scoring?** A benchmark showed Sonnet has higher variance
  on the same vacancy. Opus is stable within ±2 points rather than ±10.
- **Why 1 vacancy = 1 subagent?** Batch scoring systematically inflates
  scores by 20–50 points. Context isolation is the only way to get honest
  numbers.
- **Why is the quality gate a separate module?** `quality.py` is imported
  by the DAL, the importers, and the enrichers; keeping it stdlib-only
  means none of them drag in Firecrawl or psycopg2 transitively. The gate
  exists because one consent-wall page once overwrote dozens of real
  descriptions through a length-comparison loophole.
