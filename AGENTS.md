# Agent instructions

This repo is agent-agnostic. It works with Claude Code, Codex, or any coding
agent that can run shell commands and follow markdown runbooks.

## Where the runbooks live

`.claude/commands/*.md` are plain-markdown, step-by-step runbooks — they are
NOT Claude-specific despite the folder name. If you are Claude Code they load
as slash commands automatically; any other agent should open the file and
follow it verbatim when the user asks for that workflow:

| User asks for | Runbook |
| --- | --- |
| first-time setup | `.claude/commands/jobs-start.md` |
| daily check ("/jobs") | `.claude/commands/jobs.md` |
| fetch vacancies | `.claude/commands/jobs-fetch.md` |
| filter junk | `.claude/commands/jobs-filter.md` |
| score vacancies | `.claude/commands/jobs-score.md` |
| add a company | `.claude/commands/jobs-add.md` |
| triage liked vacancies | `.claude/commands/jobs-apply.md` |
| archive low scores | `.claude/commands/jobs-archive.md` |
| terminal triage | `.claude/commands/jobs-vac.md` |
| Telegram digest | `.claude/commands/jobs-digest.md` |

Install guides: `INSTALL-EASY.md` (simple mode, zero signups) and
`INSTALL.md` (full mode, Supabase + Vercel).

## The scoring contract (any LLM agent)

Scoring does not call any LLM API from Python — the orchestrating agent IS
the scorer. The loop:

1. `python3 scripts/score_vacancies.py --local --limit N` prints JSON to
   stdout: a list of vacancies, each item carrying its own `system_prompt`,
   `user_msg` and `member_ids`.
2. For EACH vacancy independently (never batch several into one request —
   batching causes systematic over-scoring), evaluate `system_prompt` +
   `user_msg` with your strongest available model and produce the JSON object
   the prompt requests (score, reasoning, tags, hard_requirements,
   short_summary).
3. Collect results as a JSON array and pipe it to
   `python3 scripts/score_vacancies.py --save` (stdin). Use `member_ids` from
   step 1 to address vacancies — not your own ids.
4. Keep the full `short_summary` text (4–6 sentences) — short summaries break
   the dashboard cards.

Claude Code does this with one subagent per vacancy; Codex and others should
replicate the same one-vacancy-per-request discipline. Scoring quality was
benchmarked with Claude models; other models work but calibration may differ.

## Ground rules

- Python scripts read `.env` from the repo root; never commit `.env` or
  `config/user_profile.md` (both gitignored).
- `config/user_profile.md` is the user's candidate profile — treat as private
  data, never paste it into commits, issues, or logs.
- Without `SUPABASE_DB_URL` set, the pipeline runs on a local SQLite file
  that auto-creates on first use (simple mode). With it set, Postgres
  (Supabase). Same scripts either way.
- DAL writes are not auto-committed. `save_vacancies()` and the other write
  functions in `database_supabase.py` stage their changes on the shared
  connection but leave the commit to the caller, so a direct caller that forgets
  `get_conn().commit()` silently loses its data. The fetch/score/filter scripts
  already commit at their logical checkpoints; if you call the DAL yourself
  (e.g. in a one-off script), commit before exit.
- Run `python3 -m pytest tests/ -q` after changing pipeline code — the suite
  runs offline. (`pytest` and `pydantic` are not in the easy-mode install; add
  them with `pip install pytest pydantic` to run the full suite.)
