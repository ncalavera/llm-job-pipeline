# User profile

THE single human-editable source of your taste. Copy it to
`config/user_profile.md` (the real file is gitignored) and fill it in.
The whole pipeline reads from this one file:
- **Scoring** — `scripts/prompts.py` substitutes each `## SECTION` into the
  LLM scoring prompt (`{{SECTION_NAME}}`).
- **Filtering** — `scripts/hard_filters.py` reads `## HARD_FILTERS`
  (exclude_countries / exclude_title_keywords) to drop vacancies before
  scoring.

Machine mechanics you do NOT edit (thresholds, geo tables, job boards,
universal junk words) live in `config/defaults.toml`. Sections you don't
need can be left empty or removed.

## USER_PROFILE

**Name:** Jane Doe
**Current location:** Lisbon, Portugal
**Target locations:** Berlin (DE), London (UK), remote-EU
**Target start:** within 6 months
**Salary benchmark:** target ~€5,500/month, hard floor ~€4,000/month
**Visa status:** EU passport — no sponsorship needed. (If you need a sponsor,
say so here — the prompt uses this to weight visa-friendly companies.)

### Professional experience (~7 years)

- 2022–2026, Senior Programme Manager at Example Foundation — built a
  €2M/year grants programme, managed cross-functional team of 6.
- 2019–2022, Operations Manager at Example Startup (Series B) — scaled
  ops from 30 to 120 people.
- 2017–2019, Project Manager at Example NGO — ran a youth education
  programme across 4 countries.

### Core skills (in priority order)

- Programme / project management at scale
- Operations & process design
- Stakeholder management (foundations, government, partners)
- Team building (0→1 hires)
- Languages: English (native), Portuguese (C1), Spanish (B2)

### What energises you

- Building 0→1 programmes with measurable impact
- Roles with budget ownership
- Mission-driven organisations (climate, education, public health)
- Cross-cultural, international teams

### Domain preferences

**Want to work in:** climate adaptation, philanthropy/grantmaking, public
health, education access, civic tech.

**Open but not first choice:** general operations at scaling startups,
foundation strategy roles.

## TARGET_ROLES

- **Operations track:** Director of Operations, Head of Operations,
  Chief of Staff, Operations Manager.
- **Programme track:** Senior Programme Manager, Programme Director,
  Project Director (mission-driven).
- **Product track:** Product Manager / Senior PM at mission-driven
  products (donations, civic, edtech).

**Not a target:** pure engineering roles, sales/BD, very junior
(coordinator/assistant), VP/C-level requiring 15+ years.

## EXCLUDE_PATTERNS

These reduce the score by -15 to -25 even when the title superficially
matches a target role:

- Counter-terrorism, peacekeeping, drug control.
- Defence, gambling, tobacco, crypto/web3.
- Pure M&E (monitoring & evaluation) without programme design ownership.
- Roles requiring fluent German, French or Arabic at native level.
- US-only roles that don't accept European employees.
- AI safety research roles at non-prestigious orgs (cap at 35).

## HARD_FILTERS

<!--
HARD filters drop vacancies BEFORE the LLM ever scores them. They are
deterministic on/off rules, not score penalties. Use them only for things you
NEVER want to see — everything softer belongs in EXCLUDE_PATTERNS above (those
just lower the score).

Two fields, both comma-separated. Leave a field as "(none)" to disable it.

- exclude_countries: drop a vacancy if EVERY location it lists is in one of
  these countries. A multi-country posting that also lists a country you did
  NOT exclude is kept. Match is on the country name (e.g. "united states",
  "canada"). Empty by default — so by default NO vacancy is dropped on
  geography.

- exclude_title_keywords: drop a vacancy if its job TITLE contains one of these
  words (matched on whole words, so "engineer" does not hit "engineering
  manager" only if you list the exact word). Empty by default — so by default
  NO vacancy is dropped on its title discipline.

Example — a non-technical European searcher who never wants US/Canada roles or
engineering titles would write:

    exclude_countries: united states, canada
    exclude_title_keywords: engineer, developer, software engineer

The template below is EMPTY on purpose. Add your own only if you are sure.
-->

exclude_countries: (none)
exclude_title_keywords: (none)

## SHORT_SUMMARY_INSTRUCTION

A 4–6 sentence factual summary in **English** for the dashboard card.
Include: gist of the role and key responsibilities, required experience
and skills, seniority level, location and work mode, salary if mentioned,
and team/department if disclosed. Write factually, no candidate-fit
opinions.

## OUTPUT_LANGUAGE

English

## ABOUT_INSTRUCTION

5–8 sentences describing the company: mission, main lines of work, scale
and reach.

## CUSTOM_CRITERION_LABEL

Career narrative value

## CUSTOM_CRITERION_DESCRIPTION

How does this org look on your CV in 2 years? Reputable brand, growing
field, story you can tell at the next interview?

## CUSTOM_BOOST_FIELD

career_narrative_boost
