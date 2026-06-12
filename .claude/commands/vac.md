---
description: KISS-CLI for daily vacancy triage from the terminal — list / show / mark / open / companies over Supabase, without opening the dashboard.
---

# /vac

Thin CLI on top of Supabase for everyday triage. Use it when you do not want to open the dashboard or are on a server without a browser.

## Commands

All commands run against the project directory. The script loads `database_supabase.py` automatically.

```bash
python3 scripts/vac.py <command>
```

If a `vac` alias is configured in your shell — you can write `vac <command>` directly.

| Goal | Command |
| --- | --- |
| Top 20 by score | `python3 scripts/vac.py list` |
| Only liked | `python3 scripts/vac.py list --status liked` |
| Only from a specific company | `python3 scripts/vac.py list --company "GiveDirectly"` |
| Sort by date | `python3 scripts/vac.py list --sort last_seen` |
| Filter by geo bucket | `python3 scripts/vac.py list --geo uk` |
| Full details | `python3 scripts/vac.py show <uuid>` |
| Change status | `python3 scripts/vac.py mark <uuid> --status liked` |
| Open URL in browser | `python3 scripts/vac.py open <uuid>` |
| Company summary | `python3 scripts/vac.py companies` |

## Flags

- `--limit N` — number of rows to show (default 20 for `list`).
- `--status liked,unseen` — comma-separated list of statuses.
- `--no-website` — companies without a `careers_url` (useful for `/add-source`).
- `--geo {bucket}` — filter by geographic bucket. Accepted values: `uk`, `germany`, `europe`, `us`, `cis`, `other`, `unknown`. Buckets are assigned by `geo.py` based on vacancy location data.

## Supported statuses

`unseen`, `liked`, `passed`, `to_apply`, `to_research`, `to_network`, `skipped`, `applied`, `archived`

The `archived` status covers vacancies moved to the Archive tab (low-scoring or gone from source) — they remain in the database but are hidden from the main catalog view.

## Typical workflows

**Morning review of liked vacancies:**
```bash
python3 scripts/vac.py list --status liked
python3 scripts/vac.py show <id>     # read the most interesting one
python3 scripts/vac.py open <id>     # check the original page
python3 scripts/vac.py mark <id> to_apply
```

**Scan new high-scoring unseen:**
```bash
python3 scripts/vac.py list --status unseen --min-score 70 --sort score
```

**Find all vacancies for a company:**
```bash
python3 scripts/vac.py list --org FundraiseUp --include-candidates
```

**Filter by UK-only locations:**
```bash
python3 scripts/vac.py list --geo uk
```

## When NOT to use

- Bulk operations (>10 vacancies) — use the dashboard.
- Triage with long notes — use `/triage`, which writes to `vacancy.triage` JSONB.
- Full-text search in descriptions — no full-text search here; use the Supabase SQL Editor.

## Architecture

- Code: `scripts/vac.py` (no dependencies beyond psycopg2 and stdlib).
- Database: Supabase (shared with `/fetch`, `/score`, `/triage`). No local caches.
- DAL: `database_supabase.py` — `load_vacancies()`, `update_vacancy_status()`.
