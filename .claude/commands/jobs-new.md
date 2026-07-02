---
description: The one daily command. A Python driver runs the whole pipeline in a fixed order — validate → fetch → enrich → filter → score → verdicts → publish — with checkpoints, a live progress card, and a publish gate. You (the agent) only supply judgment at the gates it stops on: scoring, WANT-scoring companies, and like/pass verdicts. First run auto-onboards an empty database.
---

# /jobs-new

One command a day. The deterministic orchestration — stage ORDER, batching,
checkpoints, the heartbeat, and the publish gate — lives in
`scripts/run_daily.py`, NOT in this file. You cannot run the stages out of
order, because you do not drive them; the driver does. Your job is only the
JUDGMENT it pauses for. Works with **any** coding agent that runs shell and
follows this file: the driver does the Python; you do the LLM scoring and talk
to the user at the gates.

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

   Boards are opt-in (off by default): add e.g.
   `--boards "80k_hours,impactpool,idealist,fast_forward,linkedin,datadotorg"`
   (catalogue in `docs/job-boards-catalogue.md`).

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

### Company scoring (WANT-score new candidate companies)
The driver scrapes about-pages and prints scoring payloads to
`vacancies/score_companies_payload.json`. For **each** company, run **one**
subagent (`model: "sonnet"`) with the payload's `system_prompt` + `user_msg`;
**1 company = 1 subagent**, at most **5 at a time** (rolling waves). Save
incrementally (each `--save` commits):

```bash
python3 scripts/score_companies.py --save < chunk.json   # wrap each result under "enrichment"
```

Scored companies land in **Companies → Pending** for approval (deeper review in
`/jobs-review companies --status candidate`). Then `--resume`.

### Vacancy scoring (the scoring contract)
The driver prints per-vacancy payloads to
`vacancies/score_vacancies_payload.json`, already capped for the day. For
**each** vacancy, run **one** subagent with the model from your profile's
`[## VOLUME] scoring_model` (Sonnet on a budget plan, Opus on a bigger one) and
the payload's `system_prompt` + `user_msg`. **Critical: 1 vacancy = 1 subagent**
— batching over-scores by +20–50. At most **5 subagents at a time**. Save
incrementally with the flat fields (`member_ids`, `score`, `reasoning`, `tags`,
`hard_requirements`, `short_summary`):

```bash
python3 scripts/score_vacancies.py --save < chunk.json
```

Then `--resume`; the driver re-checks and re-prompts only for anything still
unscored. Pure-fit scoring: the prompt judges role fit only — geography/visa
were handled in the filter stage, so a great role in the wrong place still
scores high.

### Verdicts (quick daily triage)
List the freshly scored, still-unseen vacancies (highest score first) and, for
each, ask like / pass / skip. Write **each** verdict immediately (one commit
each, so an interruption keeps captured decisions):

```bash
python3 -c "import sys;sys.path.insert(0,'scripts'); \
from database_supabase import update_vacancy_status; from db_conn import get_conn; \
update_vacancy_status('<VACANCY_ID>','liked'); get_conn().commit()"
```

Statuses: `liked | passed | skipped | to_apply`. A plain `passed` = "not for me"
and calibrates scoring. If a role is **garbage** — it should never have reached
scoring (a filter hole) — pass it *and* flag it so next run's learning review can
propose a filter:

```bash
python3 scripts/learning.py record-garbage --vacancy <VACANCY_ID> \
    --title "<title>" --source "<board/ats>" --score <llm_score>
```

The deep structured review of liked roles lives in `/jobs-review`. Then
`--resume` to publish.

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
- `--boards "a,b,c"` — enable job boards for this run (default: none).
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
