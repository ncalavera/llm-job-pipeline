---
description: Interactive archival of low-scoring unseen vacancies. Shows score brackets, flags blind-scored borderlines, requires explicit confirmation, and supports restore.
---

# /jobs-archive

Helps clean the dashboard of vacancies that scored low but were not touched by auto-archive (or where scoring was unreliable in either direction).

The pipeline has an "Archive" tab in the dashboard showing vacancies with `status = 'archived'`. This command sets that status interactively after your review.

## Step 0: Load current state

```python
from config import LLM_SCORE_THRESHOLD
from database_supabase import load_vacancies, get_protected_ids

v = load_vacancies()
threshold = LLM_SCORE_THRESHOLD  # default = 20

protected_ids = get_protected_ids()

candidates = [
    (vid, vac) for vid, vac in v.items()
    if vid not in protected_ids
    and vac.get('llm_score') is not None
    and vac['llm_score'] < threshold
]
```

Show a preview:

```
ARCHIVE PREVIEW
============================================================
  Default threshold:  score < {threshold} (unseen only)
  Total DB:           {len(v)} vacancies

  Archive candidates: {len(candidates)} vacancies
    Score  0-9:       {len(b0):3d} vacancies (clearly irrelevant)
    Score 10-14:      {len(b10):3d} vacancies
    Score 15-19:      {len(borderline):3d} vacancies  <- borderline, review these

  After archive: {remaining} vacancies remain in DB
============================================================
```

Flag borderline vacancies that were scored without a description ([BLIND]):

```
  Top borderline (score 15-19):
    [18] ACME Corp — Field Coordinator [BLIND]
         Remote
```

If any blind-scored borderlines exist, warn the user before asking for confirmation.

## Step 1: Ask for confirmation

```
Archive {CANDIDATES} vacancies (score < {THRESHOLD}, unseen only)?
  1. Archive all
  2. Raise threshold to 25 (more vacancies)
  3. Lower threshold to 15 (fewer vacancies)
  4. Exclude blind-scored borderlines  <- RECOMMENDED if blind borderlines > 0
  5. Review past archives
  6. Cancel
```

Wait for user response before proceeding. If user picks "Raise/Lower threshold" — re-run Step 0 with the new value.

## Step 2: Execute archive

Run `archive_vacancies(threshold)` from `database_supabase.py`. For option 4 (exclude blind borderlines), skip vacancies in the 15–19 range that have no description.

After archiving, show: how many were archived and the current DB size.

## Step 3: Confirm result

```
ARCHIVE COMPLETE
=======================================================
  DB now: {total} vacancies ({unseen} unseen, {scored} scored)

  Next: /finish — regenerate dashboard, commit, push
=======================================================
```

## Restoring vacancies

If you archived something by mistake — nothing is deleted, `status = 'archived'` is all that changed.

Restore a single vacancy:

```bash
python3 scripts/vac.py mark <uuid> --status unseen
```

Restore multiple vacancies via SQL:

```sql
UPDATE vacancy SET status = 'unseen', status_updated_at = NOW()
WHERE id IN ('<uuid1>', '<uuid2>', ...);
```

The Archive tab in the dashboard always shows archived vacancies if you need to review them.

## Step 4: Review past archives (optional)

If user picks "Review past archives":

```bash
python3 -c "
import json
from pathlib import Path

archive_dir = Path('vacancies/jobs-archive')
archives = sorted(archive_dir.glob('archived_*.json'), reverse=True)

if not archives:
    print('No archives found.')
    exit()

for i, path in enumerate(archives):
    with open(path) as f:
        meta = json.load(f)
    ts = meta.get('archived_at', path.stem)[:16].replace('T', ' ')
    print(f'[{i+1}] {path.name}')
    print(f'     {meta[\"count\"]} vacancies | threshold {meta[\"threshold\"]} | {ts}')
"
```

Ask: browse a specific archive or restore vacancies?

To restore specific vacancies, update their `status` back to `unseen` in Supabase via `database_supabase.get_conn()`.

## When to run

- After each `/jobs-score` — check what landed in the lower brackets.
- Weekly — clean up accumulated noise.
- Before `/jobs-apply` — keep the liked list clean.

## Important rules

- **Never auto-archive** — always show preview and wait for explicit confirmation.
- **Flag blind borderlines** — vacancies scored without a description in the 15–19 range need special attention.
- **Default threshold is 20** — from `LLM_SCORE_THRESHOLD` in `config.py`.
- **Only archive unprotected vacancies** — never archive liked/passed/applied.
- **After archiving, suggest `/finish`** — to regenerate the dashboard and push.
