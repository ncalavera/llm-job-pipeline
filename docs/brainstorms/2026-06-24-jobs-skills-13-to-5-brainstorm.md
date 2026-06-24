---
date: 2026-06-24
topic: jobs-skills-13-to-5
linear: DHA-275
---

# Simplify job-pipeline skills: 13 → 6 commands

## What We're Building

Collapse the sprawling job-pipeline command surface into a small, clean set
under the `jobs-*` prefix. Today the same runbooks exist in three drifted
copies; the daily/review flows are split across many narrow commands. We
consolidate to **6 commands** (5 daily + 1 maintenance util), one source of
truth, and fix the install funnel (landing page) to match.

## Final Command Set (6)

| Command | Absorbs | Notes |
| --- | --- | --- |
| `/jobs-new` | `jobs`, `jobs-fetch`, `jobs-filter`, `jobs-score`, `jobs-finish`, `jobs-start` | Daily run: fetch+filter+score, then auto-publish. Sub-stages via flag (`/jobs-new fetch`). **First run** auto-detected (empty DB → onboarding: discover starter companies, first fetch/score, launch dashboard). |
| `/jobs-review` | `jobs-apply`, `jobs-archive`, `jobs-vac` | Decisions on liked / low / terminal triage. Auto-publish after changes. |
| `/jobs-add` | `jobs-add` (+new) | Add a **company** OR a **job board** OR a **single vacancy** by hand. |
| `/jobs-profile` | `jobs-rules` (+update) | Update existing profile + HARD-filters/rules. Source: `config/user_profile.md`. First-time creation stays on the landing page. |
| `/jobs-digest` | `jobs-digest` | **Telegram only** for now. Email + WhatsApp deferred → DHA-282. |
| `/jobs-update` | `jobs-update` | Maintenance util: git pull + DB migrations + summary. Kept separate from the daily loop on purpose. |

## Key Decisions

- **One source of truth.** Delete `.agents/skills/source-command-jobs-*`
  entirely — they are a second hand-maintained copy with no generator and have
  already drifted (e.g. `jobs-add`: command calls `save_vacancies`, the copy
  still calls old `merge_vacancies`). `.claude/commands/*.md` is canonical and
  already agent-agnostic (per AGENTS.md). Nothing references the copies (grep
  confirmed).
- **Keep `jobs-*` prefix** (not the `*-jobs` suffix from the ticket). The prefix
  groups all commands together in autocomplete; the suffix scatters them.
- **6, not 5.** `jobs-update` (maintenance) doesn't fit fetch/review/add/
  profile/digest, so it stays as a 6th util rather than being forced or dropped.
- **First-run folds into `/jobs-new`** via empty-DB detection — no separate
  onboarding command.
- **Auto-publish, no asking.** In full mode `/jobs-new` and `/jobs-review` end
  with regen `public/data.js` + git commit + push (Vercel deploy), every run, no
  confirm. Simple mode just regenerates `data.js` locally (nothing pushed).
- **Profile creation already exists on the landing** (`buildProfile()` in
  `docs/index.html`) — `/jobs-profile` does NOT rebuild the questionnaire.
- **Landing page must be updated** (part of "done"): its install instructions
  (RU + EN, `docs/index.html` ~lines 1658–1697) hardcode old command names
  (`/jobs-start`, `/jobs`, `/jobs-fetch`, `/jobs-filter`, `/jobs-score`). After
  the rename those are dead commands for new users.

## Scope Boundaries

- **In:** rename + merge to the 6 commands; delete the `.agents` copies and the
  legacy global duplicates in `~/.claude/skills/` (fetch, score, filter, triage,
  archive, enrich, add-source, vac); new `/jobs-add` modes (board, single
  vacancy); landing page update; docs update (AGENTS.md, README*, INSTALL*,
  ARCHITECTURE.md, DATABASE.md); lint for stale command names.
- **Out (deferred):** email + WhatsApp digest channels → **DHA-282**.

## Acceptance

- 6 commands work; all others removed; no duplicate copies remain.
- No old command names left in code or docs (grep clean), including the landing.
- pytest green (604+); every command/skill loads.
- `/jobs-new` and `/jobs-review` auto-publish without a separate ship step.
- Landing install funnel points new users at the new commands.

## Open Questions (for planning)

- Exact sub-stage flag names (`/jobs-review apply|archive|vac`?).
- Whether the legacy `~/.claude/skills/` global duplicates are safe to delete
  without breaking other projects that might reference them.

## Next Steps

→ `/ce:plan` (or `superpowers:writing-plans`) for the implementation plan.
