# Architecture

How the pipeline is put together: the module map, the exact order the daily
cycle runs in, and the two database backends. The design rules behind these
choices live in [`STRATEGY.md`](../STRATEGY.md); the shared vocabulary in
[`CONCEPTS.md`](../CONCEPTS.md).

The whole thing is deliberately small: a Python core that does the deterministic
work (fetch, filter, dedup, publish, ordering) and a coding agent on top that
supplies only judgment (scoring, verdicts). No service to run, no queue, no
scheduler — one command a day.

## Repo layout

| Path | What lives there |
| --- | --- |
| `scripts/` | The pipeline core — every stage is a plain Python script (see below). |
| `scripts/fetchers/` | ATS + job-board fetchers, registered by strategy (`COMPANY_FETCHERS`, `BOARD_FETCHERS`). Adding an integration adds a module here, not a branch in the driver. Every engine — surface, config keys, failure modes, a debug recipe — is documented in [`fetch-engines.md`](fetch-engines.md). |
| `scripts/prompts/` | The LLM prompt templates (Markdown). Owner-agnostic: the rubric, salary anchor and reference orgs are injected from your profile, never baked in. |
| `config/defaults.toml` | Machine mechanics — thresholds, geo tables, junk words, the `[boards.*]` catalogue, the `[volume]` window. Neutral; ships for any field. |
| `config/user_profile.example.md` | The template for your candidate profile. Copy to `config/user_profile.md` (gitignored) — the single place personal taste lives. |
| `sql/` | `schema.sql` (Postgres) + `schema.sqlite.sql` (SQLite) and `migrations/` (numbered, dual-dialect). |
| `server.js` | The self-hosted Node HTTP server: serves `public/` and answers the `/api/*` routes the dashboard calls for live reads and status writes. |
| `public/` | The static dashboard (vanilla JS/CSS) — six sections: Today, Vacancies, Companies, Applications, Boards, Settings. |
| `docs/` | This file, the [board catalogue](job-boards-catalogue.md), the [fetch-engine reference](fetch-engines.md), and the onboarding questionnaire (`index.html`, served by GitHub Pages). |
| `tests/` | Offline pytest suite — guards, characterizations, parity checks. |
| `.claude/commands/` | Plain-Markdown runbooks (they double as Claude Code slash commands). See [`AGENTS.md`](../AGENTS.md). |

## Key modules

| Module | Responsibility |
| --- | --- |
| `scripts/run_daily.py` | The daily driver — a resumable stage machine. Owns stage ORDER, checkpoints, the heartbeat and the publish gate. The single source of truth for "what happens when". |
| `scripts/fetch_vacancies.py` | Pull vacancies from tracked companies + enabled boards (TTL-aware). Also `--report-only` (re-render the dashboard without touching data) and `--boards-only`. |
| `scripts/quality.py` | The single validation layer — strips cookie banners, nav junk and non-vacancy pages before anything reaches the DB. |
| `scripts/filter_vacancies.py` | Title blacklists, exact + fuzzy dedup across boards, geography buckets, auto-archive of vacancies gone from source. Writes `reports/REPORT-filter.html`. |
| `scripts/hard_filters.py` | Reads `## HARD_FILTERS` from the profile (geo regions, title keywords) to drop roles pre-scoring. |
| `scripts/score_vacancies.py` | Builds per-vacancy scoring payloads (`--local`), saves agent results (`--save`). One vacancy = one request. |
| `scripts/score_companies.py` | Same for whole-company WANT scoring. `--local` (agent, default) / `--save`; an optional `--api` path calls the Anthropic SDK directly (needs the optional `anthropic` dependency). |
| `scripts/learning.py` | The verdict-driven feedback loop — filter/scoring/board correction proposals, backtested against liked history, applied only on explicit approval. No LLM calls. |
| `scripts/sources.py` | Enabled-board management (list / enable-board / disable-board / recommend); the enabled set persists in the DB. |
| `scripts/prompts.py` | Loads `config/user_profile.md` and substitutes each `## SECTION` into the prompt templates as `{{SECTION_NAME}}`. |
| `scripts/product_language.py` | Resolves `## OUTPUT_LANGUAGE` — the one language of the whole product (agent replies, reports, digest, dashboard default). |
| `scripts/database_supabase.py` | The data-access layer (DAL) — same API over both backends. Writes stage changes but leaves the commit to the caller (see the DAL rule in [`AGENTS.md`](../AGENTS.md)). |
| `scripts/db_conn.py` / `db_backend.py` | Backend selection + connection (loads `.env`); picks SQLite or Postgres. |
| `scripts/migrate.py` | Applies pending numbered migrations after backing up (SQLite online-backup API — WAL-safe; Postgres `pg_dump` best-effort on top of transactional rollback); safe to re-run. |
| `scripts/telegram_digest.py` | The optional daily digest (send + poll) — full mode only. |

## The daily pipeline

`/jobs-new` is one call to `scripts/run_daily.py`. The driver runs a fixed,
resumable sequence of stages. Some are **AUTO** (silent Python, run
back-to-back); some are **GATE**s where it writes a task to disk, prints
plain-language instructions and exits so the agent can supply judgment, then
`--resume`s. This ORDER is defined once, in `STAGE_ORDER` in `run_daily.py` —
not in a runbook and not in anyone's head (STRATEGY guardrail 4).

| # | Stage | Kind | What it does |
| --- | --- | --- | --- |
| 1 | `validate_profile` | AUTO | Abort early on a missing/placeholder profile. |
| 2 | `preflight` | AUTO | DB-outage hard-stop; detect first-run vs resume. |
| 3 | `onboarding` | GATE | Only when the company table is empty: discover ~10–15 real employers that fit the profile, validate their ATS, insert the approved ones. |
| 4 | `learning_review` | GATE | Verdict-driven corrections offered *before* the fetch (skippable; skipped verdicts roll over). |
| 5 | `fetch` | AUTO | Pull new vacancies from tracked companies + enabled boards (heartbeat to disk). |
| 6 | `enrich` | AUTO | Backfill blind descriptions via Firecrawl (skips cleanly if unset). |
| 7 | `filter` | AUTO | Quality report, dedup, geo buckets, gone-from-source archive. Never auto-deletes silently. |
| 8 | `company_scoring` | GATE | WANT-score new candidate companies (1 company = 1 subagent). |
| 9 | `vacancy_scoring` | GATE | Two-pass per-vacancy scoring (see below). |
| 10 | `verdicts` | GATE | Show top fresh matches; capture like / pass / to_apply, each committed immediately. |
| 11 | `publish` | AUTO | Always publish; warn loudly on a dirty run (see the publish gate). |

Exit codes the runbook branches on: `0` done, `10` gate, `20` abort
(bad profile / DB outage — fix, do not retry blindly), `30` stage error
(fix the cause, then `--resume`). Run state persists in the gitignored
`vacancies/run_state.json`, so an interrupted or gated run resumes exactly where
it stopped and never redoes finished work.

### Scoring (two passes)

Cost is a feature and the model tier is the main dial (STRATEGY guardrail 3).
To spend the strong model only where it matters, `vacancy_scoring` runs in two
passes:

1. **Screen** — a cheap model (`screen_model`, default Haiku) scores every new
   vacancy.
2. **Escalate** — only finalists whose screen score clears `escalate_threshold`
   (default 50) are re-scored by the strong `scoring_model` (Sonnet on a budget
   plan, Opus on a bigger one). Everything below the floor keeps its cheap score.

Both passes keep the invariant: **one vacancy = one request**. Batching several
into one prompt systematically over-scores by +20–50 (STRATEGY guardrail 6), so
it is never done. Each score records its provenance in `vacancy.scored_by`, so
the dashboard can distinguish a cheap screen score from a confirmed one.

### The publish gate

The gate is a warn-only detector, not a blocker: `publish` always refreshes the
dashboard, and a dirty run — a crashed stage, a blocking warning, or a single
org losing a large share of its live roles to gone-from-source archival (the
signature of a truncated fetch) — is flagged loudly on the publish note and the
report card instead of being withheld. (Skipping protects nothing and costs
something: `publish` is the run's only dashboard rebuild — `score --save`
regenerates the snapshot only when passed `--dashboard` — so withholding it
would leave the board on stale data and hide the bad run.) In full mode
publish refreshes the hosted dashboard snapshot (a browser refresh, no
redeploy); in simple mode it rewrites the local `public/data.js`. Both go
through the same driver — no mode branching. (Dashboard *code* changes are a
separate concern: redeploy the Node server, see [MIGRATION.md](../MIGRATION.md).)

## Health & observability

Two read-only surfaces answer "does the pipeline work as intended?" without
reading logs:

- **Run report card** — every `run_daily.py` run ends with a per-stage verdict
  table (`OK / OK-BUT / FAILED / SKIPPED`) rendered from `run_state.json`.
- **Health tab** (dashboard) — `public/modules/health.js` renders four blocks
  from the live `/api/health-detail` endpoint (read-only, no LLM spend):
  - **Boards** — per enabled board: freshness, failure streak, vacancy count,
    and a **PRESUMED BROKEN** flag (3+ consecutive failures, or fetched yet zero
    vacancies).
  - **Companies** — active companies whose *direct* fetch never parses (an
    error/streak, or a `render_ok_zero`/`no_data`/`js_required` status). A
    company covered by the boards or tracked by hand (`company.coverage`) is
    listed separately as *not broken* — it was never meant to fetch directly.
  - **Waiting on you** — candidate companies pending review; unseen scored roles
    and the oldest one's age.
  - **Learning loop** — last review, changes applied since, verdicts waiting
    (mirrors `scripts/learning.py`'s cursor math).

  The endpoint reads columns added by later migrations (board/company
  `last_success`, `consecutive_failures`; `company.coverage`; `board.hidden`)
  **defensively** — an unknown-column error retries without them so a
  partially-migrated DB still answers.

- **Architecture diagram** — the canonical data-flow picture lives as a Mermaid
  source string in `public/modules/architecture-diagram.js`, rendered on the
  Health tab (Mermaid lazy-loaded from a CDN on first open) and kept in sync
  with this document. **Any change to the pipeline's shape updates both.**

## Two backends

The pipeline runs on one of two databases, chosen purely by whether
`SUPABASE_DB_URL` is set. The **same scripts** talk to both — the DAL
(`database_supabase.py`) is the only layer that knows the difference, and no
*product* decision is ever keyed off which backend is in use (STRATEGY
guardrail 2).

| | Full mode (canonical) | Simple mode (honest demo) |
| --- | --- | --- |
| Backend | self-hosted Postgres, `SUPABASE_DB_URL` set (historical name, plain Postgres URL) | local SQLite file (`data/jobsearch.db`), auto-created |
| What you need | a server you control, running Postgres and `server.js` | nothing |
| Dashboard | always-on self-hosted Node server (`server.js`) on a VPS, any device, Basic-Auth at the reverse proxy | `localhost` via `dashboard_local.py`, while your terminal is open |
| Telegram digest | yes | no (needs a server) |
| Multi-device / sync | yes | no |
| Runbook | [`INSTALL.md`](../INSTALL.md) | [`INSTALL-EASY.md`](../INSTALL-EASY.md) |

Postgres is the canonical daily path. SQLite is the zero-signup way to try the
product — everything the pipeline *computes* (fetching, filtering, dedup,
company review, scoring quality) is identical, and a crash on the demo path is
still a bug. The differences above are **documented product features that need a
server**, not silent gaps: simple mode never promises the always-on dashboard,
the digest, or multi-device sync. Simple mode upgrades to full at any time —
point `.env` at a Postgres database and the same scripts switch to it, no code
changes (the one extra step is installing `psycopg2`, which simple mode skips).

Because Postgres is always the live prod database (there is no separate
staging database), `db_backend.get_conn()` blocks INSERT/UPDATE/DELETE against
it from anything it doesn't recognize as pytest or one of the repo's KNOWN
pipeline entrypoints (an explicit allowlist in `db_backend.py` — location
under `scripts/` alone is not identity) — the guard that stops a stray ad-hoc
script (a debug one-off with `SUPABASE_DB_URL` in its environment) from
writing test data into prod, since unlike pytest it has no fixture to clean
up after itself. A genuine one-off write needs `JOBSEARCH_ALLOW_PROD_WRITE=1`
set explicitly; reads are never affected, and SQLite (simple mode) is never
guarded since it isn't prod.

## Data model & migrations

The schema ships as `sql/schema.sql` (Postgres) and `sql/schema.sqlite.sql`
(SQLite). Schema changes are numbered, dual-dialect migrations under
`sql/migrations/` (e.g. `0011_board_enabled.*.sql`), applied by
`scripts/migrate.py` with an automatic pre-migration backup. The core entities
(companies, vacancies, applications, boards, the learning log, company evidence)
and named statuses are defined in [`CONCEPTS.md`](../CONCEPTS.md).

## Prompts & neutrality

Scoring prompts live as templates in `scripts/prompts/`. They carry **no**
personal taste: `scripts/prompts.py` injects the rubric inputs from your profile
via `{{USER_PROFILE}}`, `{{TARGET_ROLES}}`, `{{EXCLUDE_PATTERNS}}` and the other
`## SECTION` placeholders. Guard tests (`tests/test_no_hardcoded_data.py`) keep
sector, salary and worldview language out of the shipped templates so the
defaults work for a nurse, a game designer and a policy analyst equally.

## Optional dependencies

The core needs only `requests`, `beautifulsoup4` and `python-dateutil`
(plus `psycopg2-binary` for the Postgres path). Two dependencies are optional:

- **`firecrawl-py`** — only for the `enrich` stage on JS-heavy career pages;
  unset Firecrawl and enrichment skips cleanly.
- **`anthropic`** — only for `score_companies.py --api`, the direct-SDK scoring
  path. The normal daily flow scores through your coding agent's subagents
  (`--local`), which needs no API key and no `anthropic` package.
