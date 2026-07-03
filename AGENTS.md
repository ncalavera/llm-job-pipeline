# Agent instructions

This repo is agent-agnostic. It works with Claude Code, Codex, or any coding
agent that can run shell commands and follow markdown runbooks.

## Read STRATEGY.md first (hard rule)

Before ANY product work — a feature, a prompt or runbook change, a new source,
board, or flag — read `STRATEGY.md`. It is the decision frame: three goals and
eight guardrails every change must pass. If a request or ticket conflicts with
STRATEGY.md, STOP and ask the user which wins; do not proceed on the conflicting
path. A change that fails a guardrail gets rewritten or rejected, however useful
it is to one user.

## Where the runbooks live

`.claude/commands/*.md` are plain-markdown, step-by-step runbooks — they are
NOT Claude-specific despite the folder name. If you are Claude Code they load
as slash commands automatically; any other agent should open the file and
follow it verbatim when the user asks for that workflow:

| User asks for | Runbook |
| --- | --- |
| first-time setup, daily fetch + score, resume, deploy | `.claude/commands/jobs-new.md` |
| review liked vacancies, archive low scores, terminal triage | `.claude/commands/jobs-review.md` |
| check scoring quality against your own labels (golden set) | `.claude/commands/jobs-eval.md` |
| add a company or job board | `.claude/commands/jobs-add.md` |
| update scoring rules or candidate profile | `.claude/commands/jobs-profile.md` |
| Telegram digest (send / poll) | `.claude/commands/jobs-digest.md` |
| pull latest code, apply DB migrations | `.claude/commands/jobs-update.md` |

The daily loop (`jobs-new.md`) is driven by `scripts/run_daily.py` — a plain
Python state machine that owns stage ORDER, checkpoints, the heartbeat and the
publish gate. Any agent runs `python3 scripts/run_daily.py`, watches for exit
code 10 (a GATE), does the printed judgment task (scoring / verdicts), then
`--resume`s — repeating until exit 0. The agent never orders the stages; it only
answers the gates. Exit codes: 0 done, 10 gate, 20 abort, 30 stage error.

Install guides: `INSTALL-EASY.md` (simple mode, zero signups) and
`INSTALL.md` (full mode, Supabase + Vercel).

`CONCEPTS.md` at the repo root defines the project's shared domain
vocabulary (entities, named processes, status concepts) — relevant when
orienting to the codebase or naming things. A local, untracked
`docs/solutions/` folder may also exist on maintainer checkouts: documented
solutions to past problems (bugs, best practices, workflow patterns),
organized by category with YAML frontmatter (`module`, `tags`,
`problem_type`) — worth searching when implementing or debugging in
documented areas.

## The scoring contract (any LLM agent)

Scoring does not call any LLM API from Python — the orchestrating agent IS
the scorer. The loop:

1. `python3 scripts/score_vacancies.py --local --limit N` prints JSON to
   stdout: a list of vacancies, each item carrying its own `system_prompt`,
   `user_msg` and `member_ids`.
2. For EACH vacancy independently (never batch several into one request —
   batching causes systematic over-scoring), evaluate `system_prompt` +
   `user_msg` with the model tier from your profile's `## VOLUME` settings and
   produce the JSON object the prompt requests (score, reasoning, tags,
   hard_requirements, short_summary).
3. Collect results as a JSON array and pipe it to
   `python3 scripts/score_vacancies.py --save` (stdin). Use `member_ids` from
   step 1 to address vacancies — not your own ids.
4. Keep the full `short_summary` text (4–6 sentences) — short summaries break
   the dashboard cards.

Claude Code does this with one subagent per vacancy; Codex and others should
replicate the same one-vacancy-per-request discipline. Scoring quality was
benchmarked with Claude models; other models work but calibration may differ.

**Two-pass scoring (the daily driver).** To spend the strong model only where it
matters, the daily driver scores in two passes: a cheap `screen_model` (default
Haiku) scores every new vacancy, then the strong `scoring_model` re-scores only
the finalists whose screen score clears `escalate_threshold` (default 50);
everything below the floor keeps its cheap score. Both passes keep the
one-vacancy-per-subagent rule. Because model calibration differs, the cheap
screen uses its own floor, not score parity: the floor was tuned against the
golden set so the screen drops none of the roles the strong model would surface.
The direct `score_vacancies.py --local` contract above is the single-pass
fallback for agents not driven by the daily runner.

## Ground rules

- Python scripts auto-load `.env` from the repo root (via `db_backend`; an
  already-exported shell var wins over the file); never commit `.env` or
  `config/user_profile.md` (both gitignored).
- `config/user_profile.md` is the user's candidate profile — treat as private
  data, never paste it into commits, issues, or logs.
- Without `SUPABASE_DB_URL` set, the pipeline runs on a local SQLite file
  that auto-creates on first use (simple mode). With it set, Postgres
  (Supabase). Same scripts either way.
- DAL writes are not auto-committed. `save_vacancies()`,
  `auto_review_candidates()`, and the other write functions in
  `database_supabase.py` stage their changes on the shared connection but leave
  the commit to the caller, so a direct caller that forgets `get_conn().commit()`
  silently loses its data. The fetch/score/filter scripts already commit at their
  logical checkpoints; if you call the DAL yourself (e.g. in a one-off script),
  commit before exit. The rule has exactly three documented exceptions:
    1. `archive_vacancies()` commits internally, then writes its on-disk JSON
       archive AFTER the commit — the delete and its disk artifact must stay
       atomic, so this function owns its transaction.
    2. `report.generate_dashboard()` (full mode) commits the `dashboard_snapshot`
       upsert — the report sink. Callers must commit their own pending data
       writes BEFORE calling it, or the snapshot commit sweeps them up by chance.
    3. `telegram_digest.py` opens its own separate `autocommit=True` connection
       (the digest poller), not the shared DAL singleton.
  Consequence for `--report-only`: `fetch_vacancies.py --report-only` must NOT
  stage any source-data mutation (e.g. `pass_expired_vacancies()` stays inside
  the fetch guard) — a report run only re-renders the dashboard from the data,
  it never changes it. Otherwise the dashboard snapshot commit would persist it.
- Run `python3 -m pytest tests/ -q` after changing pipeline code — the suite
  runs offline. (`pytest` and `pydantic` are not in the easy-mode install; add
  them with `pip install pytest pydantic` to run the full suite.)
- `docs/solutions/` — local knowledge store (gitignored, not in the
  public repo): documented solutions to past problems (bugs, best
  practices, patterns), organized by category with YAML frontmatter
  (`module`, `tags`, `problem_type`). Relevant when implementing or
  debugging in documented areas.
- `CONCEPTS.md` — shared domain vocabulary (entities, named processes,
  status concepts). Relevant when orienting to the codebase or
  discussing domain concepts.
