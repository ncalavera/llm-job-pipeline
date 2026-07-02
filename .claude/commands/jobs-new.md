---
description: The one daily command. Validates your profile, fetches new vacancies, filters junk, scores the new ones with per-vacancy subagents (model set by your plan), shows the top fresh matches in chat, captures your like/pass verdicts, then refreshes (and in full mode deploys) the dashboard. First run auto-onboards an empty database.
---

# /jobs-new

The everyday loop. Run it once a day. One linear pipeline — no sub-stage flags:

```
validate profile → (empty DB? → first-run onboarding) → resume? →
fetch → enrich once → filter → score (incremental) →
show top new matches + capture verdicts → score new candidate companies → publish
```

It works on the local SQLite database with no Supabase and no Vercel (**simple
mode** — regenerate the dashboard data locally, never deploy). With Supabase + a
linked Vercel project + `VERCEL_TOKEN`, it also deploys the live dashboard
(**full mode**). For a visual review instead of chat, run
`python3 scripts/dashboard_local.py`.

This runbook NEVER runs `git add` / `git commit` / `git push`. Versioning code
and docs to `main` stays a manual, separate concern. Publishing here means
regenerating `public/data.js` and (full mode only) `vercel --prod`.

---

## Live status — run long steps in the BACKGROUND and show a card

Three steps are slow and silent (fetch, enrich, score). A foreground command's
stdout is **invisible until it exits** — that is exactly why a 17-minute fetch
looks frozen with no status. So every long step runs in the background while you
reprint a compact progress card from the heartbeat file
`vacancies/run_status.json`:

1. Launch the script with `run_in_background: true`. **Never** wrap it in a
   `while kill -0 … sleep; done` poll loop — that blocks the foreground and
   re-creates the freeze.
2. Every ~20–30s, run `python3 scripts/run_card.py` and post its one-line output
   to chat as the current status, e.g.
   `fetch  ▕███████░░░░░░░░░▏ 18/40 · LinkedIn · +12 new · 6m02s`.
   Pace the polls with the wait / `Monitor` primitive — do **not** foreground
   `sleep` (it is blocked).
3. Stop polling when `run_status.json` shows `"finished": true` **or** the
   background task completes. Then read the tail of the task output for the final
   summary line (`FETCH COMPLETE: N new`, enrich `Done!`, any errors).

`fetch_vacancies.py` and `enrich_blind_vacancies.py` write the heartbeat
themselves. Scoring is driven by you (subagents, not one script), so write the
heartbeat from a one-liner — `begin('score', N)` once, then `step(org, k)` after
each saved chunk — and render the same card with `run_card.py`.

---

## Step 1: Validate the profile FIRST

Scoring needs `config/user_profile.md`. Validate it **before any fetch** so a bad
profile aborts early, not after expensive work:

```bash
test -f config/user_profile.md && echo "profile exists" || echo "MISSING"
```

Read `config/user_profile.md`. **Abort the run** with a clear message if it:
- is missing, or
- is empty, or
- still equals / contains the example placeholders (e.g. "Jane Doe",
  "Example Foundation", identical to `config/user_profile.example.md`).

On abort, tell the user to fill in `config/user_profile.md` (at minimum:
field/role, target locations, seniority, visa status) or run `/jobs-profile` to
edit it, then re-run `/jobs-new`. Do NOT invent profile content and do NOT
proceed past this step on an invalid profile.

---

## Step 2: First-run detection (empty database → onboarding)

Check whether any companies are tracked. The signal is `len(COMPANIES) == 0` —
**company count, not vacancy count.** A database that has companies but zero
vacancies today is a NORMAL daily run (TTL cooldown, quiet day); it must NOT
trigger onboarding.

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from company_registry import COMPANIES, registry_load_failed
print('COMPANIES', len(COMPANIES))
print('LOAD_FAILED', registry_load_failed())
"
```

**If `COMPANIES` > 0:** skip to Step 3 (normal daily run).

**If `LOAD_FAILED` is `True`:** the registry could not load — the database is
unreachable, **not** a fresh clone. The empty `COMPANIES` is an artifact of the
outage. HARD-STOP here: abort with "database unreachable, not a fresh clone — fix
the DB and retry", and **never** offer onboarding. Do not run any of the steps below.

**If `len(COMPANIES) == 0` and `LOAD_FAILED` is `False`:** the company table is
genuinely empty — likely a fresh clone. (`LOAD_FAILED` is the discriminator: it
distinguishes a true-empty table from the same empty `COMPANIES` you'd see during a
brief DB outage, when the registry returns `{}` on any DB error. The hard-stop
above already handled the outage case.) Ask one confirm line before doing anything
destructive:

```
Your database has no companies yet. Run first-run onboarding now
(discover ~10-15 starter companies for your field, validate their ATS,
and add them)?
  1. Yes, onboard
  2. No, stop
```

Only on "yes", run onboarding (folded from the old first-run wizard):

### 2a. Migrate the schema before any INSERT

Converge SQLite and Supabase to the current schema **before inserting anything**:

```bash
python3 scripts/migrate.py 2>&1
```

### 2b. Discover starter companies (web search)

Extract from the profile you read in Step 1:
- **Field / target roles** (e.g. "programme management, operations, social impact")
- **Geography** (e.g. "Berlin, London, remote-EU")

Goal: 10-15 real organisations that (a) match the field and geography and (b) are
likely hiring. Use web search. Prefer mission-aligned employers the user would
actually want — not staffing agencies or aggregators.

For each candidate you need a **careers page URL** and a guess at its **ATS slug**
(the company's handle in the ATS URL — usually a lowercased, no-spaces form of the
name). Validate by probing the public ATS APIs **directly** with `curl` — no
database row and no Firecrawl needed. Try the slug against each ATS until one
returns jobs:

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

Keep companies where a probe returns real jobs; drop the rest for now (the user
can add them later with `/jobs-add`, which does deeper detection).

Show the user the proposed shortlist (name, careers URL, detected ATS + slug) and
**wait for a yes** before inserting anything. This is the only insert gate.

### 2c. Insert the approved companies

For each approved company, create the record as `active` so its vacancies enter
the pipeline immediately:

```bash
python3 -c "
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

`ats_config` is an empty string for most ATS types — see `/jobs-add` for the
per-strategy JSON (Greenhouse EU, Workday tenant/board, Firecrawl url).

### 2d. Confirm hard filters before the first filter run

Show which jobs get dropped automatically and how geography is ranked. These
come from the `## HARD_FILTERS` section of the profile and are NEUTRAL by default
— so by default nothing is dropped or penalised on geography or job title:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from hard_filters import load_hard_filters
hf = load_hard_filters()
fmt = lambda v: ', '.join(v) if v else '(none)'
print('Banned regions:', fmt(hf['ban_regions']))
print('Kept (whitelist):', fmt(hf['keep_countries']))
print('Banned countries:', fmt(hf['ban_countries'] + hf['exclude_countries']))
print('Drop US/Canada-only roles:', 'yes' if hf['ban_us_only'] else 'no')
print('On-site no-penalty regions:', fmt(hf['onsite_ok_regions']), '| penalty:', hf['onsite_penalty'])
print('Title words dropped:', fmt(hf['exclude_title_keywords']))
"
```

If the profile geo policy is still empty, this is the moment to offer to set it:
ask the user (a) which world regions to skip entirely (remote is always kept;
whitelist exceptions like their home country), (b) whether to drop roles bound to
the US/Canada only, and (c) whether to prefer remote by penalising on-site roles
outside their home region. Region ids: `europe, north_america, latin_america,
middle_east, africa, south_asia, southeast_asia, east_asia, ex_ussr, oceania`.
Write their answers into `## HARD_FILTERS` (fields `ban_regions`,
`keep_countries`, `ban_us_only`, `onsite_ok_regions`, `onsite_penalty`) or point
them to `/jobs-profile`.

Then say the policy back plainly ("Before scoring I'll drop jobs in regions: …;
keep remote everywhere; drop US/Canada-only: …; lower on-site outside: … by N
points. Everything else gets scored."). Continue once they confirm.

### 2e. Pick the scoring model for your plan

The model tier is the main cost dial. Scoring is one LLM request per vacancy and
runs inside the user's coding-agent plan, so match the model to their plan. Ask
once:

```
Which coding-agent plan will you score on? This sets the scoring model
(you can change it later in one line — `## VOLUME` → `scoring_model`).
  1. Budget (~$20/month) → Sonnet (recommended default)
  2. Larger (~$100-200/month) → Opus (sharper, heavier on plan usage)
```

Map the answer to `scoring_model` (`sonnet` for 1, `opus` for 2; default to
`sonnet` if they skip). Write it into the profile's `## VOLUME` section — add the
section if it is missing (leave `max_per_run` at the default 150 unless they ask):

```
## VOLUME

scoring_model: {sonnet|opus}
max_per_run: 150
```

Only touch `config/user_profile.md` (never the example file). Confirm the choice
back plainly ("I'll score each vacancy with {model}; change it any time in
`## VOLUME`.").

After inserting, set the fetch scope for Step 3 to **only the just-added
companies** (`--companies "{names}" --no-boards`) to keep the first run fast,
then fall through into the normal pipeline (Steps 3→7).

---

## Step 3 (early): Resume an interrupted prior run

Before fetching new vacancies, check whether unscored `unseen` vacancies already
exist from a previous run that died mid-scoring:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from database_supabase import load_vacancies
vacs = load_vacancies(include_candidate_companies=True)
pending = [v for v in vacs.values()
           if v.get('status') == 'unseen' and v.get('llm_score') is None]
print('UNSCORED_UNSEEN', len(pending))
"
```

If the count is > 0, offer to resume:

```
You have {N} unscored vacancies from a previous run. Resume scoring those
first, before fetching anything new?
  1. Yes, resume scoring
  2. No, run the full pipeline (fetch first)
```

On "yes", jump straight to **Step 6 (Score)** for those pending vacancies, then
continue with Step 7 (verdicts) and Step 8 (publish) — skip fetch/filter for this
run. Scoring and verdict capture are re-entrant (scoring targets `unseen` +
unscored; verdicts commit per-verdict), so resuming never redoes finished work.

---

## Step 4: Fetch new vacancies

Fetch from monitored companies. TTL cooldown applies, so this is cheap daily.
**Run it in the background and show the live card** (see "Live status" above) —
fetch is the slowest, silent step; a blocking call is what made it look frozen.
Launch with `run_in_background: true`, then poll `python3 scripts/run_card.py`
every ~20–30s until `run_status.json` is `finished` or the task completes:

```bash
python3 -u scripts/fetch_vacancies.py 2>&1
```

(On the first-run path, scope to the just-added companies:
`python3 -u scripts/fetch_vacancies.py --companies "{names}" --no-boards 2>&1`.)

**Job boards are off by default and stay off here.** If the user opted into boards
matching their sectors, prefix the command with their `JOB_BOARDS=...` selection.
Board TTLs (2-30 days) keep daily runs cheap either way. Full reference:
`docs/job-boards-catalogue.md`.

**Recommended daily set** (impact-aligned; the boards with a track record of
producing good matches here):

```bash
JOB_BOARDS=80k_hours,impactpool,idealist,fast_forward,linkedin,datadotorg python3 -u scripts/fetch_vacancies.py 2>&1
```

| Board id | Sector fit | On? | Extra env |
| --- | --- | --- | --- |
| `80k_hours` | EA / AI safety / policy | **on** | — |
| `impactpool` | UN / multilateral / development | **on** | — |
| `idealist` | nonprofit (worldwide-remote) | **on** | knobs in defaults.toml |
| `fast_forward` | tech-for-good / nonprofit-tech | **on** | — |
| `linkedin` | targeted ops/programme/impact queries | **on** | edit `queries` in defaults.toml |
| `datadotorg` | data-for-social-impact / AI-for-good (real-employer ATS links) | **on** | — |
| `reliefweb` | humanitarian (M&E/field-heavy) | off | 0 good in history |
| `arbeitnow` | German market, German-language | off | 100% DE, 0 good |
| `remotive` | remote-first, eng-heavy | off | `REMOTIVE_CATEGORIES=...`; 0 good |
| `weworkremotely` | remote commercial tech | off | `WWR_CATEGORIES=...`; 0 good |
| `hn_whoishiring` | startups (monthly thread) | off | eng/US, 0 good |

The "off" boards are available but produced zero good matches (score ≥55) across
the pipeline's history — enable only for a specific reason. LinkedIn throttles
hard: keep its `queries`/`pages` modest.

Useful flags: `--force-all` (ignore TTL), `--companies "A,B"`, `--tier S`,
`--no-boards`, `--free-only`, `--boards-only`.

Note the "FETCH COMPLETE: N new vacancies" line. If N is 0, tell the user there is
nothing new today and skip to Step 8 (still refresh the dashboard). **Record any
`fetch_error`s** — they matter for the publish gate (Step 8).

### Gone-from-source archiving (automatic, stays as-is)

When fetching directly from an ATS (Greenhouse, Lever, Ashby, Workable, Workday),
the pipeline compares the live job list with the database and auto-archives
vacancies no longer on the source with reason `gone_from_source`. This is normal
and stays automatic here. **But watch the volume:** if a truncated fetch (HTTP
200, partial list) archives a large share of an org's vacancies, that's a red
flag for the publish gate (Step 8 requires gone-archive < ~30% of any org).

---

## Step 5: Enrich blind vacancies once

Right after fetch and **before** filter, enrich vacancies that have a URL but no
description (and delete junk with no URL and no description). Run this exactly
once per pipeline — the filter step only re-checks still-blind vacancies later, it
does not re-enrich. **Run it in the background with the live card** (see "Live
status") — Firecrawl makes this slow too:

```bash
python3 scripts/enrich_blind_vacancies.py 2>&1
```

(Requires `FIRECRAWL_API_KEY` in your environment. If unset, skip with a warning;
scoring tolerates some blind rows but accuracy drops.)

---

## Step 6: Filter (quality gate)

Drop junk and obvious non-fits before spending scoring budget:

```bash
python3 scripts/filter_vacancies.py 2>&1
```

This script emits JSON. Read `total_unscored`, `categories`, `delete_ids`,
`reenrich_ids`, `ready`, and `report_path`. Tell the user the report path
(`REPORT-filter.html`); never open it automatically.

Show a short summary (ready to score, delete candidates by category, re-enrich
needed) and ask before deleting — **never auto-delete**:

```bash
python3 scripts/filter_vacancies.py --delete-ids {comma_separated_ids} 2>&1
```

Delete categories include **excluded-country** (`delete_geo`) — vacancies whose
every location sits in a country your profile lists under `## HARD_FILTERS` →
`exclude_countries`, with no escaping remote option. No country is privileged in
code; that list is yours and empty by default. If a job was wrongly dropped on
geography or a title word, that's a personal hard filter, not the universal junk
list — fix it via `/jobs-profile`, not `config.py`.

Optional within filter: dedup (`--delete-ids` after reviewing
`filter_vacancies.py --dedup`), and `--suggest-blacklist` (only after deletes).
Any edit to `UNIVERSAL_JUNK` in `scripts/config.py` or to the profile's
`exclude_title_keywords` needs **explicit confirmation** — never edit silently.

The archive path for filter deletions is
`vacancies/archive/filter_YYYYMMDD_HHMM.json`.

---

## Step 7: Score the new vacancies (configured-model subagent per vacancy, save incrementally)

Score the unscored vacancies. **Pure-fit scoring:** the prompt evaluates role fit
only — skills, seniority, domain, responsibilities. Geography and visa are NOT in
the score; they were handled in Step 6. A great role in the wrong location still
scores high so you can decide with full information.

**Pick the scoring model from the profile.** The model tier is the main cost dial
(cheaper Sonnet on a budget plan, Opus on a bigger one) and lives in the
`## VOLUME` section of the profile. Read it, and use it as the subagent `model`
for every scoring subagent below:

```bash
python3 -c "import sys;sys.path.insert(0,'scripts');from scoring_settings import scoring_model;print(scoring_model())"
```

**Data-quality audit first:** count vacancies with full description / snippet only
/ no description. If blind vacancies exceed 20% of candidates, show a strong
warning and require explicit confirmation before proceeding; at ≤20%, info-level
warning and continue.

Load the vacancies to score (the script prints a JSON array to stdout). Without
`--limit` the script applies the profile's per-run cap (`[## VOLUME] max_per_run`,
default 150) as a spike-day safety net and prints a `Scoring X of Y new
vacancies` line on stderr — surface that count to the user. Pass `--limit N` only
to override the cap explicitly:

```bash
python3 scripts/score_vacancies.py --local 2>&1
```

For **each** vacancy, launch a **separate** subagent with the model you read above
(`model: "<scoring_model>"`, e.g. `"sonnet"` or `"opus"`):
- System prompt: `VACANCY_SCORING_PROMPT` (from `scripts/prompts/vacancy-scoring.md`).
- User message: `VACANCY_SCORING_USER_TEMPLATE` with substitution.
- Subagent returns JSON: `score`, `reasoning`, `tags`, `hard_requirements`,
  `short_summary`.

**Critical:** 1 vacancy = 1 subagent. Never send 2-3 vacancies in one prompt — it
causes systematic over-scoring (+20-50 points). Use the `member_ids` array from
the `--local` output (the real DB UUIDs), not the top-level `id`.

**Concurrency:** run at most **5 subagents at a time** (rolling). Launch a wave of
5, await them, then launch the next 5 — never fan out all N at once. The cleanest
way is a Workflow that loops `parallel()` over groups of 5, one role per `agent()`
call with the configured `model` (the `scoring_model` you read above); the
per-role work units come from `score_vacancies.py --local` (one JSON object per
vacancy, each carrying its own `system_prompt` + `user_msg`).

**Show the live card here too.** Scoring fans out over minutes; drive the same
heartbeat yourself. Once, after loading the batch of `N`:

```bash
python3 -c "import sys;sys.path.insert(0,'scripts');import run_status as r;r.begin('score',$N)"
```

After each saved chunk, advance it and reprint the card (`k` = vacancies scored
so far):

```bash
python3 -c "import sys;sys.path.insert(0,'scripts');import run_status as r;r.step('$ORG',$k)"
python3 scripts/run_card.py
```

When the batch is done: `r.finish()`.

### Save INCREMENTALLY (per vacancy or small chunks)

Do NOT collect all results into one array and save once at the end — a crash
before that save loses the whole batch. Instead, as each subagent returns (or
after every few), save that chunk immediately. Each `--save` call commits, so an
interrupted run keeps every score already written:

```bash
cat > /tmp/scores_chunk.json <<'EOF'
[
  {
    "member_ids": ["<uuid>", "..."],
    "org": "Acme",
    "title": "Head of Community",
    "score": 78,
    "reasoning": "...",
    "tags": ["community", "operations"],
    "hard_requirements": ["5y community leadership"],
    "short_summary": "4-6 sentences ..."
  }
]
EOF
python3 scripts/score_vacancies.py --save < /tmp/scores_chunk.json 2>&1
```

`--save` builds the DB record from these flat fields (no nested `score_data`
needed). Repeat per vacancy / small chunk until the batch is done.

**Do NOT pass `--archive`.** Score-threshold archival of low-scoring unseen
vacancies is `/jobs-review archive`'s job, deliberately separate under pure-fit
scoring (a high score in an excluded geography would otherwise be wrongly
archived). The gone-from-source archive inside fetch (Step 4) is unaffected.

Show the score distribution (75+, 55-74, 35-54, below 35). Note any scraping-
quality issues you saw (not-a-vacancy artifacts, broken pages, thin descriptions)
— useful feedback for the fetch/filter side.

If scoring is interrupted mid-batch, the next `/jobs-new` run's Step 3 resume
prompt picks up the remaining unscored `unseen` vacancies.

---

## Step 8: Show top new matches + capture verdicts

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
them say "like the first three"). **Write each verdict immediately** (one commit
per verdict, so an interruption keeps captured decisions):

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
`applied`. Default the quick triage to `liked` / `passed`. This is the quick
daily verdict pass — the deep structured review of `liked` vacancies (apply /
research / network decisions, issue tracking) lives in `/jobs-review`.

---

## Step 8.5: Score new candidate companies (optional, gated)

Fetch discovers new **companies** too — boards and LinkedIn add organisations as
`candidate`, waiting to be scored before you approve or reject them on the
Companies tab. This step enriches those candidates (the company-level
`alignment_score` + `mission_fit` + `about`), which is **separate** from the
vacancy enrich in Step 5 (that one only backfills missing job descriptions).

### Detect candidates needing a score

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from db_conn import get_conn
cur = get_conn().cursor()
cur.execute(\"\"\"
    SELECT count(*) FROM company
    WHERE status = 'candidate' AND alignment_score IS NULL
      AND website IS NOT NULL AND website != ''
\"\"\")
print('CANDIDATES_TO_SCORE', cur.fetchone()[0])
"
```

If the count is **0**, print "No new candidate companies to score" and skip
straight to Step 9. Otherwise offer the run (this is the walk-through gate):

```
{N} new candidate companies were discovered. Score them now so they show up
in Companies → Pending for your approval?
  1. Yes, score them
  2. No, skip — leave them unscored for now
```

Only on "yes", run the sub-flow below. It needs `FIRECRAWL_API_KEY` (for the
about-page scrape) — if unset, tell the user and skip to Step 9.

### 8.5a. Scrape about pages (Firecrawl, background)

Company scoring reads each company's about page from the scrape cache, so scrape
the unscored candidates first. Run it in the **background** (Firecrawl is slow)
and report the final summary line when it finishes:

```bash
python3 scripts/fetch_companies.py --limit {N} 2>&1
```

`fetch_companies.py` scrapes only candidates that have a website and no cached
markdown, writing `.firecrawl/scrape_cache.json`. Companies it cannot scrape are
skipped by the next step (no cache entry → no scoring payload).

### 8.5b. Prepare scoring payloads

```bash
python3 scripts/score_companies.py --local --limit {N} 2>&1
```

This prints a JSON array to stdout — one object per company with
`payload_kind: "company"`, `id`, `canonical_name`, `url`, `system_prompt`, and
`user_msg`. Companies not in the scrape cache are silently dropped (the stderr
line "Skipped K companies" tells you how many).

### 8.5c. Score — one subagent per company

For **each** payload, launch a **separate** subagent with `model: "sonnet"`
(company enrichment is saved with the `claude-sonnet-4-6` label; Sonnet is the
right tier here — full-org judgement, not the per-vacancy precision that needs
Opus):

- System prompt: the payload's `system_prompt`.
- User message: the payload's `user_msg`.
- The subagent returns JSON with `about`, `mission_fit` (carrying
  `alignment_score`, `alignment_label`, `strengths`, `risks`, `approach`,
  `mission_verdict`, `experience_match_reasoning`), and a top-level
  `alignment_score`.

**1 company = 1 subagent.** Never batch several companies into one prompt — it
over-scores, same failure as vacancy scoring. Run at most **5 subagents at a
time** (rolling waves), exactly like Step 7. A Workflow that loops `parallel()`
over groups of 5 — one company per `agent()` call with `model: "sonnet"` — is the
cleanest driver; the per-company work units are the objects from
`score_companies.py --local`.

### 8.5d. Save incrementally

As each subagent returns (or after a small chunk), wrap its result under
`enrichment` and save immediately — each `--save` commits, so an interrupt keeps
finished work:

```bash
cat > /tmp/company_scores.json <<'EOF'
[
  {
    "payload_kind": "company",
    "id": "<company-uuid from the payload>",
    "canonical_name": "Acme Foundation",
    "enrichment": {
      "alignment_score": 78,
      "about": { "description": "...", "sector": "...", "hq_location": "..." },
      "mission_fit": {
        "alignment_score": 78,
        "alignment_label": "Good fit",
        "strengths": ["..."],
        "risks": ["..."],
        "approach": "...",
        "mission_verdict": "...",
        "experience_match_reasoning": "..."
      }
    }
  }
]
EOF
python3 scripts/score_companies.py --save < /tmp/company_scores.json 2>&1
```

`--save` writes `about` + `mission_fit` + `alignment_score`, mirrors `category` /
`offices` onto the top-level columns the dashboard reads, computes the tier, and
sets `enriched_at`. The `id` and `canonical_name` come straight from the payload.

### 8.5e. Where they land

`--save` runs `auto_review_candidates()`, but that is **opt-in and off by
default** (env `AUTO_REVIEW_CANDIDATES`), so scored companies stay `candidate` —
i.e. they appear under **Companies → Pending** for you to approve or reject.
Tell the user the count scored and that they are waiting in Pending (the deeper
company review — approve/reject, monitoring — lives in `/jobs-review companies
--status candidate`). The Companies tab reads live from the DB, so a browser
refresh shows them with no deploy.

---

## Step 9: Publish

The dashboard now reads its data **live**. Publishing a data change no longer
needs a deploy — `--report-only` updates the source the dashboard reads, and a
browser refresh shows it. `vercel --prod` is only for dashboard **code** changes.

What `--report-only` does depends on the mode:

* **Full mode** (Supabase) — upserts the `dashboard_snapshot` row that
  `/api/vacancies` serves. The deployed dashboard is live: refresh the browser,
  no deploy.
* **Simple mode** (local SQLite) — writes `public/data.js` (gitignored), served
  by the local dashboard server. Refresh the browser; start it with
  `python3 scripts/dashboard_local.py` if needed. **Never deploy, never push.**
  Do NOT run `--report-only` from a git worktree — it writes the wrong
  `public/data.js`.

### Full mode — publish only a CLEAN run

Publishing now writes the **live** snapshot directly, so a bad run (a truncated
ATS fetch that mass-archived real vacancies, fetch/score errors) would corrupt
the live dashboard immediately — there is no deploy step left to catch it. So the
clean-run gate moved from "before deploy" to "before regenerate".

Regenerate ONLY when the run had **zero stage errors** AND gone-from-source
archival was < ~30% of any single org this run:

```bash
python3 scripts/fetch_vacancies.py --report-only 2>&1
```

If any stage failed or the gone-archive share is high: **do NOT regenerate.** The
previous good snapshot stays live. Tell the user to review (e.g. a truncated ATS
fetch may have archived real vacancies) before publishing.

**Rollback.** Each regenerate copies the old payload to a `previous` row before
overwriting `current`. If a bad snapshot did go live, restore the prior one:

```bash
psql "$SUPABASE_DB_URL" -c "UPDATE dashboard_snapshot c SET payload = p.payload, \
updated_at = now() FROM dashboard_snapshot p WHERE c.id='current' AND p.id='previous';"
```

**Preview.** In full mode the data is live on the deployed dashboard — preview by
refreshing it. `scripts/dashboard_local.py` is the **simple-mode** server (it reads
the local `data.js`, which full mode does not write), so don't use it to preview a
full-mode run. As always, never run `--report-only` from a git worktree.

### Assert dashboard auth (full mode)

`/api/vacancies` **fails closed** — with no `AUTH_USER` / `AUTH_PASS` on the
Vercel project it returns 503 and serves no PII, so the dashboard simply will not
load until auth is set. Confirm it is set so the dashboard works AND stays
private. `middleware.js` reads these from the **Vercel project** env, not your
local shell:

```bash
venv=$(vercel env ls production 2>/dev/null)
echo "$venv" | grep -q 'AUTH_USER' && echo "$venv" | grep -q 'AUTH_PASS' \
  && echo "auth OK — Vercel project has AUTH_USER + AUTH_PASS" \
  || echo "AUTH MISSING in Vercel project — /api/vacancies will 503 (safe, but dashboard is down)"
```

### Deploy (code changes only)

Run `vercel --prod` **only** when you changed dashboard CODE (`api/`, `public/`,
`middleware.js`, `vercel.json`) — never for a data change. Same gate as before:
Supabase configured AND `.vercel/project.json` exists AND `VERCEL_TOKEN` set.

```bash
vercel --prod
```

Show the resulting URL. **Never** run `git add` / `git commit` / `git push` here.

---

## Done

Summarize: N new fetched, M scored, the verdicts captured (liked / passed counts),
how many candidate companies were scored this run (and that they wait in
Companies → Pending), how many liked vacancies are now waiting for a deeper look
(`/jobs-review`), and whether the live snapshot was refreshed (full mode —
visible on browser refresh, no deploy) or `data.js` was regenerated locally
(simple mode), or publishing was skipped because the run was not clean.

## Common issues

- **Firecrawl rate limit**: large enrich batches may throttle; the script retries,
  you may need a second run.
- **High blind rate (>20%)**: run with enrichment before scoring for accuracy.
- **A company's `fetch_error` keeps rising**: its careers page likely changed
  structure — re-run `/jobs-add` for that company to re-detect the ATS.
- **Subagent timeout**: one subagent hung — re-run (scoring is idempotent; already
  saved chunks are skipped because they are no longer unscored).
