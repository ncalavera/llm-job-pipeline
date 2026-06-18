---
description: View, add, or remove your HARD filters — the deterministic rules that drop vacancies BEFORE scoring. Edits the HARD_FILTERS section of config/user_profile.md (exclude_countries, exclude_title_keywords). Empty by default, so nothing personal is dropped until you opt in.
---

# /jobs-rules

Manage your **hard filters** — the on/off rules that throw a vacancy away before
the LLM ever scores it. There are exactly two:

- **exclude_countries** — drop a job only when EVERY location it lists is in one
  of these countries. A job that also lists a country you did NOT exclude is
  kept.
- **exclude_title_keywords** — drop a job whose TITLE contains one of these
  words (whole-word match).

Both live in the `## HARD_FILTERS` section of `config/user_profile.md` and are
EMPTY by default — out of the box no job is dropped on geography or on its title
discipline. Everything softer (a role you'd rather not do but might consider)
belongs in `## EXCLUDE_PATTERNS` instead, which only lowers the score.

This command never deletes data. It only edits two lines in your profile; the
new rules take effect on the next `/jobs-filter`.

## Step 1: Show current hard filters

Read `config/user_profile.md` (fall back to `config/user_profile.example.md` if
the real one does not exist yet) and print what is active in plain language:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from hard_filters import load_hard_filters
hf = load_hard_filters()
c = hf['exclude_countries']; k = hf['exclude_title_keywords']
print('Countries dropped:', ', '.join(c) if c else '(none — keeping every country)')
print('Title words dropped:', ', '.join(k) if k else '(none — keeping every discipline)')
"
```

Then say it back the way a person would, e.g.:

> Right now I drop jobs that are ONLY in the United States or Canada, and jobs
> whose title contains "engineer" or "developer". Nothing else is dropped before
> scoring.

or, when both are empty:

> No hard filters are set. Every country and every job title makes it through to
> scoring. Scoring still ranks them against your profile.

If the user only wanted to see the rules, stop here.

## Step 2: Decide the change

Ask what they want to change, one question at a time. Typical asks:

- "Stop seeing US jobs" → add `united states` (and usually `canada`) to
  exclude_countries.
- "I don't want engineering roles" → add `engineer`, `developer` to
  exclude_title_keywords.
- "Actually, show me US jobs again" → remove `united states` from
  exclude_countries.

Use plain country names ("united states", "canada", "germany") and plain title
words ("engineer", "sales", "nurse"). Case does not matter. Confirm the exact
final lists with the user before writing.

## Step 3: Write the change

Edit ONLY the two field lines inside the `## HARD_FILTERS` section of
`config/user_profile.md`. Leave the explanatory comment block above them intact.
The two lines must look exactly like this (comma-separated, or `(none)` to clear
a field):

```
exclude_countries: united states, canada
exclude_title_keywords: engineer, developer
```

To clear a filter entirely, set its value back to `(none)`:

```
exclude_countries: (none)
```

If `config/user_profile.md` does not exist yet, copy the example first so you do
not edit the template that ships with the repo:

```bash
test -f config/user_profile.md || cp config/user_profile.example.md config/user_profile.md
```

## Step 4: Confirm it took effect

Re-run the Step 1 snippet and read the new rules back to the user. Remind them
the change applies on the next `/jobs-filter` run — it does not touch vacancies
already in the database. If they want the new rules applied to what's already
stored, suggest running `/jobs-filter` now.

## Rules of thumb

- **Hard vs soft.** Hard filters DELETE before scoring — use them only for
  things you never want to see. If you'd still glance at it, use
  `## EXCLUDE_PATTERNS` (a score penalty) instead.
- **Geography is all-or-nothing per job.** A job is only dropped when ALL its
  locations are excluded. A London + New York job survives even if you excluded
  the US.
- **Title match is whole-word.** "engineer" drops "Software Engineer" but a
  broad word can catch more than you expect — preview with `/jobs-filter` after
  a change and narrow the word if it over-deletes.
- **Empty is the safe default.** When in doubt, leave a field `(none)` and let
  the scorer rank jobs against your profile instead.
