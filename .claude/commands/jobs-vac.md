---
description: KISS-CLI for daily vacancy triage from the terminal — list / show / mark / open / companies on the active backend, without opening the dashboard.
---

# /jobs-vac

Thin triage CLI that runs against whatever backend is active — local SQLite by
default (simple mode), or Postgres/Supabase when `SUPABASE_DB_URL` is set. Use
it when you do not want to open the dashboard or are on a server without a
browser.

## Commands

All commands run against the project directory. The script loads `database_supabase.py` automatically.

```bash
python3 scripts/vac.py <command>
```

If a `vac` alias is configured in your shell — you can write `vac <command>` directly.

| Goal | Command |
| --- | --- |
| Top by score | `python3 scripts/vac.py list` |
| Only liked | `python3 scripts/vac.py list --status liked` |
| Only from a specific company | `python3 scripts/vac.py list --org "GiveDirectly"` |
| Sort by date | `python3 scripts/vac.py list --sort recent` |
| Filter by geo bucket | `python3 scripts/vac.py list --geo uk` |
| Full details | `python3 scripts/vac.py show <uuid>` |
| Change status | `python3 scripts/vac.py mark <uuid> liked` |
| Open URL in browser | `python3 scripts/vac.py open <uuid>` |
| Company summary | `python3 scripts/vac.py companies` |

## Flags

`list` flags:

- `--limit N` — number of rows to show.
- `--status <name>` — filter by a single status (e.g. `liked`, `unseen`).
- `--min-score N` — minimum LLM score.
- `--tier S|A|B|C` — filter by company tier.
- `--org "Name"` — filter by company-name substring.
- `--sort score|recent|company` — sort order (default `score`).
- `--include-candidates` — also show vacancies from non-approved companies.
- `--geo {bucket}` — filter by geographic bucket. Accepted values: `uk`, `germany`, `europe`, `us`, `cis`, `other`, `unknown`. Buckets are assigned by `geo.py` based on vacancy location data.

`mark` takes the status as a **positional** argument: `mark <uuid> <status>`.

`companies` takes `--status active|candidate|inactive` and `--limit N`.

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
- Triage with long notes — use `/jobs-apply`, which writes to `vacancy.triage` JSONB.
- Full-text search in descriptions — there is no full-text search here. Use the
  dashboard's search box, or query the database directly (SQLite: `sqlite3
  data/jobsearch.db`; Supabase: the SQL Editor).

## Architecture

- Code: `scripts/vac.py` (stdlib only; no direct DB driver — it goes through the DAL).
- Database: the active backend — local SQLite by default, Supabase when
  `SUPABASE_DB_URL` is set. Shared with `/jobs-fetch`, `/jobs-score`,
  `/jobs-apply`. No local caches.
- DAL: `database_supabase.py` — `load_vacancies()`, `update_vacancy_status()`.
