---
description: View or edit your candidate profile — summary view, hard-filter rules, or broader section edits, all in one command. Source is config/user_profile.md.
---

# /jobs-profile

Manage `config/user_profile.md` — the single source of truth for scoring AND
filtering. Three modes selected by the first argument:

| Invocation | What it does |
|---|---|
| `/jobs-profile` (no arg) | Print a profile summary + active HARD filters |
| `/jobs-profile rules` | Edit `exclude_countries` / `exclude_title_keywords` in `## HARD_FILTERS` |
| `/jobs-profile edit` | Broader edits — seniority, target roles, exclude patterns, geography |

Profile creation (first-time) happens on the onboarding page (`docs/index.html`)
or automatically when you first run `/jobs-new`. This command never creates the
profile from scratch — it only reads or edits an existing one.

---

## Mode: view (no arg)

### Step 1 — Locate the profile

Check whether `config/user_profile.md` exists.

- **File present** — proceed to Step 2.
- **File absent** — print:

  > **No profile found — showing example**
  > `config/user_profile.md` does not exist yet. The output below comes from
  > `config/user_profile.example.md` so you can see what a completed profile
  > looks like. To create your real profile, complete the onboarding wizard on
  > the landing page (`docs/index.html`) or run `/jobs-new` (first run detects
  > an empty database and walks you through setup).

  Then read `config/user_profile.example.md` and proceed — but treat every
  displayed value as illustrative, not real.

### Step 2 — Print the profile summary

Read the profile and summarise in plain language:

- **Seniority target** (from `## SENIORITY` or equivalent section)
- **Target roles / titles** (from `## TARGET_ROLES`)
- **Geography preferences** (from `## GEOGRAPHY`)
- **Exclude patterns** (from `## EXCLUDE_PATTERNS` — soft score penalty)
- **Hard filters** — run the snippet below and read the result back conversationally:

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

Example output to emulate:

> **Profile summary**
> Seniority: Senior / Staff IC. Target roles: Product Engineer, Full-Stack
> Engineer, Platform Engineer. Geography: EU-only (remote or relocation
> package). Soft exclusions: sales, recruiter, QA-only.
>
> Hard filters — these run before scoring and drop vacancies entirely:
> Countries dropped: united states, canada
> Title words dropped: (none — keeping every discipline)

Stop here unless the user asks to make changes.

---

## Mode: `rules`

Edit the HARD_FILTERS section of `config/user_profile.md`. These are
deterministic — they drop a vacancy BEFORE the LLM scores it.

Two fields only:

- **`exclude_countries`** — drop a job only when EVERY location it lists is in
  one of these countries. A job that also lists a non-excluded country is kept.
- **`exclude_title_keywords`** — drop a job whose title contains one of these
  words (whole-word, case-insensitive match).

Both are empty by default. Everything softer (roles you'd rather not do but
might consider) belongs in `## EXCLUDE_PATTERNS` (score penalty only) — use
`/jobs-profile edit` for that.

### Step 1 — Missing-profile fallback

```bash
test -f config/user_profile.md || cp config/user_profile.example.md config/user_profile.md
```

If the file was just created from the example, tell the user:

> `config/user_profile.md` did not exist — I've copied the example as your
> starting profile. Review the other sections with `/jobs-profile edit` when
> ready.

### Step 2 — Show current hard filters

Run the snippet from the view mode and read the current rules back.

### Step 3 — Decide the change

Ask what they want to change, one question at a time. Common requests:

- "Stop seeing US jobs" → add `united states` (and usually `canada`) to
  `exclude_countries`.
- "No engineering roles" → add `engineer`, `developer` to
  `exclude_title_keywords`.
- "Show me US jobs again" → remove `united states` from `exclude_countries`.

Use plain country names (`united states`, `germany`) and plain title words
(`engineer`, `sales`). Confirm the exact final lists before writing.

### Step 4 — Write the change

Edit ONLY the two field lines inside `## HARD_FILTERS` in
`config/user_profile.md`. Leave any explanatory comment block above them
intact. The two lines must look exactly like this:

```
exclude_countries: united states, canada
exclude_title_keywords: engineer, developer
```

To clear a filter entirely:

```
exclude_countries: (none)
```

### Step 5 — Confirm it took effect

Re-run the Step 2 snippet and read the new rules back. Remind the user the
change takes effect on the next `/jobs-new` run — it does not retroactively
touch vacancies already in the database. If they want the new rules applied
immediately, suggest running `/jobs-new` now.

### Rules of thumb

- **Hard vs soft.** Hard filters delete before scoring — use them only for
  things you never want to see. If you'd still glance at a role, use
  `## EXCLUDE_PATTERNS` (score penalty) instead.
- **Geography is all-or-nothing per job.** A job is dropped only when ALL its
  locations are excluded. A London + New York job survives even if you excluded
  the US.
- **Title match is whole-word.** `engineer` drops "Software Engineer" but a
  broad word can catch more than you expect — preview with `/jobs-new` after a
  change and narrow the word if it over-deletes.
- **Empty is the safe default.** When in doubt, leave a field `(none)` and let
  the scorer rank jobs against your profile.

---

## Mode: `edit`

Broader edits to sections other than HARD_FILTERS — seniority target, target
roles, exclude patterns (soft), geography preferences.

### Step 1 — Missing-profile fallback

Same as `rules` mode:

```bash
test -f config/user_profile.md || cp config/user_profile.example.md config/user_profile.md
```

Tell the user if the file was just copied from the example.

### Step 2 — Show the relevant section(s)

Read `config/user_profile.md` and print the section the user wants to change.
If no specific section was named, print a short summary of all editable
sections and ask which one to update.

Common sections and what they affect:

| Section | Affects |
|---|---|
| `## SENIORITY` | Scoring weight toward senior/staff/principal levels |
| `## TARGET_ROLES` | Role titles that score highest |
| `## GEOGRAPHY` | Preferred locations / remote policy |
| `## EXCLUDE_PATTERNS` | Soft score penalty (role types to down-rank, not delete) |
| `## HARD_FILTERS` | Hard drop before scoring — use `/jobs-profile rules` for this |

### Step 3 — Agree on the change

Ask what they want to change, one question at a time. Confirm the exact new
content before writing.

### Step 4 — Write the change

Edit only the targeted section. Do not touch any other section or the
HARD_FILTERS block.

### Step 5 — Confirm

Read the updated section back to the user. Remind them changes apply on the
next `/jobs-new` run. For EXCLUDE_PATTERNS changes, note these affect scoring
but never drop a vacancy entirely.
