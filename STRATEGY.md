# Strategy & Development Guardrails

This file is the decision frame for every change in this repo. A feature idea that fails these tests gets rewritten or rejected — regardless of how useful it is to any single user, including the maintainer.

## Three goals, one codebase

1. **A working daily tool.** The maintainer (and any user) runs one command a day and gets scored, deduplicated vacancies worth reading. Reliability of the daily loop beats new features.
2. **A reference-quality codebase.** The repo doubles as a public portfolio piece. Code, tests, and docs must survive a senior engineer reading them cold.
3. **Usable by a stranger.** The target user is an ordinary Claude Code user — a smart non-engineer who can follow a README but will not debug Python. The bar is `git clone` → scored dashboard with nothing to debug and no insider knowledge.

When goals conflict, fix the conflict rather than picking a favorite: a personal convenience that breaks goal 3 becomes a config default; a "just works" shortcut that breaks goal 1 gets a cap or a flag.

## Guardrails

Every PR / feature / prompt change must pass all of these:

1. **Neutral by default.** Personal taste — target roles, sectors, boards, queries, salary anchors, worldview — lives ONLY in `config/user_profile.md` (gitignored). Shipped defaults, prompts, runbooks, and examples must work for a nurse, a game designer, and a policy analyst equally. Enforced by `tests/test_no_hardcoded_data.py`; extend the guard when new default surfaces appear.
2. **Cloud is canonical; SQLite is the honest demo.** Postgres/Supabase (full mode) is the canonical daily path. SQLite (simple mode) is the zero-signup way to try the product, with an explicit, documented list of limitations — never a silent promise of parity. A crash on the demo path is still a bug; a documented difference is not. Product decisions are never keyed off `IS_SQLITE`.
3. **Cost is a feature, and the model tier is the main dial.** Budget plans default to a cheaper scoring model (Sonnet); higher plans to Opus — chosen at onboarding, changed in one setting. Per-run caps are the spike-day safety net, not the primary lever. Expensive paths (richer evidence, stronger models) are explicit opt-ins. README cost claims must name the real driver: plan tier × model × items scored.
4. **Operable without insider knowledge.** Stage order lives in code, not in the maintainer's head or a 700-line runbook. Each command explains what it will do and what it just did. A claim in README/INSTALL that doesn't match code behavior is a bug of the same severity as a crash.
5. **The run answers to the user, not to a hosted service; scriptable core, agent on top.** The daily cycle runs with no questions mid-run, progress on disk, and one summary at the end — whether the user starts it by hand or a scheduler on infrastructure the user controls (their own cron/launchd/systemd, their own server) starts it for them (revised 2026-08-27; plan: `docs/plans/2026-08-27-1231-feat-forge-nightly-run-digest-plan.md`). No *hosted* cloud routines — the pipeline never runs as a cron job on infrastructure outside the user's control. The local digest rule stays: `scripts/telegram_digest.py send`/`poll` run off the user's own cron/launchd/systemd (documented in `INSTALL.md` and `.claude/commands/jobs-digest.md`). Deterministic orchestration (ordering, batching, retries, publish gates) belongs in Python; the agent contributes judgment: scoring, verdicts, interviews. This keeps the door open for a possible hosted product — a separate future project, not this repo.
6. **Never batch scoring.** One vacancy = one LLM request. Batching is untested here; keep one role per request until a golden-set A/B comparison supports a change. Cost work must find other levers (caps, cheaper models for triage, prompt slimming).
7. **Simplicity budget.** Each new source, board, channel, or flag must serve at least two of the three goals, or it doesn't ship. Prefer deleting an option to documenting it.
8. **Closed feedback loops, never silent self-edits.** Every judging surface — filters, scoring, boards — has a path from user verdicts back to proposed corrections, offered at the start of the next run (skippable; skipped verdicts roll over). Filter-word proposals must pass a backtest against liked history; every change applies only on explicit user approval and is logged. User factors are declared with a strength — hard filter / score penalty / display-only note — and the loop may propose moving a factor between strengths, never move it itself.
9. **Derive, never bake.** The dashboard's roles payload ships raw entities, not pre-baked UI aggregates. Every number in the roles UI — basket counts and badges, the Geo table, Today's lists, section counts — is computed in the browser from `(raw list + live statuses + today's date)` through the one shared visibility filter in `public/modules/derive.js`, so a badge and its list are the same computation and can never disagree. One documented exception remains: run-level facts about rows that are NOT shipped (e.g. `stats.unscored_count` — fetched-but-unscored vacancies) — keep the smallest raw scalar and document why it can't be derived. The other exception this guardrail used to carve out — the per-company rollups (`vacancy_count`, `avg_llm_score`) consumed by `companies.js` — was closed by PR #98 (DHA-407/408): `deriveCompanyRollups` in `derive.js` now computes both fields in the browser alongside company geography. Enforced by `tests/test_e2e_pipeline.py` (the `stats` contract), `public/modules/derive.test.js`, and `tests/test_company_rollups_derived.py`.

## What this repo is not

- Not a hosted service, and not the future paid "just works" product — that would be a separate codebase with its own economics.
- Not an auto-applier. It finds, ranks, and — on explicit request — helps prepare an application (drafts, research, case bank); the human reviews and submits. Application artifacts are private data: they live in the gitignored profile space and the database, never in public code.
- Not a general ATS scraper library. Fetchers exist to serve the daily loop.

## Quick test for any new idea

Ask in order: Which of the three goals does it serve? Does it survive the guardrails? What is its daily cost in LLM tokens and in operator attention? If the answers take more than a minute, the idea needs a smaller version.
