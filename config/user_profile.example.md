# User profile

THE single human-editable source of your taste. Copy it to
`config/user_profile.md` (the real file is gitignored) and fill it in.
The whole pipeline reads from this one file:
- **Scoring** — `scripts/prompts.py` substitutes each `## SECTION` into the
  LLM scoring prompt (`{{SECTION_NAME}}`).
- **Filtering** — `scripts/hard_filters.py` reads `## HARD_FILTERS`
  (exclude_countries / exclude_title_keywords) to drop vacancies before
  scoring.
- **Product language** — `## OUTPUT_LANGUAGE` picks the ONE language of the
  whole product: the agent's replies in `/jobs-new` and `/jobs-review`, the run
  reports, the Telegram digest, and the dashboard's default. Change it here (or
  via `/jobs-profile`) and everything switches at once. `English`/`en` and
  `Russian`/`ru` ship with UI translations; any other value still sets the
  language of the scored text fields but leaves the UI chrome in English.

Machine mechanics you do NOT edit (thresholds, geo tables, job boards,
universal junk words) live in `config/defaults.toml`. Sections you don't
need can be left empty or removed.

## USER_PROFILE

Replace everything in this section with your own details. The `[brackets]` show
what goes where — delete the guidance once you have filled them in. Nothing here
is a default: your field is whatever you type.

**Name:** [your name]
**Current location:** [city, country]
**Target locations:** [where you'd work — cities, countries, or "remote-EU" style]
**Target start:** [e.g. within 6 months]
**Salary benchmark:** [target and hard floor in your currency — or leave blank]
**Visa status:** [e.g. "EU passport — no sponsorship needed", or "need sponsorship
for the EU/UK". The prompt uses this to weight visa-friendly employers.]

### Professional experience

One line per role: years, title, employer — plus one line on what you actually
did. Honest beats impressive; the scorer reads this. A few shapes across
different fields, so you can see the format (replace with your own):

- 2021–2026, [your title] at [employer] — [the result you're proudest of].
- 2018–2021, [earlier title] at [employer] — [scope: team size, budget, output].
- e.g. "Charge Nurse at [hospital] — ran a 20-bed ward, cut handover errors."
- e.g. "Backend Engineer at [company] — owned the payments service, 5→50 req/s."
- e.g. "Operations Manager at [org] — scaled ops from 30 to 120 people."

### Core skills (strongest first)

- [your strongest skill]
- [next skill]
- [and so on]
- Languages: [language (level), language (level)]

### What energises you

- [the kind of work you want more of — e.g. building 0→1, deep hands-on craft,
  leading people, owning a budget, a mission you care about]

### Domain preferences

**Want to work in:** [the fields you're targeting — any sector works: healthcare,
games, climate, fintech, developer tools, public policy, manufacturing, …].

**Open but not first choice:** [adjacent fields you'd still consider].

## TARGET_ROLES

The exact job titles you want to see — one per line or comma-separated. Any
field; pick your own. The lines below are only format examples from different
careers, not a default set — replace them:

- [your target title], [a more senior version], [an adjacent title]
- e.g. "Registered Nurse, Charge Nurse, Nurse Manager"
- e.g. "Backend Engineer, Senior Software Engineer, Staff Engineer"
- e.g. "Operations Manager, Head of Operations, Chief of Staff"

**Not a target:** [titles or levels to steer away from — e.g. very junior
(coordinator/assistant), or roles far outside your track].

## LINKEDIN_QUERIES

<!--
Search terms for the LinkedIn board (enable it with `sources.py enable-board
linkedin`). OPTIONAL — leave this section empty and the pipeline DERIVES queries
from your TARGET_ROLES above + your target locations, defaulting to Remote. Fill
it in only when you want exact control (specific phrasing, specific cities).

One query per line: `keywords | location`. The location is optional (blank =
LinkedIn's default, worldwide). Works for any field — a nurse might write
`ICU Nurse | Manchester`; a game designer `Level Designer | Remote`. The shipped
config carries NO queries: they always come from here or from your TARGET_ROLES,
never from a default someone else picked.

    Backend Engineer | Berlin
    ICU Nurse | Manchester
    Operations Manager | Remote

Leave the block empty (or delete it) to use the derived queries.
-->

## EXCLUDE_PATTERNS

<!--
FACTOR STRENGTH: penalty. Every taste factor you declare has a STRENGTH —
filter (blocks a role before scoring), penalty (subtracts points during
scoring), or note (display-only, never touches the score). The same factor is a
filter for one person and a penalty for another; choose the strength that fits.
This section holds your PENALTY factors: they are fed to the scorer and lower
the score, but a role can still surface if it is strong on everything else.
Hard blocks go in HARD_FILTERS below; display-only reminders go in NOTES.
The learning loop may PROPOSE moving a factor between strengths (e.g. a penalty
that never once collided with a role you liked → a hard filter); it never moves
one itself, and every move needs your yes.
-->

These reduce the score by -15 to -25 even when the title superficially matches a
target role. Put YOUR OWN here — the examples below span different fields on
purpose, so none is an exclusion you inherit by leaving it in:

- [a sub-area you'd rather avoid — e.g. "night-shift-only roles", "pure
  maintenance work", "on-call-heavy positions"].
- [an industry you'd score down but not hard-block — e.g. gambling, tobacco,
  defence, crypto — your call, not a shipped default].
- [a missing-skill signal — e.g. "requires a language you don't speak at native
  level"].
- [a seniority/scope mismatch — e.g. "individual-contributor only when you want
  to manage", or the reverse].

## NOTES

<!--
FACTOR STRENGTH: note. Display-only factors. A note NEVER reaches the scorer and
NEVER changes whether a role passes — it is only a highlight in the dashboard and
in explanations, a reminder to yourself. Put here the things you want flagged but
not judged on. (A nurse might note "night shifts" to see it called out without
letting it lower the score.) Leave empty if you have none. Because notes are
display-only, the scoring prompt is built WITHOUT this section — moving a factor
here is how you stop it influencing the score without deleting it.
-->

- [something to surface but not score on — e.g. "flag roles with frequent
  travel", "note fully-remote vs hybrid", "highlight a written-work culture"].

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

## COMPANY_TITLE_FILTERS

<!--
Per-company title INCLUDE-lists. HARD_FILTERS above is GLOBAL — a word banned
there is banned everywhere. This section is the opposite tool: keep a high-volume
company ACTIVE but let ONLY profile-relevant titles through to scoring. Use it
when a word is safe to kill at one org but a real fit elsewhere (so it can't go
in the global exclude_title_keywords).

One entry per line:

    - <canonical company name> :: <comma-separated include patterns>

Semantics: for a LISTED company, a role is DROPPED AT FETCH TIME unless it
matches at least one pattern (whole-word, case-insensitive — the same matching
the title blacklist uses). Dropped means never stored: the pipeline does not pay
to save, enrich, score or report it. Company names are alias-resolved, so a board
spelling ("WFP") still hits an include-list declared under the canonical name.
The filter stage keeps the same check as a safety net for rows stored before you
added the entry — it flags them as delete candidates with the reason
"company_title_filter — not in <Company> include list" (reviewable in
/jobs-review before anything is deleted). UNLISTED companies are completely
unaffected. A missing or empty section = feature OFF. A malformed line is skipped
with a warning.

Example — keep WFP and FHI 360 active but only surface programme/data roles:

    - World Food Programme :: monitoring, evaluation, programme officer, data
    - FHI 360 :: research, evaluation, data, strategy

MATCHING THE JOB BODY. A pattern matches the TITLE by default. Prefix it with
"desc:" to match the job DESCRIPTION instead:

    - WFP :: business innovation, product manager, desc:innovation accelerator

Use it when the thing you want is never in the title. A team, a unit or a product
that the employer names only in the body — "you will join our Innovation
Accelerator" — cannot be found any other way. The two scopes UNION: a role
survives when its title matches a title pattern OR its body matches a desc
pattern, so adding a desc pattern only ever widens what you keep.

Two cautions, both learned the hard way:

  * A body is long, so a body match is much looser than a title match. Employers
    name-drop their own famous units in boilerplate ("X was launched as a pilot of
    our Innovation Accelerator") in roles that have nothing to do with them. Check
    what a new desc pattern actually keeps before you trust it.
  * A desc pattern can never keep a role whose body we do not have. If the source
    gave no description, the title patterns alone decide. Nothing is let through
    on a body we could not read.

The template below is EMPTY on purpose. Add your own only if you are sure.
-->

## COMPANY_NEVER_FETCH

<!--
Companies you want NOTHING from. COMPANY_TITLE_FILTERS above keeps a company and
narrows it to a few titles; this is the blunt version — the whole company is
skipped before the request, so it costs no HTTP call, no Firecrawl credit and no
stored row.

One entry per line, a bare company name:

    - <canonical company name>

Semantics: a listed company is skipped whole at fetch time, and any role a JOB
BOARD lists under that company is dropped before the save too. The company STAYS
in the registry — its history, aliases and notes survive, and the ban is one line
to undo. Rows stored before you added it are left alone; archive them yourself if
you want them gone. Names are alias-resolved, same as COMPANY_TITLE_FILTERS.
UNLISTED companies are completely unaffected. A missing or empty section =
feature OFF. A malformed line — an empty name, or a line still carrying ":: "
patterns from the sibling section — is skipped with a warning.

Example:

    - Some Agency That Only Posts Driver Jobs
    - A Consultancy You Will Never Join

The template below is EMPTY on purpose. Add your own only if you are sure.
-->

## VOLUME

<!--
Cost/volume knobs tied to YOUR plan tier. The scoring model is the main cost
dial; the per-run cap is a spike-day safety net.

Scoring runs in two passes: a cheap model SCREENS every new vacancy, then the
strong model RE-SCORES only the finalists that clear a floor. Everything else
keeps its cheap score, sorted out of view.

- scoring_model: the STRONG model tier that scores the finalists — haiku | sonnet
  | opus. This is the main cost dial: match it to your plan — a budget plan (~$20)
  → sonnet; a bigger plan (~$100-200) → opus. Empty/omitted → sonnet (the cheap
  default).
- screen_model: the CHEAP model that gives every new vacancy a fast first score —
  haiku | sonnet | opus. Only roles that clear escalate_threshold are re-scored by
  scoring_model. Empty/omitted → haiku (the cheapest tier). Clamped so it can
  never cost more than scoring_model (a screen as dear as the final pass would
  erase the saving).
- escalate_threshold: the screen-score floor (0-100) at/above which a role is
  escalated to the strong model. Calibrated so the cheap screen drops none of the
  roles the strong model would surface; the default (50) also diverts the weak
  majority. Lower it for more caution (more escalations, less saving); raise it to
  save more. Empty/omitted or out of range → 50. Honest tradeoff: raising it above
  the dashboard's visible match band (APPLYABLE_SCORE, default 60) means kept-cheap
  screen scores start showing up as if they were confirmed matches — the
  dashboard's "screen score" badge (from vacancy.scored_by) is what keeps them
  distinguishable, so don't turn that badge off if you raise this. A value near
  100 effectively disables the strong pass entirely (real screen scores rarely
  reach it) — the driver warns loudly if you set it that high.
- max_per_run: the most vacancies (and candidate companies) to score in one run
  when you don't pass --limit. A quiet day scores 20-30; this cap keeps a burst
  day (hundreds of new roles) from silently draining your plan — the overflow is
  offered on the next run, and the run prints "scored X of Y". In the two-pass
  flow the cap bounds the SCREEN set (the strong pass is a subset). This PERSONAL,
  plan-tier value OVERRIDES the neutral `[volume] daily_scoring_limit` in
  config/defaults.toml; leave it empty/omitted to inherit that shared default
  (150). A non-positive value also falls back to the shared default.

The neutral "how many do I see" dials (how many active companies fetch per run,
the shared scoring limit, the digest size) live in one place —
config/defaults.toml `[volume]` — and `/jobs-new` prints the current values at
run start. This section holds only the parts tied to YOUR plan (model tier + the
per-run cap).
-->

scoring_model: sonnet
screen_model: haiku
escalate_threshold: 50
max_per_run: 150

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

## CASE_BANK

<!--
Your case bank is a private folder of personal stories, worked examples and
typical answers the agent pulls from when it helps you draft a cover letter or
answer application questions — so you tell your OWN stories, not generic filler.
It sits in the gitignored private zone next to this profile; nothing personal
ever enters git or the public dashboard.

It is a CONFIG KEY, never a hardcoded path. Point JOBSEARCH_PRIVATE_DIR at your
private space (e.g. a separate private repo); the default is a gitignored
`private/` under the project root, holding:

    private/case_bank/       <- your stories / cases / typical answers
    private/applications/    <- the CV versions, cover letters and answers sent

What lives in the case bank is entirely yours and sector-neutral:
- a nurse: a de-escalation story, a patient-safety catch, a night-shift example;
- a game designer: a shipped-feature retro, a playtest-driven redesign, a jam win;
- a policy analyst: a brief that changed a decision, a stakeholder negotiation.

This section is just a reminder; the files themselves are private. Fill the
folder with one short story per file and the agent will reach for them by name
when you run `/jobs-review apply`.
-->

(your case bank lives in JOBSEARCH_PRIVATE_DIR/case_bank — see the note above)

## BUG_TRACKER

<!--
Where /jobs-new files the bugs it finds during a run. Every run writes a bug log
to docs/jobs-new-bugs-<date>.md; at the end it also opens ONE item in the tracker
you name here, linking that log.

LEAVE THIS EMPTY (the shipped default) and there is no tracker step — the dated
log file is the record. Fill it only if you actually track issues somewhere.

Free text: name the tool and the exact destination so the agent knows where to
file. Examples:
  - "Linear, team <Your Team> (key <KEY>)"
  - "GitHub issues in <owner>/<repo>, label: pipeline-bug"
  - "Obsidian, append to vault/inbox/jobs-bugs.md"
-->

(empty — bugs stay in the dated log file only)
