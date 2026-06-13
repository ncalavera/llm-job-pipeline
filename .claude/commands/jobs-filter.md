---
description: Quality gate between /jobs-fetch and /jobs-score — classifies unscored vacancies, deletes junk by title/location/geo bucket, optionally deduplicates cross-board. Suggests blacklist improvements.
---

# /jobs-filter

Quality gate. Runs `scripts/filter_vacancies.py` and interprets the output.

## Step 0: Optional dedup pre-step

Ask:
```
Run deduplication before filtering?
  1. Yes (exact hash dupes + fuzzy title matching across boards)
  2. No, classification only
```

**If yes:**
```bash
python3 scripts/filter_vacancies.py --dedup
```

Parse the JSON output. If `dedup.merged > 0`, report exact duplicates cleaned and archive path.
If `fuzzy_dupes` is present, show a table of pairs for manual review. For each suspicious pair, ask: "Delete the shorter-description duplicate? (y/n/skip all)". On confirmation, run `--delete-ids` with the confirmed IDs.

## Step 0b: Run analysis

```bash
python3 scripts/filter_vacancies.py
```

Parse the JSON output. Extract key values:
- `total_unscored` — total unscored vacancies
- `categories` — counts per category
- `delete_ids` — IDs grouped by delete category
- `reenrich_ids` — IDs grouped by re-enrich type
- `ready` — count ready to score
- `report_path` — path to the HTML report

Tell the user the report path (`REPORT-filter.html`). Never open it automatically.

## Step 1: Terminal summary

```
FILTER ANALYSIS
═══════════════════════════════════════
  Unscored total:     {total_unscored}
  Ready to score:     {ready}
  Delete candidates:  {sum of delete categories}
    - Blacklist (title):       {delete_blacklist}
    - Content junk:            {delete_junk}
    - Re-archived:             {delete_rearchived}
    - Excluded country:        {delete_geo}
    - Stale blind:             {delete_stale_blind}
  Re-enrich needed:   {sum of reenrich categories}
    - Blind (no desc):   {reenrich_blind}
    - Thin (<100 chars): {reenrich_thin}
═══════════════════════════════════════
```

**Geo-based deletion category:**
- **Excluded country** (`delete_geo`) — vacancies whose every location sits in a country your profile lists under `## HARD_FILTERS` → `exclude_countries`, with no remote option that escapes it. Detected via `geo.py` bucket classification. No country is privileged in code; the exclusion list is yours and empty by default.

## Step 2: DELETE decision

```
Delete {N} irrelevant vacancies?
  1. Delete all ({N}) — recommended
  2. Excluded-country only ({geo_count})
  3. Choose categories (blacklist, junk, rearchived, geo, stale)
  4. Skip
```

**Delete all:**
```bash
python3 scripts/filter_vacancies.py --delete-ids {comma_separated_ids}
```

After deletion, show: deleted count, archive file path, remaining DB size.

## Step 2.5: Duplicate check (before enrichment)

Before spending Firecrawl credits on blind vacancies, check whether they are already duplicates of non-blind vacancies:

```python
from collections import defaultdict
from database_supabase import load_vacancies

v = load_vacancies(unscored_only=True)
blind_ids = {vid for vid, vac in v.items()
             if not (vac.get('full_description') or '').strip()
             and not (vac.get('snippet') or '').strip()}

# Index non-blind vacancies by (org, title) for exact match
non_blind_index = defaultdict(list)
...
```

If confirmed duplicates are found, show them and ask: "Delete blind duplicates? (y/n)".

## Step 3: RE-ENRICH decision

If there are re-enrich candidates, show the first 10 and estimated Firecrawl credit cost (~5 credits each):

```
RE-ENRICH CANDIDATES ({count} vacancies)
  Org              | Title                          | Type
  ...
  Estimated Firecrawl credits: ~{count * 5}
```

Ask:
```
Enrich {N} vacancies via Firecrawl (~{credits} credits)?
  1. Yes, enrich all
  2. Skip
```

**If enrich:**
```bash
python3 scripts/enrich_blind_vacancies.py
```

### Step 3b: Delete still-blind (mandatory after enrichment)

After enrichment, find vacancies that are still blind or thin (<100 chars) — these failed enrichment and will be skipped by scoring anyway. Delete them automatically:

```bash
python3 scripts/filter_vacancies.py --delete-ids {still_blind_ids}
```

## Step 4: Blacklist feedback

Only if vacancies were deleted in Step 2:

```bash
python3 scripts/filter_vacancies.py --suggest-blacklist
```

Show title patterns with 3+ matches across 2+ organizations, and companies with >80% deletion rate. For each suggested pattern, decide where it belongs: a universal non-job pattern (talent pool, course, etc.) goes in `UNIVERSAL_JUNK` / `UNIVERSAL_JUNK_SUBSTR` in `scripts/config.py`; a discipline you personally don't want goes in your profile's `exclude_title_keywords` via `/jobs-rules`. Ask the user before editing either.

For companies with >80% deletion: ask whether to pause monitoring.

## Step 5: Advisory LLM Review (optional)

Ask:
```
Run LLM advisory review of {ready_count} remaining vacancies?
  1. Yes — each vacancy reviewed, results in a table
  2. No — proceed to summary
```

If yes, spawn parallel Haiku subagents (1 per vacancy) to classify each as `relevant | junk | borderline` and suggest filter improvements. Save results to `REPORT-filter-advisory.html`.

Show counts (relevant / junk / borderline / new filter suggestions). Ask whether to apply proposed filters.

## Step 6: Final summary

```
FILTER COMPLETE
═══════════════════════════════════════
  Dedup:     {exact_cleaned} exact, {fuzzy_pairs} fuzzy (quarantined)
  Deleted:   {count} vacancies (archive: {filename})
  Enriched:  {count} vacancies
  Blacklist: {count} new patterns
  LLM review:{reviewed} reviewed, {junk} junk, {new_filters} filters
  Ready:     {ready_count} vacancies for /jobs-score
═══════════════════════════════════════
  Next: /jobs-score --limit {ready_count}
```

## If the filter removes something important

There are two layers of deletion:

- **Universal junk** (`UNIVERSAL_JUNK` / `UNIVERSAL_JUNK_SUBSTR` in
  `scripts/config.py`) — talent pools, "expression of interest" listings,
  volunteer calls, course/bootcamp listings. These apply to everyone and rarely
  need editing.
- **Your personal hard filters** — geography (`exclude_countries`) and title
  words (`exclude_title_keywords`) from your profile's `## HARD_FILTERS`.

If a job was dropped on geography or because of a title word (e.g. "Operations
Engineer" deleted because you excluded `engineer`), that's a personal hard
filter, NOT the universal list. Fix it with `/jobs-rules`: remove or narrow the
country/word, then re-run `/jobs-filter`. Only touch `UNIVERSAL_JUNK` in
`config.py` if something nobody would ever want is slipping through (or being
wrongly caught).

## If relevant vacancies keep slipping through

If vacancies you then mark `passed` keep appearing in `ready`, you usually don't
want a hard delete — let scoring rank them low instead (tune `## EXCLUDE_PATTERNS`
in your profile). Add a word to `exclude_title_keywords` via `/jobs-rules` only
when you NEVER want to see that discipline again. The command shows common junk
title words near the top of its report.

## Important rules

- **Never auto-delete** — always show preview and wait for explicit confirmation.
- **HTML report** — tell user the path, never open automatically.
- **Archive format** — `vacancies/jobs-archive/filter_YYYYMMDD_HHMM.json`.
- **Blacklist edits need confirmation** — show exact changes before editing `config.py`.
- **After filtering, suggest /jobs-score** — the natural next step in the pipeline.
