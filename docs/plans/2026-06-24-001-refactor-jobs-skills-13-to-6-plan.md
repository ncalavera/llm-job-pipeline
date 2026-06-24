---
title: Simplify job-pipeline skills — 13 commands → 6 (jobs-* scheme)
type: refactor
status: active
date: 2026-06-24
linear: DHA-275
origin: docs/brainstorms/2026-06-24-jobs-skills-13-to-5-brainstorm.md
---

# ♻️ Simplify job-pipeline skills: 13 → 6 commands

## Overview

Collapse the job-pipeline command surface from 13 commands into **6** under the
`jobs-*` prefix, delete two layers of duplicate copies, fold first-run setup
into the daily command, add two new `/jobs-add` modes, and fix the landing-page
install funnel so new users aren't told to run dead commands.

This is a rename + merge + small-feature refactor, **not** a behavior rewrite.
Karpathy discipline: touch only what's needed, keep the pipeline behavior
identical except where a merge is explicitly specified.

All key decisions come from the brainstorm (see
`docs/brainstorms/2026-06-24-jobs-skills-13-to-5-brainstorm.md`). This plan does
not re-debate them.

## ⚠️ Deep-Review Enhancement (2026-06-24) — read first

Six review agents (architecture, simplicity, security, spec-flow, publish
mechanics, learnings) audited this plan. They found one premise-breaking error and
several plan-changing corrections. **Where this section conflicts with text below,
this section wins.**

### A. Auto-publish premise is WRONG — `git push` does not deploy, and the repo is public
- There is **no GitHub→Vercel git integration**. The live dashboard only updates
  via the **Vercel CLI** (`vercel --prod`), which needs `VERCEL_TOKEN` to run
  unattended (`.vercel/project.json` links the project; `vercel.json` has no branch
  config).
- `public/data.js` is **gitignored and untracked** (`.gitignore:10`) — committing
  never ships the data; the CLI uploads it via `.vercelignore`.
- The GitHub repo **`ncalavera/llm-job-pipeline` is PUBLIC**, and `public/data.js`
  embeds the candidate's PII inside `llm_reasoning`/`llm_summary` (real name,
  Yandex history, comp, Tbilisi→EU visa status). A stray `git add -f data.js` would
  leak PII publicly.
- From the work **worktree**, a bare `git push` lands on the feature branch, not
  `main`, and still deploys nothing.
- **Consequence:** the brainstorm's "auto-push, no asking → Vercel deploys" was
  decided on a false model. **This is now a pending user decision** (see Open
  Decisions). Real "publish" = regen `data.js` + `vercel --prod`; the git push is a
  *separate* concern about versioning code/doc edits to `main`.

### B. Auto-publish security requirements (non-negotiable if any auto-git happens)
- **Explicit-path staging only.** Forbid `git add -A` / `git add .` / `git add -f`
  in `/jobs-new` and `/jobs-review`. This replaces the old `jobs-finish` "never
  `git add .`" human rule with an automated one.
- **Pre-push guard** that aborts (non-zero, loud) if `git diff --cached
  --name-only` contains any of: `.env*`, `config/user_profile.md`,
  `public/data.js`, `vacancies/`, `data/`, `*.db`, `.firecrawl/`,
  `architecture-notes/`, `.claude-session-acceptance.md`. Independent of
  `.gitignore` (which `-f` bypasses). Add a lint test mirroring `test_no_stale_*`.
- **Fail-open auth hole:** `middleware.js` leaves the dashboard fully public if
  `AUTH_USER`/`AUTH_PASS` are unset. Full-mode auto-deploy must assert auth is set
  (or warn + confirm once) before shipping PII.
- Remove the dangling `!public/data.js.example` `.gitignore` exception (no such
  file; foot-gun).

### C. Empty-DB onboarding switch is unsound → key on COMPANY count, with a gate
- Detect first-run via `len(COMPANIES) == 0` (the signal `fetch_vacancies.py:311`
  already prints), **not** vacancy count. Companies-exist-but-zero-vacancies is a
  normal daily state (TTL cooldown, quiet day) — must NOT trigger the
  company-discovery wizard (destructive surprise).
- Add a one-line confirm before onboarding fires; pin the exact detection snippet
  in the runbook so it's deterministic, not improvised per run.
- **Run `scripts/migrate.py` in the first-run branch before any INSERT** (learning:
  `docs/solutions/database-issues/data-safe-schema-migrations-dual-backend.md`) so
  SQLite and Supabase converge to current schema.

### D. Partial-failure / auto-publish-on-every-run is the biggest architecture gap
- `--report-only` regenerates `data.js` from whatever the DB currently holds. If
  fetch succeeds but **score fails**, the tail publishes unscored rows; a truncated
  ATS fetch (HTTP 200, partial list) auto-archives real vacancies as "gone" and
  ships the deletion. The old manual `/jobs-finish` was the human checkpoint the
  merge removes.
- **Gate the publish tail on zero stage-errors** (fetch errors == 0, score failures
  == 0, gone-archive < ~30% of an org). On error: regen locally, **do not
  deploy/push**, tell the user to review and run an explicit `publish`.
- Both `/jobs-new` and `/jobs-review` publishing in one session = multiple deploys;
  debounce (only push if `data.js`/tracked files actually changed) or document it.
- Collision #3 reword: there are **two** archive paths — score-threshold (deferred
  to `/jobs-review archive`) and gone-from-source (stays automatic in fetch). The
  deferral applies only to `score_vacancies.py --archive`.

### E. Simplicity reversals (pending user confirmation)
- **Sub-stage flags `/jobs-new fetch|filter|score`** re-expose the exact surface the
  refactor deletes. The DHA-275 ticket explicitly asked for them (`/new-jobs
  fetch`), but the simplicity review flags them as self-contradictory. **User
  decision.** (`--limit`/`--companies`/`--force-all`/`--boards` are params, keep.)
- **Board persistence:** prefer **Option 2 (env-only, zero new mechanism)** over
  Option 1 — building a config-persistence layer in a simplification refactor is
  premature for a one-user tool. **User decision.**

### F. Spec gaps to add (folded into phases/acceptance below)
- **Deprecation path** for the 10 deleted names: README old→new mapping table +
  optionally stub `.claude/commands/<old>.md` redirect files (else silent
  "command not found"). Note `/jobs-digest poll` cron calls the Python script
  directly — rename doesn't break cron, but `INSTALL.md` prose must stay accurate.
- **`/jobs-add` Step-0 router** keyword collision: `board`/`vacancy`/`job` can be
  real company names ("Board Intelligence", "JobTeaser") — disambiguate when the
  arg could be a company.
- **`/jobs-add` Mode C unknown company:** `ensure_company()` makes a fetch-less
  stub → future `/jobs-new` never pulls it. Offer to configure ATS or warn.
- **`/jobs-profile` missing `config/user_profile.md`:** fall back to
  `user_profile.example.md` with a clear "showing example" header (mirror
  `jobs-rules.md` behavior); never silently operate on the example.
- **`/jobs-review` bare (no arg)** → status menu (counts), not auto-enter the long
  `apply` interview.
- **AGENTS.md** runbook table must be *restructured* to exactly 6 rows, not just
  find-replaced.

## New Requirements (user, 2026-06-24)

### G. Private-data guardrails for the PUBLIC repo (partly DONE)
Goal: it is *impossible* to commit private data to the public GitHub repo.
- **DONE now (live):** local `.git/hooks/pre-commit` blocks staging any of `.env*`
  (except `.env.example`), `config/user_profile.md`, `public/data.js`, `vacancies/`,
  `.firecrawl/`, `architecture-notes/`, `*.db|*.sqlite`,
  `.claude-session-acceptance.md`, plus a secret-signature content scan (`sk-`,
  `ghp_`, `AKIA…`, PEM). Tested: blocks `public/data.js`. Applies to worktrees too
  (shared git dir). `.gitignore` foot-gun (`!public/data.js.example`) removed.
- **DONE now (audit):** no private data is currently tracked (only `.env.example`
  and `sql/schema.sqlite.sql`, both intended public).
- **To commit (durable, survives clone):**
  - tracked `hooks/pre-commit` (copy of the live one) + `scripts/install-hooks.sh`
    that runs `git config core.hooksPath hooks` (so a fresh clone is protected).
  - `tests/test_no_private_data_tracked.py` — asserts `git ls-files` contains no
    sensitive path (regression guard, runs in the 604-suite, also catches anyone
    who bypasses the hook with `--no-verify`).
  - README one-liner: run `scripts/install-hooks.sh` after clone.

### H. `/jobs-new` resumability — continue a half-finished run
Goal: an interrupted `/jobs-new` (e.g. died at vacancy 12 of 20) resumes the rest
on the next run instead of redoing work.
- **Scoring is the lossy stage.** Today results are collected into one array and
  saved once via `score_vacancies.py --save` — a crash before that save loses the
  whole batch. **Change: save incrementally** (per vacancy, or small chunks) so
  completed scores persist. Verify current `--save` granularity during work.
- **Resume detection at startup:** if unscored `unseen` vacancies already exist
  from a prior run, `/jobs-new` offers to **resume scoring those first** before
  fetching new ones. Naturally re-entrant because scoring already targets
  `unseen`+unscored and verdict capture commits per-verdict.
- **Stage re-entrancy:** fetch (TTL-gated, merges), filter (operates on unscored),
  verdicts (per-verdict commit) are already idempotent; document the checkpoint
  order so resume picks the furthest-incomplete stage. Tie to §D (don't auto-deploy
  a half-finished run).

## Target Command Set (6)

| New command | Absorbs | Auto-publish? |
| --- | --- | --- |
| `/jobs-new` | `jobs`, `jobs-fetch`, `jobs-filter`, `jobs-score`, `jobs-finish`, `jobs-start` (first-run) | **Yes** |
| `/jobs-review` | `jobs-apply`, `jobs-archive`, `jobs-vac` | **Yes** |
| `/jobs-add` | `jobs-add` + NEW board mode + NEW single-vacancy mode | n/a |
| `/jobs-profile` | `jobs-rules` + profile update (creation stays on landing) | n/a |
| `/jobs-digest` | `jobs-digest` (Telegram only) | n/a |
| `/jobs-update` | `jobs-update` (maintenance util, unchanged) | n/a |

Old names that fully disappear: `jobs`, `jobs-fetch`, `jobs-filter`,
`jobs-score`, `jobs-finish`, `jobs-start`, `jobs-apply`, `jobs-archive`,
`jobs-vac`, `jobs-rules`. (`jobs-add`, `jobs-digest`, `jobs-update` survive.)

## Research Findings (consolidated)

### Blast radius — old command names
- **552 occurrences across ~52 files** (sweep agent). Heaviest: `README.md` (19),
  the `.agents/skills/source-command-*` copies, `scripts/fetchers.py` (12),
  `docs/SKILLS.md` (12), `INSTALL.md` (11), and `.claude/commands/*` themselves.
- `jobs` (bare) is the trap: only `/jobs`, `jobs.md`, or command-context refs
  count — not every English "jobs". Lint and edits must use the slash-bounded
  regex (below), not a naïve string match.

### Duplicate layers to delete (both safe — nothing references them)
- **In-repo (12 dirs):** `.agents/skills/source-command-jobs{,-add,-apply,-archive,-digest,-fetch,-filter,-finish,-rules,-score,-start,-vac}/`.
  Dependency check: no code/script/test loads `.agents/skills/`; only mentions are
  in this plan and the brainstorm.
- **⚠️ GLOBAL, OUTSIDE REPO (8 dirs):** `~/.claude/skills/{fetch,score,filter,triage,archive,enrich,add-source,vac}`.
  These are in the user's personal config, not the repo. **Requires explicit
  confirmation before deletion** — deleting global config is not a repo change and
  could affect other projects. Treat as a separate, gated step.

### Merge collision risks (7) — resolve once, in the merged command
1. **Publish step has 5 callers** (`jobs` S5, `jobs-fetch`, `jobs-finish`,
   `jobs-archive`, `jobs-start`). Single owner: `--report-only` regen runs **once
   at the end** of `/jobs-new` and after each `/jobs-review` change; the git
   commit+push (old `jobs-finish`) becomes the auto-publish tail.
2. **Double enrich**: `enrich_blind_vacancies.py` called by both `jobs-fetch` and
   `jobs-filter`. In `/jobs-new`, enrich **once** after fetch, before filter
   analysis; filter only re-checks still-blind.
3. **Score→archive ordering**: `score_vacancies.py --archive` vs interactive
   `jobs-archive`. In `/jobs-new` pipeline, save **without** `--archive`; archival
   is the explicit `/jobs-review archive` step. Never both in one run.
4. **Verdict capture seam**: `/jobs-new` does quick like/pass on today's `unseen`;
   `/jobs-review` does deep structured review of already-`liked`. Complementary,
   keep the seam explicit in both runbooks.
5. **Blacklist-edit confirm gate**: both `jobs-filter --suggest-blacklist` and
   `jobs-apply` calibration can edit `scripts/config.py`. Both paths must gate on
   explicit confirmation; never fire both silently.
6. **Profile-read-first**: `/jobs-new` validates `config/user_profile.md` as
   step 1 (before fetch), so a bad profile aborts early, not after expensive work.
7. **Devex branch**: `scrape_devex.py`+`import_devex.py` live only in `jobs-fetch`
   and are cookie-dependent; carry the branch into `/jobs-new fetch`.

### Auto-publish — SUPERSEDED by Enhancement §A/§B/§D (pending user decision)
The brainstorm said "auto commit+push → Vercel deploys, no confirmation." Deep
review proved that model false (see §A): `git push` deploys nothing, `data.js` is
gitignored, and the repo is public with PII in `data.js`. The corrected design,
pending the user's call: real publish = regen `data.js` + `vercel --prod` (needs
`VERCEL_TOKEN`), guarded by §B staging/secret checks and §D success-gating; git
commit/push of *code* is a separate, `main`-targeted, optional step. Simple mode
(no Supabase / no `.vercel` link / no remote): regen locally only, never deploy or
push.

### Landing page (`docs/index.html`) — 7 edit sites
Hardcoded old names at: 987 (EN i18n `hiwt`), 1037 (RU i18n `hiwt`), 1659 (RU
easy install), 1670 (RU full install), 1682 (EN easy install), 1693 (EN full
install), **1737 (terminal animation — NOT language-switched, easiest to miss)**.
All `/jobs`, `/jobs-start`, `/jobs-fetch`, `/jobs-filter`, `/jobs-score` collapse
to `/jobs-new`; `/jobs-add` stays. RU and EN are independent copies — edit both.
`buildProfile()` (1552–1648) already produces a complete `config/user_profile.md`
from an 11-question wizard → first-time profile creation stays here, untouched.

### Tests — zero mechanical breakage
- No test loads a command file or asserts one exists; all command-name refs are
  prose in docstrings. **604 tests stay green** through the rename.
- Cosmetic prose updates (optional, good hygiene): `test_add_source.py:4,87,122`,
  `test_vac_cli.py:1,3`, `test_score_vacancies.py:105`.
- **New lint** `tests/test_no_stale_jobs_command.py` mirrors the existing
  `test_no_stale_score_command.py` regex `(?<![\w/-])/NAME(?![\w-])`, forbidding
  the 10 dead names across `docs/`, `.claude/commands/`, `README.md`, `AGENTS.md`,
  `INSTALL*.md`. Allowlist must include the brainstorm + this plan (they
  legitimately name old commands).
- **Watch**: `test_no_hardcoded_data.py` scans the whole `.claude/` tree for
  owner-infra tokens — new command file content must not contain any.

## Implementation Phases

### Phase 0 — Setup
- Work in the worktree on branch `ndsolovev/dha-275-...` (already named).
- Baseline: `pytest -q` green (record count), every current command loads.

### Phase 1 — Delete duplicate layers
- [ ] Delete the 12 `.agents/skills/source-command-jobs-*/` dirs.
- [ ] **(Gated, needs explicit user OK)** delete the 8 `~/.claude/skills/`
  legacy dirs. Do NOT bundle into the repo commit; it's global config.
- [ ] `pytest -q` still green (the two scanner tests are deletion-blind).

### Phase 2 — Build the 6 merged commands (the core)
Each is one subagent task. Use **sonnet** for the near-mechanical ones and
**opus** for the two real merges with new control flow.

- [ ] **`/jobs-new`** (opus) — `.claude/commands/jobs-new.md`. Pipeline: validate
  profile → (empty-DB? → first-run onboarding from `jobs-start`) → fetch → enrich
  once → filter → score (no `--archive`) → show top `unseen` + capture verdicts →
  regen `public/data.js` → **auto commit+push in full mode**. Flags from the merge
  spec: `fetch` / `filter` / `score` / `review` sub-stage entry, `--boards`,
  `--limit N`, `--force-all`, `--companies "X,Y"`. Carry the Devex branch.
- [ ] **`/jobs-review`** (opus) — `.claude/commands/jobs-review.md`. Modes:
  default/`apply` (deep `jobs-apply` interview), `archive` (`jobs-archive`),
  `vac [list|show|mark|open|companies]` (`jobs-vac` CLI). Auto-publish after
  `apply`/`archive` changes.
- [ ] **`/jobs-add`** (opus) — extend `.claude/commands/jobs-add.md` with a Step-0
  router: first arg `board` / `vacancy` / `job` selects mode, else = company
  (today's flow verbatim). See "New /jobs-add modes" below.
- [ ] **`/jobs-profile`** (sonnet) — `.claude/commands/jobs-profile.md`. Fold
  `jobs-rules` (HARD_FILTERS edit of `config/user_profile.md`) + broader profile
  edits. Modes: view / `rules` / `edit`. Do NOT duplicate the landing wizard.
- [ ] **`/jobs-digest`** (sonnet) — rename `jobs-digest.md` → keep Telegram
  `send`/`poll` 1:1. Email/WhatsApp are **out of scope** → DHA-282.
- [ ] **`/jobs-update`** (sonnet) — rename `jobs-update.md` 1:1, no logic change.
- [ ] Delete the 10 old `.claude/commands/*.md` files once content is absorbed.
- [ ] After EACH command: `pytest -q` green + the new command file loads.

### Phase 3 — Landing page (`docs/index.html`)
- [ ] Apply the 7 edits (table above). Both RU and EN copies. **Don't forget line
  1737** (terminal animation, not i18n-switched).
- [ ] Easy-mode install text → "run `/jobs-new` (first run auto-detects empty DB
  and onboards)". Full-mode → "`/jobs-add` … then `/jobs-new`".
- [ ] `buildProfile()` untouched.

### Phase 4 — Docs sweep
- [ ] Update old→new names in: `README.md`, `INSTALL.md`, `INSTALL-EASY.md`,
  `AGENTS.md` (the runbook table), `docs/ARCHITECTURE.md`, `docs/DATABASE.md`,
  `docs/SKILLS.md`, `docs/PROMPTS.md`. Use the sweep inventory as the checklist.
- [ ] `scripts/fetchers.py` and any script print/usage strings mentioning old
  command names (12 hits in fetchers).

### Phase 5 — Tests / lint
- [ ] Add `tests/test_no_stale_jobs_command.py` (design above) with allowlist =
  {brainstorm, this plan}.
- [ ] Optional prose-comment updates in the 3 test files listed.
- [ ] Verify new command files contain no owner-infra tokens
  (`test_no_hardcoded_data.py` passes).
- [ ] Full `pytest -q` green; confirm count ≥ baseline.

### Phase 6 — Verification (real runs, not just tests)
- [ ] `/jobs-new` end-to-end on the local DB: fetch→filter→score→verdicts→**auto
  regen** (simple mode: no push).
- [ ] `/jobs-new` first-run path on an empty DB → onboarding fires.
- [ ] `/jobs-review archive` and `apply` → changes + auto-publish.
- [ ] `/jobs-add board <id>` → board enabled + first fetch + scored.
- [ ] `/jobs-add vacancy` → hand-added job lands `unseen`, scores, no dup.
- [ ] Full-mode auto-push sanity (if Supabase/Vercel configured): one run pushes.

## New `/jobs-add` modes (design detail)

Reuse existing machinery; cite `scripts/database_supabase.py`,
`scripts/config.py`, `config/defaults.toml`.

**Mode B — board.** Boards are defined in `config/defaults.toml [boards.*]`,
loaded by `config._ALL_JOB_BOARDS`, enabled per-run by env `JOB_BOARDS=...` →
`_select_enabled_boards()`. First fetch:
`JOB_BOARDS=<ids> python3 -u scripts/fetch_vacancies.py --boards-only --free-only`
(→ `save_board_vacancies` → `unseen` → scored). Adding a brand-new board (not one
of the 6 built-ins) is **out of scope** — needs a `defaults.toml` block + a new
`fetch_*_board` strategy.

**Mode C — single vacancy.** Reuse `save_vacancies(org, tier, [job])`
(`database_supabase.py:568`) — it resolves canonical org, runs the quality gate,
dedups on `dedup_hash = md5(lower(org|title))`, merges locations, inserts as
`status='unseen'` → auto-scored. Minimal asks: **org, title, url** (required);
**description** strongly recommended; location/snippet/compensation/deadline/dept
optional. Check `new_count` — the gate can silently drop a thin row
(`has_enough_content`); if 0, tell the user why and offer Firecrawl enrichment.

## Decisions Made (user, 2026-06-24)

1. **Publishing model — RESOLVED → deploy-only, git stays manual.** `/jobs-new` and
   `/jobs-review` regen `data.js` + run `vercel --prod` to the private dashboard;
   **no auto-commit/push**. Needs `VERCEL_TOKEN` in 1Password for unattended deploy.
   Simple mode (no Supabase / no `.vercel` link): regen locally only, no deploy.
   Deploy still gated by §D (only on a clean, error-free run) and §B auth assertion.
2. **Sub-stage flags — RESOLVED → CUT.** `/jobs-new` is one linear pipeline. No
   `fetch|filter|score` sub-modes. (`--limit`/`--companies`/`--force-all`/`--boards`
   params stay.) Partial runs = call the underlying script directly. Lint/docs/the
   landing get simpler. Note: an explicit `publish`-only path may still be needed
   for §D recovery — keep that as the single exception, decided during work.
3. **Board persistence — recommend Option 2 (env-only).** No new config mechanism;
   `/jobs-add board` runs the first fetch and tells the user the `JOB_BOARDS=…` to
   keep. (Confirm during work; matches the simplicity lean.)

## Acceptance Criteria

- [ ] 6 commands exist and work; the 10 dead command files are gone; no duplicate
  `.agents` copies remain.
- [ ] No old command name appears in code/docs/landing (new lint green, grep
  clean apart from allowlisted plan/brainstorm).
- [ ] `pytest` green (≥ baseline, ~604); every command file loads.
- [ ] `/jobs-new` and `/jobs-review` auto-publish (no separate ship); full-mode
  pushes automatically, simple-mode regenerates locally only.
- [ ] Landing install funnel points new users at `/jobs-new` (+ `/jobs-add`).
- [ ] `/jobs-add` dispatches company / board / single-vacancy; a hand-added
  vacancy enters scoring with dedup.
- [ ] Global `~/.claude/skills/` legacy dirs deleted **only after explicit OK**.
- [ ] **Security (§B):** auto-publish stages an explicit allowlist; `git add -A|.|-f`
  forbidden (lint test); pre-push guard aborts if any sensitive path is staged;
  `git ls-files` stays clean of `data.js` after a full-mode run; full-mode refuses
  (or warns+confirms) when Vercel `AUTH_USER`/`AUTH_PASS` are unset.
- [ ] **First-run (§C):** onboarding fires only on `len(COMPANIES)==0` + confirm;
  `migrate.py` runs before first INSERT; a DB with companies but zero vacancies does
  NOT re-fire the wizard (Phase 6 check).
- [ ] **Partial-failure (§D):** a run with a failed stage regenerates `data.js`
  locally but does NOT deploy/push; explicit `publish` ships after fix.
- [ ] **Deprecation (§F):** README carries an old→new mapping; deleted names don't
  silently no-op.
- [ ] **Edge cases (§F):** `/jobs-add` router disambiguates company-name collisions;
  Mode C unknown-company warns about no fetch config and handles silent
  `has_enough_content` drop; `/jobs-profile` falls back to the example profile with
  a clear header.
- [ ] **Guardrails (§G):** tracked `hooks/pre-commit` + `scripts/install-hooks.sh`
  (wired via `core.hooksPath`); `tests/test_no_private_data_tracked.py` green and
  asserts no sensitive path is git-tracked; live local hook already blocks
  `public/data.js`/`.env`/profile (verified).
- [ ] **Resumability (§H):** scoring saves incrementally; an interrupted `/jobs-new`
  resumes the remaining unscored vacancies on next run instead of redoing them; a
  half-finished run does not auto-deploy.

## Out of Scope (deferred)
- Email + WhatsApp digest channels → **DHA-282** (Low).
- Adding brand-new (non-built-in) job boards via `/jobs-add`.

## Sources & References

### Origin
- **Brainstorm:** `docs/brainstorms/2026-06-24-jobs-skills-13-to-5-brainstorm.md`
  — carried forward: single source of truth (delete `.agents` copies), `jobs-*`
  prefix, 6 commands (5 daily + `jobs-update`), first-run folds into `/jobs-new`,
  auto-push without asking, profile creation stays on landing, landing must be
  updated.

### Internal references
- Commands: `.claude/commands/jobs-*.md` (13 files).
- Insert/dedup: `scripts/database_supabase.py:568 save_vacancies`,
  `:707 save_board_vacancies`, `:246 ensure_company`, `:166 make_vacancy_id`,
  `:1048 update_source_tracking`; `scripts/company_registry.py:188
  resolve_canonical_name`.
- Boards: `scripts/config.py:203 _ALL_JOB_BOARDS`, `:206 _select_enabled_boards`,
  `config/defaults.toml [boards.*]`, dispatch `scripts/fetch_vacancies.py:419-480`.
- Schema/scoring: `sql/schema.sql:75` (`status DEFAULT 'unseen'`),
  `scripts/score_vacancies.py`, `scripts/filter_vacancies.py:257 _find_fuzzy_dupes`.
- Lint template: `tests/test_no_stale_score_command.py`.
- Landing: `docs/index.html` (edit sites 987,1037,1659,1670,1682,1693,1737;
  `buildProfile()` 1552–1648).

### Related work
- Linear: DHA-275 (this), DHA-282 (deferred digest channels).
