---
description: The one daily command. Fetches new vacancies, filters junk, scores the new ones, shows the top fresh matches in chat, and captures your like/pass verdict straight into the database.
---

# /jobs

The everyday loop for the easy install path. Run it once a day. It does
fetch -> filter -> score -> show top new vacancies -> capture verdicts, then
refreshes the local dashboard data. Works on the local SQLite database with no
Supabase and no Vercel.

For a visual review instead of chat, run `python3 scripts/dashboard_local.py`.

## Step 1: Fetch new vacancies

Fetch from all monitored companies (TTL cooldown applies, so this is cheap to
run daily):

```bash
source ~/.zshrc 2>/dev/null && python3 -u scripts/fetch_vacancies.py 2>&1
```

Job boards are off by default and stay off here. If the user searches in
EA / AI-safety or humanitarian sectors and wants them, prefix the command with
`JOB_BOARDS=80k_hours,reliefweb` (or `JOB_BOARDS=all`).

Note the "FETCH COMPLETE: N new vacancies" line. If N is 0, tell the user there
is nothing new today and skip to Step 5 (still refresh the dashboard).

## Step 2: Filter

Run the quality gate to drop junk and obvious non-fits before spending scoring
budget:

```bash
source ~/.zshrc 2>/dev/null && python3 scripts/filter_vacancies.py 2>&1
```

## Step 3: Score the new vacancies

Score the unscored vacancies. Use the standard scoring flow (`/jobs-score`): one Opus
subagent per vacancy, save results via `score_vacancies.py --save`. Keep the
batch bounded (e.g. the newest 20-30) so a daily run stays fast.

## Step 4: Show top new matches + capture verdicts

List the freshly scored, still-unseen vacancies, highest score first:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from database_supabase import load_vacancies
vacs = [v for v in load_vacancies(include_candidate_companies=True).values()
        if v.get('status') == 'unseen' and v.get('llm_score') is not None]
vacs.sort(key=lambda v: -(v.get('llm_score') or 0))
for v in vacs[:10]:
    loc = ', '.join(filter(None, [(v['locations'][0].get('city') if v.get('locations') else None),
                                  (v['locations'][0].get('work_mode') if v.get('locations') else None)]))
    print(f\"{v['llm_score']:>3} | {v['org']} — {v['title']}  [{loc}]\")
    print(f\"      {v['id']}\")
    if v.get('llm_summary'): print(f\"      {v['llm_summary'][:200]}\")
    print()
"
```

Walk the user through the top matches. For each, ask like / pass / skip (or let
them say "like the first three"). Write each verdict immediately:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from database_supabase import update_vacancy_status
from db_conn import get_conn
update_vacancy_status('{VACANCY_ID}', '{STATUS}')   # liked | passed | skipped | to_apply
get_conn().commit()
print('marked {VACANCY_ID} -> {STATUS}')
"
```

Statuses: `liked`, `passed`, `skipped`, `to_apply`, `to_research`, `to_network`,
`applied`. Default the quick triage to `liked` / `passed`.

## Step 5: Refresh the dashboard data

Regenerate `public/data.js` so the local dashboard reflects today's run:

```bash
source ~/.zshrc 2>/dev/null && python3 scripts/fetch_vacancies.py --report-only 2>&1
```

If the local dashboard server is already running, the user just refreshes the
browser. Otherwise mention they can launch it with
`python3 scripts/dashboard_local.py`.

## Done

Summarize: N new fetched, M scored, the verdicts captured (liked/passed counts),
and how many liked vacancies are now waiting for a deeper look (`/jobs-apply`).
