---
description: Add a source to the pipeline in one of three modes — company (auto-detect ATS, default), board (enable a built-in job board via JOB_BOARDS), or vacancy/job (hand-add a single role that auto-scores in /jobs-new).
---

# /jobs-add

Adds a source to the pipeline. The first argument selects the mode:

| First arg | Mode | What it does |
| --- | --- | --- |
| `board` | **Board** | Enable a built-in job board for fetching (persisted; survives sessions). |
| `vacancy` / `job` | **Single vacancy** | Hand-add ONE vacancy; it lands `unseen` and is auto-scored by `/jobs-new`. |
| _(anything else)_ / `company` | **Company** (default) | Auto-detect a company's ATS and add it to Supabase. |

## Step 0: Route on the first argument

Inspect the first argument the user passed.

- If it is exactly `board` → go to **Mode B (board)**.
- If it is exactly `vacancy` or `job` → go to **Mode C (single vacancy)**.
- Otherwise → go to **Mode A (company)**, treating the argument as the company name / careers URL.

**Disambiguation (keyword collision).** The keywords `board`, `vacancy`, and
`job` can also be real company names ("Board Intelligence", "JobTeaser", "Jobandtalent").
If the first argument is one of those keywords **but the surrounding message
suggests a real company** (e.g. a careers URL is present, or the user typed
"add Board Intelligence"), ask **once** which they meant before routing:

```
"board" can mean two things here:
  1. Enable a built-in job board (Mode B)
  2. Add the company "Board Intelligence" (Mode A)
Which did you mean?
```

Do not guess silently. If there is no collision signal, route by the keyword.

---

## Mode B — enable a job board

Boards are **built into** `config/defaults.toml [boards.*]` and loaded by
`config._ALL_JOB_BOARDS`. Enabling one **persists**: it sets the `board.enabled`
flag in the DB (`database_supabase.set_board_enabled`, surfaced by
`scripts/sources.py`), so the board participates in every future `/jobs-new`
with no env var and no reminder. The `JOB_BOARDS` env var / `--boards` flag
stays a **manual override applied ON TOP** of the persisted set for a single run
(`run_daily.py` unions the two). You enable a board by persisting the flag and
running one seed fetch.

### B0. Out-of-scope guard

Adding a **brand-new** board (one not already in the 6 built-ins) is **out of
scope** for this command — it needs a new `[boards.<id>]` block in
`config/defaults.toml` plus a matching `fetch_*_board` strategy. If the user
wants a board that is not listed below, say so plainly and stop.

### B1. Present the built-in boards

List the boards from `config._ALL_JOB_BOARDS` so the user can pick by id:

```bash
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from config import _ALL_JOB_BOARDS
for bid in _ALL_JOB_BOARDS:
    print(bid)
"
```

Present them with their sector fit and any extra env knobs:

| Board id | Sector fit | Extra env |
| --- | --- | --- |
| `80k_hours` | EA / AI safety / policy | — |
| `reliefweb` | humanitarian / development | — |
| `arbeitnow` | European tech, visa sponsorship | `ARBEITNOW_VISA_ONLY=1` |
| `remotive` | remote-first roles | `REMOTIVE_CATEGORIES=product,marketing` |
| `weworkremotely` | remote product / business | `WWR_CATEGORIES=product,marketing` |
| `hn_whoishiring` | startups (monthly HN thread, 30-day TTL) | — |

Ask the user which board id(s) to enable (comma-separated). If a board they name
is not in `_ALL_JOB_BOARDS`, fall back to the B0 out-of-scope guard.

### B2. Persist the board(s), then run a seed fetch

First **persist** each chosen id so it sticks across sessions:

```bash
python3 scripts/sources.py enable-board <id>   # once per board id
```

Then run a free, boards-only seed fetch so the roles land now (the `JOB_BOARDS`
here is a one-off override for this immediate fetch; persistence already
happened above):

```bash
JOB_BOARDS=<ids> python3 -u scripts/fetch_vacancies.py --boards-only --free-only 2>&1
```

This routes through `save_board_vacancies` → rows land `status='unseen'` → they
will be auto-scored on the next `/jobs-new`. The seed fetch also runs
`sync_boards`, backfilling each board's catalog metadata (name/strategy/ttl).

Show the result: total vacancies pulled per board, description coverage, any
fetch errors.

> Remotive asks API users for very few calls (max ~4/day) and the HN thread is
> monthly (30-day TTL) — one fetch per run is enough; do not loop it.

### B3. Confirm it will keep running

The board is now persisted — it fetches on **every** `/jobs-new` automatically,
no env var to remember. Tell the user:

```
Board(s) enabled (persisted): <ids>

They now fetch on every /jobs-new automatically — nothing to remember.

See everything you have enabled (boards + companies):
   python3 scripts/sources.py
Stop one:
   python3 scripts/sources.py disable-board <id>

(JOB_BOARDS / --boards still works as a one-off override ON TOP of this set —
e.g. to try a board for a single run without persisting it.)
```

---

## Mode C — add a single vacancy by hand

Add ONE vacancy directly, reusing `save_vacancies(org, tier, [job])`
(`scripts/database_supabase.py`). That function resolves the canonical org, runs
the quality gate, dedups on `dedup_hash = md5(lower("org|title"))`, merges
locations, and inserts the row as `status='unseen'` → auto-scored by `/jobs-new`.

### C1. Collect inputs

Ask for (skip any the user already provided):

**Required**
1. **org** — company name (e.g. "Stripe")
2. **title** — the role title
3. **url** — the vacancy URL

**Strongly recommended**
4. **description** — paste the full job description (without it, scoring is blind / may be dropped)

**Optional**
- **location** — e.g. "Remote (EU)", "Berlin, Germany"
- **snippet** — a short summary line
- **compensation** — e.g. "€80–100k"
- **deadline** — application deadline
- **department** — e.g. "Engineering"

Also ask for **tier** (1 = Top Priority, 2 = Strong Fit, 3 = Good Options) — default 2.

### C2. Build the job dict and insert

```bash
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from database_supabase import save_vacancies

org   = '{ORG}'
tier  = {TIER}
job = {
    'title':            '{TITLE}',
    'url':              '{URL}',
    'full_description': '''{DESCRIPTION}''',
    'snippet':          '{SNIPPET}',
    'location':         '{LOCATION}',
    'compensation':     '{COMPENSATION}',
    'deadline':         '{DEADLINE}',
    'department':       '{DEPARTMENT}',
}
# Drop empty optional keys so they don't overwrite anything.
job = {k: v for k, v in job.items() if v not in ('', None)}

new_count = save_vacancies(org, tier, [job])
print(f'new_count={new_count}')
"
```

Report `new_count`:
- **`new_count == 1`** → inserted. The row is `status='unseen'` and will be scored
  automatically on the next `/jobs-new`. Done.
- **`new_count == 0` and the row already existed** → it was a duplicate (same
  `org|title`); `save_vacancies` merged the new location/url/description into the
  existing row instead of inserting. Tell the user it was de-duplicated.
- **`new_count == 0` and it was NOT a duplicate** → the **quality gate silently
  dropped it** (see C4).

### C3. Unknown-company handling

`save_vacancies` calls `ensure_company(org, status=_auto_discovery_status())`
when the org is not already in the registry — this creates a **fetch-less
stub company** (no `fetch_strategy`, no `ats_slug`), landing `candidate` by
default (config `auto_discovery_status`, the SAME gate every other
auto-discovered company goes through — see database_supabase.py). Warn the
user:

```
"{ORG}" was not a tracked company — I created a stub for it so the vacancy
could be saved. It's a candidate pending review (same gate as any
board-discovered company); the single vacancy you added still scores via the
candidate-rescue path on the next /jobs-new. WARNING: this stub has no ATS
config, so /jobs-new will NOT auto-fetch new roles from {ORG}.
```

Then offer two follow-ups:
1. **Configure the ATS** so future roles auto-fetch → route to **Mode A
   (company)** for `{ORG}` (auto-detect + set `fetch_strategy`/`ats_slug`).
2. **Approve it now** — set `status='active'` directly if you already know you
   want this company (an explicit yes, skipping the review gate on purpose).
   No future auto-fetch either way without step 1.

If the org already exists, skip this step.

### C4. Silent quality-gate drop (`new_count == 0`, not a dup)

The gate inside `save_vacancies` can drop a row for: blacklisted title words
(`filters.title_words_blacklisted`), content junk (`filters.is_content_junk`), or
too little content (`filters.has_enough_content` — needs ≥50 chars of
description/snippet **or** a non-empty URL). Since the URL is required here,
`has_enough_content` usually passes, so a silent drop most often means a thin
description that tripped a content check.

Explain which check likely fired, then offer **Firecrawl enrichment** to fetch a
real description from the URL before re-inserting:

```
The vacancy was not inserted — the quality gate dropped it (likely thin/empty
description). Options:
  1. Enrich with Firecrawl — scrape {URL} for a full description, then retry.
  2. Paste a fuller description and retry.
  3. Skip.
```

If the user picks Firecrawl, scrape the URL (requires `FIRECRAWL_API_KEY`), put
the result in `full_description`, and re-run C2.

---

## Mode A — add a company (default)

Adds a new company to the pipeline.

### Step 0: Collect inputs

Ask the user for (if not already provided):
1. **Careers page URL** — the company's jobs page
2. **Company name** — how it should appear in the dashboard (e.g. "Stripe", "Khan Academy")
3. **Tier** — 1 (Top Priority), 2 (Strong Fit), 3 (Good Options)

If the user provided these in their message, skip the question and proceed.

### Step 1: ATS Auto-Detection

Detect which ATS the company uses by probing known API patterns.

#### 1a. Check page HTML for ATS signals

```bash
curl -s -L --max-time 10 "{CAREERS_URL}" | head -200
```

Look for these indicators in the HTML:
- `boards.greenhouse.io` or `greenhouse.io` → **Greenhouse**
- `jobs.lever.co` or `lever.co` → **Lever**
- `jobs.ashby.io` or `ashbyhq.com` → **Ashby**
- `apply.workable.com` or `workable.com` → **Workable**
- `myworkdayjobs.com` or `myworkdaysite.com` → **Workday**

#### 1b. Extract slug from redirects

If the careers URL itself is an ATS URL, extract the slug directly:
- `https://boards.greenhouse.io/{SLUG}/` → slug = `{SLUG}`
- `https://jobs.lever.co/{SLUG}/` → slug = `{SLUG}`
- `https://jobs.ashby.io/{SLUG}/` → slug = `{SLUG}`
- `https://jobs-apply.workable.com/{SLUG}/` → slug = `{SLUG}`
- `https://{TENANT}.myworkdayjobs.com/{BOARD}/` → tenant = `{TENANT}`, board = `{BOARD}`

#### 1c. Probe known API endpoints

If the slug is uncertain, try common guesses (company name lowercased, no spaces):

```bash
SLUG_GUESS=$(echo "{COMPANY_NAME}" | tr '[:upper:]' '[:lower:]' | tr -d ' -')

# Greenhouse probe
curl -s --max-time 8 "https://boards-api.greenhouse.io/v1/boards/${SLUG_GUESS}/jobs" \
  -w "\n%{http_code}" | tail -1

# Lever probe
curl -s --max-time 8 "https://api.lever.co/v0/postings/${SLUG_GUESS}?mode=json" \
  -w "\n%{http_code}" | tail -1

# Ashby probe
curl -s --max-time 8 \
  -X POST "https://api.ashbyhq.com/posting-api/job-board/${SLUG_GUESS}" \
  -H "Content-Type: application/json" -d '{}' \
  -w "\n%{http_code}" | tail -1

# Workable probe
curl -s --max-time 8 "https://jobs-apply.workable.com/api/v3/accounts/${SLUG_GUESS}/jobs" \
  -w "\n%{http_code}" | tail -1
```

HTTP 200 = ATS confirmed. HTTP 404 = wrong slug or not that ATS.

#### 1d. Determine result

Based on the probes, determine:
- **Detected ATS** (greenhouse / lever / ashby / workable / workday_api / firecrawl_scrape)
- **Slug** (for greenhouse/lever/ashby/workable) or **tenant + board** (for workday)
- **Confidence** (high = direct URL match, medium = API 200, low = guessed)

If detection is uncertain, show the user what was found and ask them to confirm or provide the correct slug.

**If no ATS detected → fallback to `firecrawl_scrape` strategy.**

### Step 2: Show detection result and confirm

Present to the user:

```
ATS Detection Result for {COMPANY_NAME}:
   Detected:     {ATS_NAME}
   Slug/tenant:  {SLUG_OR_TENANT}
   Strategy:     {STRATEGY}
   Confidence:   {HIGH/MEDIUM/LOW}

Record that will be added to the company table:
   name:          {COMPANY_NAME}
   fetch_strategy:{STRATEGY}
   ats_slug:      {SLUG}
   ats_config:    {ATS_CONFIG_JSON}
   careers_url:   {CAREERS_URL}
   website:       {WEBSITE}
```

Ask: "Does this look correct? Should I add it and run a test fetch?"

Wait for confirmation before adding to Supabase.

### Step 3: Add to Supabase company table

**Check for duplicates first:**

```bash
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from company_registry import COMPANIES
org = '{COMPANY_NAME}'
if org in COMPANIES:
    print(f'DUPLICATE: {org} already exists in company registry')
else:
    print(f'OK: {org} not found — safe to add')
"
```

If already exists, stop and tell the user.

**Add new company record:**

```bash
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from database_supabase import ensure_company, get_conn

company_id = ensure_company('{COMPANY_NAME}', status='active')
print(f'Company ID: {company_id}')

conn = get_conn()
cur = conn.cursor()
cur.execute('''
    UPDATE company
    SET fetch_strategy = %s,
        ats_slug = %s,
        careers_url = %s,
        ats_config = %s,
        website = %s
    WHERE id = %s
''', ('{STRATEGY}', '{SLUG}', '{CAREERS_URL}', '{ATS_CONFIG_JSON}', '{WEBSITE}', company_id))
conn.commit()
cur.close()
print(f'Company {company_id} metadata updated')
"
```

**ATS Config JSON** format by strategy:
- **Greenhouse** (with EU endpoint): `{"eu": true}`
- **Workday**: `{"tenant": "...", "board": "...", "base_url": "https://..."}`
- **Firecrawl** (url differs from careers_url): `{"url": "..."}`
- **UNOPS**: `{"url": "...", "title_blacklist": [...]}`
- **All others**: empty string (no ATS config needed)

### Step 4: Run test fetch

Run a targeted fetch for just this company:

```bash
python3 scripts/fetch_vacancies.py --companies "{COMPANY_NAME}"
```

Show results:
- Total vacancies fetched
- Breakdown: full description / snippet only / no description
- Sample titles (first 5) with location and department

If 0 vacancies returned — ask the user to verify the ATS config manually.

**Blind scoring risk:** if vacancies have no description, flag this prominently:

```
WARNING: {N} vacancies have NO description — blind scoring risk
Options:
  1. Score as-is (fast, may be inaccurate for these roles)
  2. Enrich with Firecrawl before scoring
  3. Switch to firecrawl_scrape strategy (uses Firecrawl credits)
```

### Step 5: Merge into Supabase (if fetch succeeded)

If the test fetch returned vacancies, offer to merge them into the database:

```bash
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from company_registry import COMPANIES
from fetchers import fetch_greenhouse, fetch_lever, fetch_ashby, fetch_workable, fetch_firecrawl_scrape, fetch_workday_api
from database_supabase import save_vacancies, update_source_tracking

org_name = '{COMPANY_NAME}'
config = COMPANIES[org_name]
strategy = config['strategy']

jobs = []
if strategy == 'greenhouse':
    jobs = fetch_greenhouse(org_name, config['slug'], eu=config.get('eu', False))
elif strategy == 'lever':
    jobs = fetch_lever(org_name, config['slug'])
elif strategy == 'ashby':
    jobs = fetch_ashby(org_name, config['slug'])
elif strategy == 'workable':
    jobs = fetch_workable(org_name, config['slug'])
elif strategy == 'workday_api':
    jobs = fetch_workday_api(org_name, config['tenant'], config['board'], config['base_url'])
elif strategy == 'firecrawl_scrape':
    jobs = fetch_firecrawl_scrape(org_name, config['url'])

new_count = save_vacancies(org_name, config.get('tier', 2), jobs)
update_source_tracking(org_name, config.get('tier', 2), strategy, new_count)
print(f'Merged: {new_count} new vacancies added to Supabase')
"
```

### Step 6: Quality report

Present the final summary:

```
Source added: {COMPANY_NAME} (Tier {TIER})
   Strategy: {STRATEGY}
   Slug:     {SLUG}

Test fetch results:
   Total vacancies:         N
   With full description:   X  (excellent — accurate scoring)
   Snippet only:            Y  (acceptable — moderate scoring)
   No description:          Z  (blind scoring risk)

{If all good:}
Ready for scoring! Run /jobs-new to fetch, filter, and score {COMPANY_NAME} roles.
```

### If ATS is not detected

Options when a company uses a custom site without a known ATS:

- **Firecrawl scrape** — slower (5–10 s per page), requires `FIRECRAWL_API_KEY`, but works for most HTML pages.
- **RSS feed** — some companies publish vacancies as RSS. Find the feed URL, set `fetch_strategy = 'rss'` and `ats_config = {"feed_url": "..."}`.
- **Skip** — if the scraping cost outweighs the expected value, do not add the company.

### Aliases

If a company publishes under different names (e.g. "Wikimedia Foundation" and "Wikipedia"), add all variants to the `aliases` array:

```sql
UPDATE company SET aliases = ARRAY['Wikipedia', 'Wikipedia Foundation']
WHERE canonical_name = 'Wikimedia Foundation';
```

This is needed for cross-board deduplication.

### Important rules (company mode)

- **Always confirm with the user** before adding to Supabase
- **Always run a test fetch** before merging into Supabase
- **Flag blind scoring risk** prominently if descriptions are missing
- **Never use firecrawl_scrape as first choice** — only if no ATS detected
- **Check for duplicates** before adding (check `company_registry.COMPANIES`)
- **Supabase `company` table is the single source of truth** — `company_registry.py` loads COMPANIES from it

---

## Common Issues

- **Ambiguous first arg**: `board`/`vacancy`/`job` collides with a real company
  name → ask once which mode (Step 0 disambiguation), do not guess.
- **Board not built in (Mode B)**: the requested board has no `[boards.<id>]`
  block → out of scope; needs a `defaults.toml` block + `fetch_*_board` strategy.
- **Single vacancy silently dropped (Mode C)**: `new_count == 0` and not a dup →
  quality gate dropped it (thin description / blacklisted title / content junk).
  Offer Firecrawl enrichment or a fuller description.
- **Unknown company on hand-add (Mode C)**: `ensure_company` made a fetch-less
  stub → warn that `/jobs-new` won't auto-fetch it; offer to configure the ATS
  (Mode A) or just leave it active.
- **ATS detection fails (Mode A)**: Company uses a custom careers page with no known ATS. Fallback to `firecrawl_scrape` strategy and ask the user for the correct slug.
- **Wrong slug guess (Mode A)**: The API returns 404 for the guessed slug. Ask the user to check the careers page URL and provide the correct identifier.
- **Duplicate company (Mode A)**: `ensure_company()` finds an existing record. Check if the existing record needs updating rather than creating a new one.
- **Test fetch returns 0 vacancies (Mode A)**: The ATS endpoint may require an EU domain, a different slug, or the company simply has no open roles. Verify the careers page manually.
- **Firecrawl scrape returns empty**: The page may use heavy JS rendering. Try adding `wait_for` or `actions` parameters, or switch to a different strategy.
