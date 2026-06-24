# Prompt templates

Scoring relies on two templates:

- `scripts/prompts/vacancy-scoring.md` — scores a vacancy against your
  profile.
- `scripts/prompts/company-scoring.md` — scores a company.

Both contain `{{NAME}}`-style placeholders, substituted with sections from
`config/user_profile.md`. The loader is `scripts/prompts.py`.

## Pure-fit scoring (v4.0)

The vacancy prompt deliberately contains **no location scoring**.
Geography, relocation, remote policy, and visa / work-authorisation
considerations are excluded from the score and from the
`hard_requirements` field — the score reflects only role fit, mission fit,
and seniority fit. Geography is enforced earlier by the pre-score filter
(`filter_vacancies.py` + `hard_filters.py`), which deletes vacancies whose
every location is in a country you listed under `exclude_countries` in the
`## HARD_FILTERS` section of your profile — before they ever reach the LLM.
No country is hardcoded; the list is empty by default.

If you add location rules back into the prompt, remember the filter will
double-penalize — pick one layer.

## Available placeholders

| Placeholder | Comes from | What to put there |
| --- | --- | --- |
| `{{USER_PROFILE}}` | `## USER_PROFILE` section | Who you are, experience, skills, languages |
| `{{TARGET_ROLES}}` | `## TARGET_ROLES` section | Which roles you want |
| `{{EXCLUDE_PATTERNS}}` | `## EXCLUDE_PATTERNS` section | What to exclude |
| `{{SHORT_SUMMARY_INSTRUCTION}}` | `## SHORT_SUMMARY_INSTRUCTION` section | How to write the dashboard-card summary |
| `{{OUTPUT_LANGUAGE}}` | `## OUTPUT_LANGUAGE` section | Output language (English / anything) |
| `{{ABOUT_INSTRUCTION}}` | `## ABOUT_INSTRUCTION` section | How to describe a company |
| `{{CUSTOM_CRITERION_LABEL}}` | `## CUSTOM_CRITERION_LABEL` section | Name of your extra criterion |
| `{{CUSTOM_CRITERION_DESCRIPTION}}` | `## CUSTOM_CRITERION_DESCRIPTION` section | Description of that criterion |
| `{{CUSTOM_BOOST_FIELD}}` | `## CUSTOM_BOOST_FIELD` section | Field name in the LLM response |

## Adding your own placeholder

1. Add a section to `config/user_profile.md`:
   ```markdown
   ## MY_NEW_FIELD

   Any text here.
   ```
2. Use it in a prompt template: `{{MY_NEW_FIELD}}`.
3. `prompts.py` picks it up automatically — nothing to recompile.

If a placeholder isn't found in `user_profile.md`, it stays in the text
as-is (so don't leave holes).

## Re-scoring after a prompt change

When you change `user_profile.md` or the prompt itself, old scores stay in
the DB. To re-run everything:

```sql
-- Reset scores so /jobs-new runs scoring again
UPDATE vacancy SET llm_score = NULL, llm_scored_at = NULL
WHERE status = 'unseen';
```

Then `python3 scripts/score_vacancies.py --local --limit 200`.

## Good practices

- **Don't write the profile in negatives**: "I don't want X" works worse
  than "I want Y". Use `EXCLUDE_PATTERNS` for negatives, `USER_PROFILE`
  for positives.
- **Specifics beat abstractions**: "8 years in operations roles, the last
  3 at 100+ headcount" works better than "senior operations leader".
- **List languages with levels**: "English native, German B1, Spanish B2".
  Without this the LLM can't tell whether a "working language: Spanish"
  role is viable for you.
- **Review 10 random scores once a week**: if something is systematically
  over- or under-scored, add a rule to `EXCLUDE_PATTERNS` or fix the
  profile.

## A different output language

`OUTPUT_LANGUAGE` can be `English`, `Spanish`, anything — the LLM adapts.
The dashboard renders any script equally well.

If you want intake in one language but output in another, write the
profile in whatever language you like — just make sure `OUTPUT_LANGUAGE`
matches what you want to see on the cards.
