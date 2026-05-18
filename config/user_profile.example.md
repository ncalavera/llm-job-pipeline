# User profile

This file is read by `scripts/prompts.py` and substituted into the scoring
prompts. Copy it to `config/user_profile.md` (the real file is gitignored)
and fill it in for yourself.

Each `## SECTION` block becomes a `{{SECTION_NAME}}` placeholder in the
prompt templates. Sections you don't need can be left empty or removed.

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
