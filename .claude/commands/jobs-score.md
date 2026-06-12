---
description: Run LLM scoring via Claude Opus subagents. One subagent per vacancy (no batching). Saves score, reasoning, tags, hard_requirements, summary. Pure-fit scoring — geography handled by /jobs-filter, not by the scoring prompt.
---

# /jobs-score

Scores vacancies in parallel via subagents in Claude Code (within the subscription's token limits), without calling the Anthropic API directly. Default batch: 20 vacancies, 5 in parallel.

## Pure-fit scoring model

The scoring prompt evaluates **role fit only** — skills, seniority, domain, responsibilities. Geography and visa restrictions are **not** part of the LLM score; they are handled earlier in the `/jobs-filter` step via geo buckets. This means a great role in the wrong location still gets a high score so you can make an informed decision, rather than being silently downgraded.

## Steps

1. Read `config/user_profile.md` — make sure it is not empty and not equal to `user_profile.example.md`. If the profile is not configured, warn the user.

2. Pre-flight check: show total/scored/unscored counts and a breakdown of unscored vacancies by company.

3. **Data quality audit (mandatory before scoring):**
   - Count vacancies with full description / snippet only / no description.
   - If blind vacancies exceed 20% of candidates, show a strong warning and require explicit confirmation before proceeding.
   - At ≤ 20% blind, show an info-level warning and continue.

4. Ask:
   - How many vacancies to score (default 20)?
   - Only with empty `llm_score` (default) or rescore everything (`--rescore`)?

5. **Candidate companies** — scoring also pulls in strong vacancies from unreviewed ("candidate") companies when they have a high enough raw fit signal. These are capped and clearly labeled in the output. To disable this behavior, use `--no-candidates`.

6. Run (phase 1 — load):
   ```bash
   source ~/.zshrc 2>/dev/null && python3 scripts/score_vacancies.py --local --limit N
   ```
   The script prints a JSON array of vacancies to stdout.

7. Parse the JSON. For each vacancy launch a separate subagent with `model: "opus"`. Subagent prompt:
   - System prompt: `VACANCY_SCORING_PROMPT` (loaded template from `scripts/prompts/vacancy-scoring.md`).
   - User message: `VACANCY_SCORING_USER_TEMPLATE` with substitution.
   - Subagent returns JSON with fields: `score`, `reasoning`, `tags`, `hard_requirements`, `short_summary`.

8. Collect responses into an array `[{id, score, reasoning, ...}]`.

9. Save:
   ```bash
   echo '<JSON array>' | source ~/.zshrc 2>/dev/null && python3 scripts/score_vacancies.py --save
   ```

10. Show distribution: how many scored 75+, 55–74, 35–54, below 35.

11. **Session report** — generate a Markdown report at `vacancies/REPORT-scoring-{YYYYMMDD}.md` with:
    - Score distribution for this session
    - Top candidates (score ≥ 50)
    - Scraping quality issues found during scoring (not-a-vacancy artifacts, broken pages, incomplete descriptions)
    - Recommendations for `/jobs-filter` and `/jobs-fetch` pipeline improvements

## Auto-archive after scoring

After scoring, vacancies with `llm_score < LLM_SCORE_THRESHOLD` (default 20) and `status = unseen` can be auto-archived. **This is currently paused under pure-fit scoring** — a high score from a role in an excluded geography would be incorrectly archived. Auto-archive is opt-in only: confirm with the user before running `archive_vacancies()`.

## Two scoring modes

| Mode | Flag | Cost | Notes |
|------|------|------|-------|
| **Local** (Opus subagents) | `--local` (default) | $0 (included in subscription) | Primary mode |
| **OpenClaw** (SSH) | `--openclaw` | Server cost | Requires SSH access to configured host |

## Critical rules

- **1 vacancy = 1 subagent.** Never send 2–3 vacancies in one prompt — this causes systematic over-scoring of +20–50 points.
- **Use `member_ids`** from `--local` output when building the save payload, not the top-level `id`. The `member_ids` array contains the real Supabase UUIDs.
- **`flush=True`** — scripts already use `print(..., flush=True)` for progress. If progress is not visible in Claude Code, check that the script is not invoked via `subprocess` with a captured pipe.

## OpenClaw mode

```bash
source ~/.zshrc 2>/dev/null && python3 scripts/score_vacancies.py --openclaw --limit {BATCH_SIZE}
```

Uses SSH to a remote host configured via `OPENCLAW_SSH_*` environment variables.

## If scoring breaks

- `Empty profile`: `config/user_profile.md` not created or empty. Copy from `user_profile.example.md`.
- `Subagent timeout`: one subagent hung. Reduce parallelism in the orchestrator or re-run (scoring is idempotent).
- **High blind rate**: more than 20% of vacancies have no description. Run `/jobs-fetch` with enrichment before scoring for better accuracy.
