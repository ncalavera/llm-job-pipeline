# Manual trial protocol

A ~30-minute hands-on checklist for a smart non-engineer to run before a
release. It covers what CI cannot: does the product actually feel right when
a stranger follows the docs verbatim, with no insider knowledge? CI covers
three synthetic personas and cost/volume math with fixtures and no network
calls — see `tests/` for that half. This document is the other half: the
parts that need a real terminal, a real clock, and (for the last section) a
real second person.

Every step below traces back to a claim already made in a shipped doc
(README, INSTALL.md, INSTALL-EASY.md, AGENTS.md, docs/ARCHITECTURE.md) or to
one of the five failure modes the original user test surfaced (see
`STRATEGY.md` and the audit that produced DHA-350). If a step fails, that is
either a real bug or a doc that promises something the code does not — note
which, do not guess.

**Setup — do this once, before Section 1.**

- Clone into a **scratch directory outside any existing checkout** — never
  reuse a maintainer's real working copy, which may hold a live `.env`,
  `config/user_profile.md`, or database. If you already have a
  `llm-job-pipeline/` folder from other work, pick a distinct name so the two
  never collide:

  ```bash
  cd ~ && git clone https://github.com/ncalavera/llm-job-pipeline.git manual-trial-simple
  cd manual-trial-simple
  ```

- Test persona (use these answers verbatim at onboarding, for a reproducible
  run): a backend engineer named Alex Rivera, based in Berlin, targeting
  remote or Berlin-based roles, EU passport (no visa sponsorship needed), 4
  years of experience shipping backend services. This persona is invented,
  generic, and deliberately has no impact/nonprofit signal — it is the
  control case for the "boards" failure mode (Section 4, step 1).

Total for Sections 1–4: **~30 minutes**. Section 5 (the live retest with a
real person) is separate and not timeboxed the same way — budget 45–60
minutes of that person's time, once.

---

## Section 1 — T4, honest demo: simple mode from nothing (~10 min)

Follows INSTALL-EASY.md verbatim. No `.env` at any point in this section.

1. **Action.** Confirm no `.env` exists: `ls -a | grep -x .env` should print
   nothing.
   Expected: no file. This is the precondition for the whole section — do
   not run `cp .env.example .env` here.
   Fails if: a `.env` is already present (stale scratch dir — delete and
   re-clone).

2. **Action.** `python3 -m pip install requests beautifulsoup4 python-dateutil`
   then `./scripts/install-hooks.sh` (INSTALL-EASY.md §2).
   Expected: both commands exit 0; the hook script prints that it activated
   the pre-commit guard.
   Fails if: pip errors with `externally-managed-environment` — this is
   **documented**, not a failure: create a venv (`python3 -m venv .venv &&
   source .venv/bin/activate`) and re-run the pip install, per the doc's own
   fallback. A crash with any other message is a real failure.

3. **Action.** Open a coding agent in this folder and run `/jobs-new`
   (INSTALL-EASY.md §3 / `.claude/commands/jobs-new.md` onboarding gate).
   Expected, in order: the agent confirms `Backend: local SQLite (...)`;
   sets up `config/user_profile.md` by asking you to describe yourself in
   plain language (field, target roles, locations, visa status — answer with
   the Alex Rivera persona above); web-searches 10–15 real companies and
   shows a shortlist for a yes/no; on approval, runs the first fetch →
   filter → score cycle; opens the local dashboard.
   Fails if: any step is skipped silently, the agent asks something the
   onboarding gate text does not cover, or the banner says
   `Backend: Postgres (Supabase)` — the latter means `SUPABASE_DB_URL` leaked
   from your shell environment (a documented trap in INSTALL-EASY.md); running
   `unset SUPABASE_DB_URL SUPABASE_DIRECT_URL` and restarting the shell is the
   documented fix, not a bug — but if the banner is silently wrong (no warning
   printed either way), that is a bug.

4. **Action.** At the company shortlist, before approving, scan the list.
   Expected: every company plausibly fits a backend engineer in Berlin. No
   niche impact/nonprofit board (`80k_hours`, `impactpool`, `idealist`,
   `reliefweb`, `datadotorg`, `fast_forward`, `consultants_for_impact` — the
   full catalogue is `docs/job-boards-catalogue.md`) is pre-enabled or
   suggested as a "starter."
   Fails if: an impact-sector company or board shows up unprompted for this
   non-impact persona — this is the exact "boards" failure from the original
   test recurring.

5. **Action.** Approve the shortlist, let the run finish, then open
   `http://127.0.0.1:8000/` (or run `python3 scripts/dashboard_local.py` if it
   did not auto-launch).
   Expected: dashboard loads with scored vacancies; six sections are visible
   (Today, Vacancies, Companies, Applications, Boards, Settings).
   Fails if: you see the "Run `--report-only` first" warning from
   INSTALL-EASY.md's troubleshooting section despite `/jobs-new` having
   already completed — the doc says that warning means data.js is missing,
   which should not be true right after a finished run.

---

## Section 2 — T4, honest demo: upgrade to Supabase (~8 min)

Continues in the **same scratch clone** from Section 1 — this is the
documented "easy → hardcore" migration path (INSTALL-EASY.md "Upgrading to
hardcore later" → INSTALL.md from step 3), not a fresh clone.

1. **[human]** Create a free Supabase project, any name/region
   (INSTALL.md §3.1). A throwaway project reused across trial runs is fine.
   Expected: project ready in the Supabase dashboard.

2. **Action.** Supabase SQL Editor → paste the contents of `sql/schema.sql` →
   run (INSTALL.md §3.2).
   Expected: success message, `company` and `vacancy` tables now exist.
   Fails if: the paste errors — the doc's schema does not match what
   `migrate.py` expects on a fresh database (flag as a finding, do not
   patch it).

3. **Action.** Copy the **Session pooler** connection string (Project
   Settings → Database), `cp .env.example .env`, fill in `SUPABASE_DB_URL`,
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (INSTALL.md §4).
   Expected: `.env` created with the three values filled.
   Fails if: nothing — this step just prepares the next one. (If you paste
   the *direct* connection string instead of the pooler one, INSTALL.md's
   troubleshooting section predicts a `psycopg2` connection failure later —
   that is the documented behavior, not a bug, if the error text matches.)

4. **Action.** `python3 -m pip install -r requirements.txt` (installs
   `psycopg2`, which simple mode skipped — INSTALL-EASY.md "Upgrading to
   hardcore later" step 1).
   Expected: installs cleanly.
   Fails if: a later script crashes with a raw traceback instead of the
   documented `psycopg2 is not installed — pip install -r requirements.txt`
   message (only relevant if you skip this step to test the message itself).

5. **Action.** `python3 scripts/migrate.py`.
   Expected: banner shows the Postgres backend; pending migrations apply
   with an automatic backup note; ends with either a list of applied
   versions or "Up to date."
   Fails if: it crashes, or applies without ever printing which backend it
   picked.

6. **Action.** `python3 scripts/fetch_vacancies.py --report-only`.
   Expected: the banner explicitly prints `Backend: Postgres (Supabase)`.
   Fails if: it still says SQLite — `.env` is not being picked up.

7. **Action.** `/jobs-add <any company>` and check the new company's status
   in the dashboard's Companies → Pending view.
   Expected: it lands as `candidate`, awaiting your review — the same gate
   simple mode used in Section 1, per INSTALL-EASY.md's explicit claim that
   "a board/ATS-discovered company lands in the same review gate on both...
   easy mode does not skip it."
   Fails if: the company is auto-`active` here while it was `candidate` (or
   vice-versa) in Section 1 — a real backend-parity regression, not a
   documented difference.

---

## Section 3 — T5, Russian user (~5 min)

1. **Action.** In the scratch clone's `config/user_profile.md`, edit the
   `## OUTPUT_LANGUAGE` section to `Russian` (the same section shipped as
   `English` in `config/user_profile.example.md`).
   Expected: file saves; no other section needs to change.

2. **Action.** Run `/jobs-new` (or `/jobs-review`) again.
   Expected: the agent's chat replies, the run banner, and the end-of-run
   summary are all in Russian — per `.claude/commands/jobs-new.md`'s "Reply
   in the user's product language" instruction and
   `docs/ARCHITECTURE.md`'s claim that `## OUTPUT_LANGUAGE` drives "the
   agent's replies... the run banner + summary."
   Fails if: chat stays in English despite the profile change.

3. **Action.** Reload the local dashboard.
   Expected: dashboard chrome (section names, labels, buttons) renders in
   Russian — `## OUTPUT_LANGUAGE` is documented as the dashboard's default
   language source.
   Fails if: the dashboard stays English-only.

4. **Action (optional, full mode only).** If Telegram is configured,
   `python3 scripts/telegram_digest.py send --limit 5`.
   Expected: digest text in Russian.
   Skip and note "not tested" if Telegram was never set up — do not treat
   the skip as a failure.

5. **Note, don't fail:** raw script stdout/stderr and `--status` output are
   **not** in scope for translation per the shipped design (only chat,
   banner/summary, digest, and dashboard chrome are) — if one of those four
   scoped surfaces stays English, that is the finding; a Python script's log
   line staying English is expected.

---

## Section 4 — Manual slices of T1 / T3 / T6 (~7 min)

Build on the run from Section 1 — no new clone needed.

1. **T1 — no insider knowledge.** Re-read the onboarding questions the agent
   asked in Section 1 step 3.
   Expected: every question is answerable by someone who has never seen this
   repo before — plain language, no jargon (ATS, WANT score, ticket IDs) left
   unexplained.
   Fails if: a question assumes context only the maintainer would have.

2. **T3 — started and walked away.** Recall Section 1 step 3: count how many
   times the agent asked you something between hitting Enter on `/jobs-new`
   and reaching the company shortlist (the first documented gate).
   Expected: zero. The driver fetches/validates silently until the first
   gate, per `.claude/commands/jobs-new.md`: "The driver never asks
   questions mid-run."
   Fails if: it asked anything not listed as a gate in that file.

3. **T3 — self-explanatory summary.** Read the final one-screen summary
   printed at the end of the run.
   Expected: plain language, every number explained (e.g. "found N new
   companies, M scored, K awaiting your review") — no internal stage names,
   column names, or ticket references.
   Fails if: you have to guess what a number means.

4. **T6 — volume levers are documented.** Without running anything new, open
   README.md's "What it costs" section and `config/user_profile.example.md`'s
   `## VOLUME` section.
   Expected: you can find, within a couple of minutes of reading, (a) the
   setting that caps how much gets scored in one day (`max_per_run`, `##
   VOLUME` in the profile) and (b) how to turn off a job board
   (`python3 scripts/sources.py disable-board <id>`, documented in
   `docs/job-boards-catalogue.md`).
   Fails if: neither lever is discoverable from the docs alone.

5. **T6 — Today tab stays reviewable.** Open the dashboard's Today section.
   Expected: the "New 70+" list is a size you could realistically read in one
   sitting.
   If it is not: this is worth a finding note (not a required fail) — the
   shipped Today view filters by score/status but does not hard-cap the list
   length; write down the count you saw.

---

## Section 5 — Live retest with a real person

The final gate: repeat the original failed user test with a real second
person (a friend, not the maintainer) driving the keyboard. You observe and
take notes; you do not answer questions from your own knowledge of the repo
— if you have to explain something the docs don't, that is itself a finding
for the "operator knowledge" row below. Give them the repo URL (or the
onboarding questionnaire page) and let them go.

Structure the observation around the five failure classes the original test
produced:

| Failure class | What to observe | Passes | Original failure recurring if |
| --- | --- | --- | --- |
| **Boards** | Which boards end up enabled after their onboarding; what `sources.py recommend` proposes for their actual field | Only boards matching their stated field are enabled or suggested | Any impact/nonprofit board is enabled or recommended without a match in their own words |
| **Cost** | What plan tier + model they picked at onboarding; whether a normal day's `/jobs-new` finishes without a plan-usage scare | They complete a normal run without exhausting their coding-agent plan outside the documented per-run cap | They report running out of usage mid-run, or the run scores well past `max_per_run` unannounced |
| **Modes** | Ask them, unprompted, to explain in their own words when they'd use simple vs full mode | They state the real difference (local file vs hosted, phone access, digest) from the README table alone | They guess, or believe the two modes score/filter differently |
| **Overload** | Their reaction to the first day's result count and the Today tab | They either find the volume manageable, or find a volume lever themselves, unaided | They say they feel overwhelmed and cannot find a way to shrink it without your help |
| **Operator knowledge** | Whether they ever get stuck needing a step only you know | The agent's own gate text tells them what to do next at every pause | They ask you "what do I do now" at a point the driver should have answered itself |

Record the outcome per row (pass / original failure recurred, with a one-line
note) — that record is the acceptance evidence for DHA-350's "live test
passed without the five original failures" criterion.
