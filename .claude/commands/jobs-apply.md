---
description: Deep review of liked vacancies. Structured interview per vacancy, apply/research/network/skip verdicts, issue tracking integration, and pipeline calibration feedback.
---

# /jobs-apply

When LLM scoring has surfaced liked vacancies — `/jobs-apply` walks through them one by one and helps make an informed decision.

## Steps

1. Load vacancies with `status = 'liked'` via `scripts/triage.py:get_liked_vacancies()`.

2. Filter out vacancies past their deadline. Show closed vacancies briefly, then proceed with open ones only.

3. Group by company. If a company has 5+ open positions, ask: review all or just the top-scoring ones.

4. For each vacancy — structured interview (one question at a time):

   **Q1: First impression**
   - Interested / Uncertain / Skip

   **Q2: Requirements match**
   - Yes / Partially / No
   - If "No" — suggest Skip (hard requirements are critical).

   **Q2.5: Growth and complexity**
   - The next role should offer tasks complex enough to produce real growth — mission fit alone is not sufficient.
   - Options: Yes, it's new/building and I'll grow / Mixed, partly growth partly maintenance / No, mostly maintenance
   - If "No" — suggest Skip.

   **Q3: Verdict**
   - `to_apply` — will apply
   - `to_research` — needs more investigation before deciding
   - `to_network` — long-term target, need a warm contact first
   - `skipped` — decided not to apply

   **Q4: Verdict-specific follow-up**
   - Apply: CV adaptation notes, preparation steps, deadline
   - Skip: reason (for scoring calibration)
   - Research: what to investigate, deadline
   - Network: who could be a contact

5. Save the decision immediately after each vacancy (do not defer):
   ```python
   triage.update_status(vacancy_id, 'to_apply')   # one vacancy
   # or, for several at once:
   # triage.batch_update_statuses_and_commit({vac_id_1: 'to_apply', vac_id_2: 'skipped'})
   ```
   Keep free-text notes in `triage/session-notes-{date}.md` via the Edit/Write tool after every single vacancy.

6. Save incrementally after every company (Step 1d) — prevents data loss if the session is interrupted.

7. If `to_apply` — create a tracking issue in your issue tracker:
   - Title: `{COMPANY} — {ROLE} (score {SCORE})`
   - Include: location, deadline, CV notes, preparation checklist, apply link
   - One issue per company (multiple roles in one issue), not one per vacancy.

8. After all companies are reviewed — update vacancy statuses in Supabase:

   | Verdict | Status |
   |---------|--------|
   | apply | `to_apply` |
   | skip | `skipped` |
   | research | `to_research` |
   | network | `to_network` |

## Calibration loop

At the end of the session, ask three calibration questions (one at a time):

1. **Scoring accuracy** — were any LLM scores significantly off (too high or too low)?
2. **Pipeline calibration** — any new blacklist patterns to add? Companies to stop monitoring?
3. **Strategy insights** — patterns, direction shifts, new priorities?

If the user provides feedback:
- Proposed scoring prompt changes → show exact diff, ask for confirmation before editing.
- New blacklist patterns → show them, ask for confirmation before editing `scripts/config.py`.
- Strategy updates → propose updating `strategy.md`, ask for confirmation.

## When to run

- After each `/jobs-score` session — to review newly liked vacancies.
- Before `/jobs-finish` — to clean up the liked list.

## Files

- `scripts/triage.py` — helpers (load, group, persist).
- `vacancy.triage` JSONB column — stores notes and decision metadata.
- `triage/jobs-apply.json` — session history (metadata only; Supabase `vacancy.status` is the source of truth).
- `triage/session-notes-{date}.md` — running Markdown notes for the session.

## Auto-archive note

The auto-archive by score threshold is currently paused under pure-fit scoring — do not archive automatically from within `/jobs-apply`. Use `/jobs-archive` explicitly after reviewing.

## Important rules

- **One question at a time** — never ask multiple questions at once.
- **Save after every vacancy** — append to `triage/session-notes-{date}.md` immediately, never defer.
- **Save after every company** — incremental DB save is mandatory, not optional.
- **1 tracker issue = 1 company** — not one per vacancy, to avoid tracker spam.
- **Calibration changes need confirmation** — never auto-edit the scoring prompt or blacklist.
- **Supabase `vacancy.status` is the source of truth** — `triage.json` stores metadata only.
