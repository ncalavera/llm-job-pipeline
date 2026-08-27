---
description: Headless nightly scoring session. Invoked by scripts/nightly_run.py as `/jobs-night <gate> <night_dir>` — one session per scoring gate. Reads payload files from <night_dir>/score_in/, fans them out to night-scorer subagents (file-in/file-out), saves after every wave of five, then resumes the driver exactly once. Never asks a question; never writes a verdict.
---

# /jobs-night — one scoring gate, unattended

Arguments: `$ARGUMENTS` = `<gate> <night_dir>` where `<gate>` is one of
`screen_companies | score_companies | score_vacancies` and `<night_dir>` is
this night's private directory (`vacancies/nightly/<date>/`).

This is the NIGHT variant of the `/jobs-new` gate protocol. The pipeline
orchestration already ran in `scripts/run_daily.py --unattended`; your only job
is the judgment for ONE gate, then one `--resume`. Everything below overrides
the interactive habits of `/jobs-new`.

## Overrides — read first, they are absolute

1. **Never ask a question.** There is no human. If something is ambiguous,
   take the safe direction (skip the item, record it failed in the log) and
   keep going.
2. **Subagent model comes from settings.** Resolve it ONCE:
   `python3 -c "import sys;sys.path.insert(0,'scripts');from scoring_settings import scoring_model;print(scoring_model())"`
   and pass that model to EVERY scoring subagent. Do not hardcode a model name.
3. **Spawn only the `night-scorer` agent type.** One payload file = one
   `night-scorer` subagent. No other agent type, ever. At most **5 subagents
   at a time** (rolling waves). 1 item = 1 subagent — batching over-scores.
4. **File in, file out.** Each subagent reads its own payload
   `<night_dir>/score_in/NNN.json` and writes its one result to
   `<night_dir>/score_out/NNN.json` (same NNN). Subagents have Read and Write
   only — you (the orchestrator) run every shell command.
5. **Save after EVERY wave of five, never once at the end** (a dead session
   must not lose finished work). Always the `--files` form — a malformed file
   is named and skipped, the rest still save:
   - `score_vacancies` gate:
     `python3 scripts/score_vacancies.py --save --scored-by "<model>" --files <night_dir>/score_out/<wave files>`
   - `score_companies` gate:
     `python3 scripts/score_companies.py --save --files <night_dir>/score_out/<wave files>`
   - `screen_companies` gate:
     `python3 scripts/screen_candidates.py --save --files <night_dir>/score_out/<wave files>`
6. **Log after every wave.** Append one line per wave to
   `<night_dir>/scoring_log.md`: time, gate, items in the wave, saved count,
   failures with their NNN. Findings and anomalies go HERE, not to an issue
   tracker.
7. **Resume exactly once, at the very end.** Only after every score_in item is
   saved or recorded failed in the log:
   `python3 scripts/run_daily.py --resume --unattended`
   Then STOP when the driver exits with anything but 10. If it exits 10 again,
   do NOT start a new scoring round yourself — end the session; the wrapper
   owns the loop.

## Forbidden — these end the session as a failure if you do them

- Any vacancy status write (`vac.py mark`, verdict SQL, `update_vacancy_status`).
- `learning.py apply` (a learning proposal needs the human's yes).
- `gh issue create` (write findings to `<night_dir>/scoring_log.md` instead).
- `--full-rescore`.
- Publish / dashboard rebuilds (`fetch_vacancies.py --report-only`, deploys).
- `git commit`, `git push`, or editing anything under `.claude/`.
- Passing `--archive` to any `--save` command.

## Gate protocol

Common to every gate: list `<night_dir>/score_in/*.json`. Each payload file
carries its own `system_prompt` + `user_msg` and the real DB ids. Give each
`night-scorer` subagent (model from override 2): the payload path, the exact
result path `<night_dir>/score_out/NNN.json`, and the result shape for the
gate (below). Run waves of 5: spawn, wait, save the wave (override 5), log the
wave (override 6), next wave. A subagent that returns garbage or writes no
file: record it failed in the log and move on — the driver re-prompts for
whatever is missing tomorrow.

### score_vacancies

The scoring contract from `/jobs-new`, night form. 1 vacancy = 1 subagent.
Result file — ONE flat JSON object:

```json
{"member_ids": [...from the payload...], "org": "...", "title": "...",
 "score": 0-100, "reasoning": "...", "tags": [...],
 "hard_requirements": [...], "short_summary": "..."}
```

Pure-fit scoring: judge role fit only — geography/visa were handled by the
filter stage. Save with the `score_vacancies.py` line from override 5; the
`--scored-by` model is the same resolved model every subagent used.

### score_companies

WANT-scoring new candidate companies. 1 company = 1 subagent. The result is
the payload's requested enrichment verdict; wrap it exactly as the payload's
`system_prompt` demands (the saver expects the result under `"enrichment"`
with the payload's `id`):

```json
{"id": "<from the payload>", "enrichment": { ...the subagent's scoring JSON... }}
```

Save with the `score_companies.py` line from override 5. Scored companies land
in Pending for the human's morning review — you never approve or reject one.

### screen_companies

The no-API-key relevance screen: decide only what reaches PAID enrichment.
Drop ONLY clear mismatches (staffing agencies, plainly out-of-profile
commercial businesses, obvious duplicates); borderline or unknown from the
name alone → keep. 1 company = 1 subagent. Result file — ONE JSON object:

```json
{"id": "<from the payload>", "keep": true, "reason": "<one short sentence>"}
```

Save with the `screen_candidates.py` line from override 5. An unanswered
company stays kept — the safe direction.

## End of session

After the single `--resume --unattended` (override 7), append a final
`scoring_log.md` line: items total, saved, failed, driver exit code. Then stop.
