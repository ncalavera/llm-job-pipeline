---
description: First-run setup for the easy (no-Supabase) install path. Checks the user profile, discovers 10-15 starter companies matching the user's field and geography, validates each ATS, inserts them, runs the first fetch/jobs-filter/jobs-score, and launches the local dashboard.
---

# /jobs-start

One-time onboarding for a fresh clone. Gets a non-technical user from empty
database to a working dashboard with real, scored vacancies — no Supabase, no
Vercel, no SQL editor. Everything runs locally on SQLite.

Run `/jobs` for the daily loop after this.

## Step 0: Confirm the backend

The pipeline uses a local SQLite file when `SUPABASE_DB_URL` is not set. Confirm
which mode we are in and that the database is reachable (it auto-creates on
first connection):

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from db_backend import IS_SQLITE, sqlite_db_path
if IS_SQLITE:
    print('Backend: local SQLite ->', sqlite_db_path())
else:
    print('Backend: Supabase (SUPABASE_DB_URL is set)')
from db_conn import get_conn
get_conn(); print('Connection OK')
"
```

If this is Supabase mode, tell the user `/jobs-start` still works but they likely
want the hardcore path; otherwise continue.

## Step 1: Check the user profile

The scoring prompts need `config/user_profile.md`. If it is missing, copy the
example and ask the user to fill it in (at minimum: field/role, target
locations, seniority, visa status):

```bash
test -f config/user_profile.md && echo "profile exists" || cp config/user_profile.example.md config/user_profile.md
```

Read `config/user_profile.md`. If it still contains the example placeholders
(e.g. "Jane Doe", "Example Foundation"), ask the user to describe themselves in
a few sentences (current role, target roles, target locations/geography, visa
status) and write those answers into the relevant `## SECTION` blocks before
continuing. Do NOT invent profile content.

Extract two things from the profile for the next step:
- **Field / target roles** (e.g. "programme management, operations, social impact")
- **Geography** (e.g. "Berlin, London, remote-EU")

## Step 2: Discover starter companies (web search)

Goal: 10-15 real organisations that (a) match the user's field and geography and
(b) are likely to be hiring. Use web search. Prefer mission-aligned employers
the user would actually want, not staffing agencies or aggregators.

For each candidate you need a **careers page URL** and a guess at its **ATS
slug** (the company's handle in the ATS URL — usually a lowercased, no-spaces
form of the name). Validate by probing the public ATS APIs **directly** — this
needs nothing in the database yet and no Firecrawl, just `curl`. Try the slug
against each ATS until one returns jobs:

```bash
SLUG="acmefoundation"   # your guess; try a couple of variants if the first misses

# Greenhouse (also try the EU host: boards-api.eu.greenhouse.io)
curl -s "https://boards-api.greenhouse.io/v1/boards/$SLUG/jobs" | head -c 300

# Lever
curl -s "https://api.lever.co/v0/postings/$SLUG?mode=json" | head -c 300

# Ashby
curl -s "https://api.ashbyhq.com/posting-api/job-board/$SLUG?includeCompensation=true" | head -c 300

# Workable
curl -s "https://apply.workable.com/api/v1/widget/accounts/$SLUG" | head -c 300
```

A hit returns a JSON array/object of jobs (Greenhouse: `{"jobs":[...]}`, Lever: a
JSON array, Ashby: `{"jobs":[...]}`, Workable: `{"jobs":[...]}`). A miss returns
`404`, `{}`, an empty array, or an HTML error page. Record the ATS that hit and
the slug that worked. (Workday and other tenant-based ATSs are fiddlier — skip
them here; the user can add them later with `/jobs-add`.)

Keep companies where a probe returns real jobs. Drop the rest for now (the user
can add them later with `/jobs-add`, which does deeper detection).

Show the user the proposed shortlist (name, careers URL, detected ATS + slug)
and wait for a yes before inserting anything. This is the only confirmation gate.

## Step 3: Insert the approved companies

For each approved company, create the record as `active` so its vacancies enter
the pipeline immediately:

```bash
source ~/.zshrc 2>/dev/null && python3 -c "
import sys; sys.path.insert(0, 'scripts')
from database_supabase import ensure_company, get_conn

cid = ensure_company('{COMPANY_NAME}', status='active')
conn = get_conn(); cur = conn.cursor()
cur.execute('''
    UPDATE company SET fetch_strategy = %s, ats_slug = %s,
        careers_url = %s, ats_config = %s, website = %s
    WHERE id = %s
''', ('{STRATEGY}', '{SLUG}', '{CAREERS_URL}', '{ATS_CONFIG_JSON}', '{WEBSITE}', cid))
conn.commit(); cur.close()
print('added', '{COMPANY_NAME}', cid)
"
```

`ats_config` is an empty string for most ATS types — see `/jobs-add` Step 3
for the per-strategy JSON (Greenhouse EU, Workday tenant/board, Firecrawl url).

## Step 3.5: Suggest matching job boards (optional)

Six free job boards are built in but OFF by default (opt-in via the `JOB_BOARDS`
env var). Based on the field and geography extracted in Step 1, suggest only the
boards that fit — and skip this step entirely if none do:

| Profile signal | Suggest | Env to use |
| --- | --- | --- |
| product / marketing / remote-friendly | `remotive` + `weworkremotely` | `JOB_BOARDS=remotive,weworkremotely REMOTIVE_CATEGORIES=product,marketing WWR_CATEGORIES=product,marketing` |
| Europe target / needs visa sponsorship | `arbeitnow` | `JOB_BOARDS=arbeitnow` (add `ARBEITNOW_VISA_ONLY=1` if sponsorship is required) |
| startups / engineering-adjacent | `hn_whoishiring` | `JOB_BOARDS=hn_whoishiring` (monthly thread, 30-day TTL) |
| EA / AI safety / policy | `80k_hours` | `JOB_BOARDS=80k_hours` |
| humanitarian / development | `reliefweb` | `JOB_BOARDS=reliefweb` |

Ask the user before enabling any: boards add many candidate companies to review.
If they opt in, prefix the Step 4 fetch with the chosen env vars and drop
`--no-boards`. If they decline, keep Step 4 as is — companies only.

## Step 4: First fetch -> filter -> score

Fetch only the companies just added (skip job boards on the first run to keep it
fast), then filter and score:

```bash
source ~/.zshrc 2>/dev/null && python3 -u scripts/fetch_vacancies.py --companies "{COMMA_SEPARATED_NAMES}" --no-boards 2>&1
source ~/.zshrc 2>/dev/null && python3 scripts/filter_vacancies.py 2>&1
```

Scoring runs one Opus subagent per vacancy (see `/jobs-score`). For the first run,
score the unscored vacancies and save the results. Then regenerate the
dashboard data:

```bash
source ~/.zshrc 2>/dev/null && python3 scripts/fetch_vacancies.py --report-only 2>&1
```

## Step 5: Launch the local dashboard

```bash
python3 scripts/dashboard_local.py
```

This serves the dashboard at `http://127.0.0.1:8000/` against the local
database (status and company-review buttons persist locally) and opens the
browser. Tell the user to keep this terminal open while reviewing; Ctrl+C stops
it. Pass `--no-browser` or `--port N` if needed.

## Done

Tell the user what they have: N companies monitored, M vacancies scored,
dashboard live locally. From now on the daily command is `/jobs`.
