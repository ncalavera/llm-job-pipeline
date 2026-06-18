---
description: Add a new company to the vacancy monitoring pipeline. Auto-detects the ATS (Greenhouse, Lever, Ashby, Workable, Workday), runs a test fetch, and adds the record to Supabase.
---

# /jobs-add

Adds a new company to the pipeline.

## Step 0: Collect inputs

Ask the user for (if not already provided):
1. **Careers page URL** — the company's jobs page
2. **Company name** — how it should appear in the dashboard (e.g. "Stripe", "Khan Academy")
3. **Tier** — 1 (Top Priority), 2 (Strong Fit), 3 (Good Options)

If the user provided these in their message, skip the question and proceed.

## Step 1: ATS Auto-Detection

Detect which ATS the company uses by probing known API patterns.

### 1a. Check page HTML for ATS signals

```bash
curl -s -L --max-time 10 "{CAREERS_URL}" | head -200
```

Look for these indicators in the HTML:
- `boards.greenhouse.io` or `greenhouse.io` → **Greenhouse**
- `jobs.lever.co` or `lever.co` → **Lever**
- `jobs.ashby.io` or `ashbyhq.com` → **Ashby**
- `apply.workable.com` or `workable.com` → **Workable**
- `myworkdayjobs.com` or `myworkdaysite.com` → **Workday**

### 1b. Extract slug from redirects

If the careers URL itself is an ATS URL, extract the slug directly:
- `https://boards.greenhouse.io/{SLUG}/` → slug = `{SLUG}`
- `https://jobs.lever.co/{SLUG}/` → slug = `{SLUG}`
- `https://jobs.ashby.io/{SLUG}/` → slug = `{SLUG}`
- `https://jobs-apply.workable.com/{SLUG}/` → slug = `{SLUG}`
- `https://{TENANT}.myworkdayjobs.com/{BOARD}/` → tenant = `{TENANT}`, board = `{BOARD}`

### 1c. Probe known API endpoints

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

### 1d. Determine result

Based on the probes, determine:
- **Detected ATS** (greenhouse / lever / ashby / workable / workday_api / firecrawl_scrape)
- **Slug** (for greenhouse/lever/ashby/workable) or **tenant + board** (for workday)
- **Confidence** (high = direct URL match, medium = API 200, low = guessed)

If detection is uncertain, show the user what was found and ask them to confirm or provide the correct slug.

**If no ATS detected → fallback to `firecrawl_scrape` strategy.**

## Step 2: Show detection result and confirm

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

## Step 3: Add to Supabase company table

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

## Step 4: Run test fetch

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

## Step 5: Merge into Supabase (if fetch succeeded)

If the test fetch returned vacancies, offer to merge them into the database:

```bash
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from company_registry import COMPANIES
from fetchers import fetch_greenhouse, fetch_lever, fetch_ashby, fetch_workable, fetch_firecrawl_scrape, fetch_workday_api
from database_supabase import merge_vacancies, update_source_tracking

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

new_count = merge_vacancies(org_name, config.get('tier', 2), jobs)
update_source_tracking(org_name, config.get('tier', 2), strategy, new_count)
print(f'Merged: {new_count} new vacancies added to Supabase')
"
```

## Step 6: Quality report

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
Ready for scoring! Run /jobs-score to evaluate {COMPANY_NAME} roles.
```

## If ATS is not detected

Options when a company uses a custom site without a known ATS:

- **Firecrawl scrape** — slower (5–10 s per page), requires `FIRECRAWL_API_KEY`, but works for most HTML pages.
- **RSS feed** — some companies publish vacancies as RSS. Find the feed URL, set `fetch_strategy = 'rss'` and `ats_config = {"feed_url": "..."}`.
- **Skip** — if the scraping cost outweighs the expected value, do not add the company.

## Aliases

If a company publishes under different names (e.g. "Wikimedia Foundation" and "Wikipedia"), add all variants to the `aliases` array:

```sql
UPDATE company SET aliases = ARRAY['Wikipedia', 'Wikipedia Foundation']
WHERE canonical_name = 'Wikimedia Foundation';
```

This is needed for cross-board deduplication.

## Important rules

- **Always confirm with the user** before adding to Supabase
- **Always run a test fetch** before merging into Supabase
- **Flag blind scoring risk** prominently if descriptions are missing
- **Never use firecrawl_scrape as first choice** — only if no ATS detected
- **Check for duplicates** before adding (check `company_registry.COMPANIES`)
- **Supabase `company` table is the single source of truth** — `company_registry.py` loads COMPANIES from it

## Common Issues

- **ATS detection fails**: Company uses a custom careers page with no known ATS. Fallback to `firecrawl_scrape` strategy and ask the user for the correct slug.
- **Wrong slug guess**: The API returns 404 for the guessed slug. Ask the user to check the careers page URL and provide the correct identifier.
- **Duplicate company**: `ensure_company()` finds an existing record. Check if the existing record needs updating rather than creating a new one.
- **Test fetch returns 0 vacancies**: The ATS endpoint may require an EU domain, a different slug, or the company simply has no open roles. Verify the careers page manually.
- **Firecrawl scrape returns empty**: The page may use heavy JS rendering. Try adding `wait_for` or `actions` parameters, or switch to a different strategy.
