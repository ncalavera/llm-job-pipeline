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
HARD filters shape geography and titles around scoring. Geography is region-
based: ban whole world regions, whitelist exceptions, and softly penalise
on-site roles outside your preferred regions. Region ids come from
defaults.toml [geo.country_region]: europe, north_america, latin_america,
middle_east, africa, south_asia, southeast_asia, east_asia, ex_ussr, oceania.
Leave any field "(none)" / empty to disable it. Everything is EMPTY by default,
so out of the box NOTHING is dropped or penalised on geography.

- ban_regions: drop a vacancy when EVERY location sits in one of these regions.
  Remote roles are always kept; whitelisted countries below survive.
- keep_countries: countries that override a region ban (e.g. keep "georgia"
  though "ex_ussr" is banned).
- ban_countries: extra explicit country bans on top of the regions.
- ban_us_only: yes/no — drop roles the scorer flags us_only (US/Canada-residency
  bound, unreachable from abroad). Default no.
- onsite_ok_regions: regions where an ON-SITE role gets NO soft penalty (remote
  is never penalised). Everything else on-site loses onsite_penalty points.
- onsite_penalty: integer points subtracted from an on-site role outside
  onsite_ok_regions (0 = no soft penalty).
- exclude_countries: legacy exact-country ban (still works; prefer ban_regions).
- exclude_title_keywords: drop a vacancy if its TITLE contains one of these words
  (whole-word match).

Example — a Europe-based searcher who can't relocate outside the EU, won't take
US/Canada-only roles, and prefers remote elsewhere would write:

    ban_regions: africa, south_asia, southeast_asia
    keep_countries: (none)
    ban_us_only: yes
    onsite_ok_regions: europe
    onsite_penalty: 15
    exclude_title_keywords: engineer, developer, software engineer

The template below is EMPTY on purpose. Add your own only if you are sure.
-->

ban_regions: (none)
keep_countries: (none)
ban_countries: (none)
ban_us_only: no
onsite_ok_regions: (none)
onsite_penalty: 0
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
