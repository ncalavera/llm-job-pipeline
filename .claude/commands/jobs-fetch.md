---
description: Fetch new vacancies from configured sources (ATS + job boards). Interactive mode — source selection, TTL cache, tier filtering, and optional auto-score pipeline.
---

# /jobs-fetch

Runs `scripts/fetch_vacancies.py` with the right flags after a short interactive dialog.

> **Job boards are opt-in.** By default no boards are fetched — only your tracked
> companies. Six free boards are built in; pick the ones matching the user's
> sectors and enable per run with `JOB_BOARDS`, e.g.
> `JOB_BOARDS=arbeitnow,remotive python3 scripts/fetch_vacancies.py` (or
> `JOB_BOARDS=all`). The board phases / `--boards-only` flag below only do
> anything when `JOB_BOARDS` opts the boards in.
>
> | Board id | Sector fit | Extra env |
> | --- | --- | --- |
> | `80k_hours` | EA / AI safety / policy | — |
> | `reliefweb` | humanitarian / development | — |
> | `arbeitnow` | European tech, visa sponsorship | `ARBEITNOW_VISA_ONLY=1` |
> | `remotive` | remote-first roles | `REMOTIVE_CATEGORIES=product,marketing` |
> | `weworkremotely` | remote product / business | `WWR_CATEGORIES=product,marketing` |
> | `hn_whoishiring` | startups (monthly HN thread, 30-day TTL) | — |
>
> Remotive asks API users for very few calls (max ~4/day) — the fetcher already
> makes one request per run (or one per category); do not loop it. The HN
> thread is monthly; its 30-day TTL stops daily refetches automatically.

## Step 0: Source Status Dashboard

Before fetching, display a live source status dashboard showing each company, when it was last fetched, vacancy count, and whether it is stale or due:

```bash
python3 -c "
import sys
from datetime import datetime, date
sys.path.insert(0, 'scripts')
from company_registry import COMPANIES
from config import JOB_BOARDS
from database_supabase import load_vacancies, get_conn

# ... (see scripts/fetch_vacancies.py for full source-status logic)
"
```

The dashboard groups companies by fetch strategy (free API vs paid Firecrawl), shows job board status, and lists stale/new sources.

## Step 1: Ask what to fetch

Present staged options — from cheapest to most expensive:

```
Four phases, safest to costliest — decide after each one.

  1. Free stale sources — Workday, Greenhouse, Lever, Ashby companies past TTL.
     Free, highest-value stale data.
  2. Free job boards — e.g. 80,000 Hours, ReliefWeb.
  3. New S-tier companies on Firecrawl — sources with status "never".
     Paid, but high priority.
  4. Remaining Firecrawl A/B/C + paid boards.

Start with phase 1? Or describe a different plan (specific companies,
a source type, "all at once", etc.).
```

Wait for user response. Default flow runs phases 1→4 with confirmation between each.

## Step 2: Fetch flags

Run in background and monitor progress. **Never launch all 100+ companies in one process** — stage by value and risk.

| Phase | Flags | Notes |
|-------|-------|-------|
| 1. Free stale | `--free-only --no-boards --force-all` | Free API sources, most valuable stale data |
| 2. Free boards | `--boards-only` (minus paid boards) | Free job boards |
| 3. New S-tier Firecrawl | `--strategy firecrawl_scrape --tier S --companies "..."` | Paid, high priority |
| 4. Rest of Firecrawl + paid boards | `--strategy firecrawl_scrape --tier A` → `--tier B` → ... | Lower priority |

Always use `python3 -u` (unbuffered stdout) so progress lines appear in real time:

```bash
python3 -u scripts/fetch_vacancies.py {FLAGS} 2>&1
```

**Single-company or targeted runs:**

| Selection | Flags |
|-----------|-------|
| Specific companies | `--companies "X,Y" --no-boards` |
| Specific strategy | `--strategy {type} --no-boards` |
| Tier filter | `--tier {N}` |
| Ignore TTL | `--force-all` |
| Auto-filter + score after fetch | `--auto-score --auto-score-limit N` |
| Regenerate dashboard only | `--report-only` |

## Fetch flags reference

| Flag | What it does |
| --- | --- |
| `--force-all` | Ignore TTL, fetch all companies |
| `--companies "A,B,C"` | Only the listed canonical names |
| `--tier S` | Only companies with tier S |
| `--no-boards` | Skip job boards |
| `--auto-score` | Run filter + score after fetch |
| `--auto-score-limit N` | How many vacancies to score (default 50) |
| `--report-only` | Regenerate `public/data.js` only, no fetch |
| `--free-only` | Only free-API strategies (no Firecrawl) |
| `--boards-only` | Only job boards, skip company ATS sources |

## Gone-from-source auto-archiving

When fetching directly from an ATS (Greenhouse, Lever, Ashby, Workable, Workday), the pipeline compares the live job list with what is already in the database. Vacancies that are no longer on the source are automatically archived with reason `gone_from_source`. This only applies to direct ATS fetches — board-sourced vacancies are not checked this way.

## JS-required marking

If a company page requires JavaScript rendering and the fetch returns nothing useful, the vacancy is tagged `js_required` for manual review rather than silently discarded.

## Step 3: Data quality cleanup

After fetching, run enrichment to clean up blind vacancies:

```bash
python3 scripts/enrich_blind_vacancies.py 2>&1
```

(Requires `FIRECRAWL_API_KEY` in your environment.)

This enriches vacancies that have a URL but no description (via Firecrawl), and deletes junk vacancies with no URL and no description.

## Step 4: Quality report

After fetching, show:
- Total new vacancies found
- Breakdown by organization (top 10)
- Description coverage percentage
- Any fetch errors

```bash
python3 -c "
import sys
from collections import Counter
from datetime import date
sys.path.insert(0, 'scripts')
from database_supabase import load_vacancies

vacancies = load_vacancies(include_inactive_companies=True)
today = date.today().isoformat()
# ... show totals, new today, scored/unscored, coverage
"
```

Note any new departments that the pipeline captured but are not yet in the `department_exclude` list for that company's `ats_config`.

## Step 5: S/A tier zero-vacancy alert

After every fetch, show companies at S/A tier that have zero vacancies in the database — these need manual verification:

```bash
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from database_supabase import load_vacancies
from company_registry import COMPANIES

# ... identify S/A tier companies with 0 vacancies
"
```

If the user confirms, open each careers page in the browser for manual inspection.

## After fetching

If new vacancies are few or zero — that is normal. The default TTL is 3–7 days. Use `--force-all` or `--companies "Name1,Name2"` to bypass the cooldown.

If a company's `fetch_error` keeps increasing, its careers page likely changed structure. Re-run `/jobs-add` for that company to re-detect the ATS.

**Does NOT auto-archive. Does NOT regenerate the dashboard.** Those are separate steps.

## Devex (cookie-authenticated)

Devex requires browser cookies. Check freshness and run scrape + import in one step:

```bash
# Check cookie file
COOKIE_FILE=~/Downloads/www.devex.com_cookies.txt
[ -f "$COOKIE_FILE" ] && echo "Found" || echo "MISSING — export from browser first"

# Scrape and import
python3 scripts/scrape_devex.py 2>&1 && python3 scripts/import_devex.py 2>&1
```

If the cookie file is missing, warn the user and skip Devex without failing the rest of the fetch.

## Common Issues

- **Devex cookies expired**: Scrape returns 403 or empty results. Re-export cookies from the browser using a cookie extension.
- **Firecrawl rate limit**: Large batches may hit the rate limit. The script handles retries, but you may need multiple runs.
- **Supabase connection timeout**: Long fetches may lose the DB connection. The script reconnects automatically — check for partial saves.
- **ATS API changes**: Greenhouse/Lever/Ashby occasionally change their API. If a source suddenly returns 0 vacancies, check the API response manually.
