---
description: The one daily command. A Python driver runs the whole pipeline in a fixed order — validate → fetch → enrich → filter → prepare evidence → publish — with checkpoints, a live progress card, and a publish gate. You (the agent) only supply judgment at the gates it stops on: quoted evidence preparation and profile comparison. First run auto-onboards an empty database.
---

# /jobs-new

One command a day. The deterministic orchestration — stage ORDER, batching,
checkpoints, the heartbeat, and the publish gate — lives in
`scripts/run_daily.py`, NOT in this file. You cannot run the stages out of
order, because you do not drive them; the driver does. Your job is only the
JUDGMENT it pauses for. Works with **any** coding agent that runs shell and
follows this file: the driver does the Python; you prepare evidence at its gates.
Human keep/put-aside decisions happen in the dashboard.

**Reply in the user's product language.** Before you say anything to the user,
read the `## OUTPUT_LANGUAGE` section of `config/user_profile.md` (resolve it
with `python3 -c "import sys;sys.path.insert(0,'scripts');import product_language as p;print(p.resolve())"` → `en`/`ru`). Write ALL your chat, gate summaries and
progress notes in that language. The driver already prints its banner/summary in
it; match it. (Scoring/verdict *data* stays as the pipeline emits it — you are
translating your own words, not the DB.)

---

## The loop

1. **Launch the driver in the background and show the live card.** A foreground
   command's stdout is invisible until it exits, so a long fetch looks frozen.
   Launch with `run_in_background: true`, then every ~20–30s run
   `python3 scripts/run_card.py` and post its one-line output to chat
   (`fetch ▕███░░░▏ 18/40 · LinkedIn · +12 · 6m02s`). Pace polls with the
   wait / `Monitor` primitive — never a foreground `sleep` (blocked).

   ```bash
   python3 scripts/run_daily.py
   ```

   Boards you have **enabled** (via `/jobs-add board` or
   `python3 scripts/sources.py enable-board <id>`) fetch automatically every run
   — nothing to pass. `--boards "a,b,c"` adds more boards for THIS run only,
   unioned ON TOP of the persisted set; it never has to be repeated to keep an
   enabled board on. See what's enabled with `python3 scripts/sources.py`
   (catalogue in `docs/job-boards-catalogue.md`).

   Not sure which boards fit? `python3 scripts/sources.py recommend` proposes the
   ones that match **your** profile (target field, roles, geography) — an
   engineer is proposed engineering boards, not someone else's fixed set. It only
   suggests; enable what you want with `enable-board`. Boards never auto-enable.

2. **When it stops, read WHY.** The driver exits with a code and a printed block:

   | Exit | Meaning | You do |
   | --- | --- | --- |
   | 0  | DONE — pipeline complete | Relay the final one-screen summary. |
   | 10 | GATE — judgment needed | Do the printed task, then `--resume`. |
   | 20 | ABORT — bad profile / DB outage | Fix as told; do NOT retry blindly. |
   | 30 | ERROR — a stage crashed | Show the error; fix, then `--resume`. |

3. **On a GATE (exit 10),** do exactly what the block says (see the gates
   below), then continue — again in the background with the live card:

   ```bash
   python3 scripts/run_daily.py --resume
   ```

   Repeat until the driver prints DONE. Gates are idempotent: if you save only
   part of the work, `--resume` re-prompts for exactly what is still missing —
   it never redoes finished work. `python3 scripts/run_daily.py --status` shows
   the stage board at any time.

The driver never asks questions mid-run: the slow, silent work runs
autonomously while the user gets coffee; questions cluster at the gates.

---

## The gates — your only jobs

### Onboarding (only on an empty database)
Discover ~10–15 real employers that fit `config/user_profile.md`
(the candidate's target field + geography — whatever those are), validate each by
probing the public ATS APIs with `curl`
(Greenhouse / Lever / Ashby / Workable), show the shortlist and **wait for a
yes**, then `migrate.py` and insert the approved companies as `active`. Then
`--resume`. (The driver only reaches this gate when the company table is empty
AND the registry loaded fine — a DB outage aborts instead, never onboards.)

Also surface the boards that fit this profile —
`python3 scripts/sources.py recommend` (derived from the same profile) — and
enable only the ones the user confirms. Never enable boards for them by default.

### Learning review (verdict-driven corrections — before the fetch)
Runs before fetching when there are verdicts to learn from (skipped on the first
run and on a quiet day with nothing to review). The driver writes the review to
`vacancies/learning_review.json`; the deterministic mechanics (proposals,
backtests, rollover) live in `scripts/learning.py` — **no LLM calls**. Your job:

1. Read the payload. For **each** proposal, show the user the word/move **and its
   backtest** — clean means it would have killed 0 liked / ≥40-scored roles; a
   dirty candidate lists the exact roles it would have wrongly killed.
2. Apply **only** what the user approves (nothing changes without a yes; each
   apply is logged):
   ```bash
   python3 scripts/learning.py apply --type add_filter_word --word W
   python3 scripts/learning.py apply --type move_factor --factor "…" --keyword K
   python3 scripts/learning.py apply --type disable_board --board B
   ```
3. Filter-kill revision ("anything alive here?"): for any killed title the user
   says is actually good, weaken its culprit rule:
   ```bash
   python3 scripts/learning.py apply --type weaken_filter_word --word CULPRIT
   ```
4. When you engaged (even if you applied nothing), close the cycle so these
   verdicts don't reappear next run:
   ```bash
   python3 scripts/learning.py complete --agreement <n|skip> --applied <k>
   ```
   **In a hurry? SKIP** — do *not* run `complete`; just `--resume`. Skipped
   verdicts roll over and are reviewed next time together with new ones.

Then `--resume`.

### Screening preparation (default daily gate)

`prepare_screening` points to `vacancies/prepare_screening_payload.json` (use the
actual path printed by the gate). For each payload independently, run ONE
subagent using its `system_prompt` and `user_msg`; return the requested facts,
quotes, work profile and profile comparison. Do not produce a numerical score.
Save each result with its original `id` to a private JSON file, then:

```bash
python3 scripts/prepare_screening.py --save --files r1.json r2.json
python3 scripts/run_daily.py --resume
```

The driver reuses successful results until the posting or profile changes.
Failed preparations retry next run. Review and keep/put aside in the dashboard
Screen view; the daily command never waits for human verdicts on vacancies.
Company scoring and vacancy scoring remain available explicitly through
`score_companies.py` and `score_vacancies.py`, outside the daily path.

---

## Publish — automatic, gated

The driver publishes only a **clean** run: zero stage crashes AND no single org
that lost a large share of its live roles to gone-from-source archival (the
signature of a truncated fetch). A dirty run keeps the previous good snapshot;
the driver says so in the summary. In full mode publish refreshes the live
dashboard snapshot (browser refresh, no deploy); in simple mode it rewrites the
local `public/data.js`. Both go through the same driver — no mode branching.
`vercel --prod` is only for dashboard **code** changes and is never run here.

---

## Flags

- `--resume` — continue a gated/interrupted run.
- `--new` — start fresh, discarding any prior run state.
- `--status` — print the stage board.
- `--boards "a,b,c"` — extra job boards for THIS run, unioned ON TOP of the
  persisted enabled set (`scripts/sources.py`). Enabled boards fetch every run
  without this flag; use `sources.py enable-board <id>` to make one stick.
- **Scope one run** without touching persisted state: `--tier S` (or A/B/C)
  fetches only companies of that importance tier, and `--skip-boards "linkedin"`
  drops boards from this run's effective set (subtracted after `--boards` and the
  persisted set resolve). An unknown board name in `--skip-boards` warns loudly
  (it never silently no-ops), and `--skip-boards all` skips every board for the
  run (companies still fetch). Both apply to a single fetch only — no company
  tier and no `board.enabled` flag is changed. Example: only S-tier employers on
  the social-impact boards, no LinkedIn → `--new --tier S --skip-boards linkedin`.
- `--no-publish` — run every stage but never publish (use from a git worktree).
- `--full-rescore` — explicit opt-in that LIFTS the per-run scoring cap and
  re-scores far more vacancies, with a loud warning. A normal run keeps the cap
  (a spike-day fuse); this is the only way to blow past it.

---

## Common issues

- **Firecrawl unset**: enrich and company scoring skip cleanly (accuracy drops).
- **A stage crashed (exit 30)**: fix the cause, then `--resume` — finished
  stages are not re-run.
- **Never** run `fetch_vacancies.py --report-only` from a git worktree (it
  clobbers the main copy's `public/data.js`) — use `--no-publish`.

---

## Bug-log routine (every run)

Treat each `/jobs-new` run as a self-iterating loop that also hunts for pipeline
bugs. Standing routine, no need to be asked:

1. **Open a bug log at the start** — `docs/jobs-new-bugs-<YYYY-MM-DD>.md` (the
   `docs/` tree is gitignored except a whitelist, so it stays local). Append as
   you go; don't wait until the end.
2. **Log every bug you hit** during the run — a stage crash, a scraper returning
   0 valid jobs when the page clearly has roles, a stale/nonsensical progress
   card, a wrong count, a driver exit that doesn't match reality, a payload that
   won't parse. One entry each: what happened, expected, impact, repro, suspected
   cause, severity. Fix inline only if it's blocking the run; otherwise log and
   keep going.
3. **At the end, route the bugs where your profile says** — read the
   `## BUG_TRACKER` section of `config/user_profile.md`. It names your issue
   tracker and destination (e.g. a Linear team, a GitHub repo, an Obsidian
   inbox). File ONE item there collecting that run's bugs, linking the log file,
   one checkbox per bug. If the section is empty or absent (the shipped default),
   the bug log file IS the record — don't invent a tracker; just tell the user
   where the log is.

Distinguish a *bug* (the pipeline misbehaved) from *expected noise* (a company
genuinely has no open roles, a board legitimately empty) — only real defects go
in the log.
