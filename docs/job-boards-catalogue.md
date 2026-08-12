# Job boards catalogue

Besides the companies you track, the pipeline can pull from a set of **built-in
job boards** — all free public APIs or feeds, no key. This page is the single
source of truth for that list: the table below is generated from
`config/defaults.toml` (`[boards.*]`) by `scripts/gen_board_table.py`, so its
count and per-board audience can never drift from the code. A guard test
(`tests/test_board_catalogue_matches_config.py`) fails CI if they do.

## How boards behave

- **Opt-in, off by default.** No board fetches until you enable it. Each is
  niche and floods a general search when it does not match your field, so the
  neutral default is: only your tracked companies are fetched.
- **Enable one so it sticks:** `python3 scripts/sources.py enable-board <id>`
  (or `/jobs-add board`). It persists in the DB and then fetches on every
  `/jobs-new` — no env var to remember. `python3 scripts/sources.py` lists what
  is enabled; `disable-board <id>` turns one off.
- **Not sure which fit you?** `python3 scripts/sources.py recommend` proposes the
  boards that match *your* profile (target field, roles, geography) and only
  suggests — it never enables anything for you.
- **One-off override:** `--boards "a,b,c"` on `run_daily.py` (or the `JOB_BOARDS`
  env var on a direct `fetch_vacancies.py` run) adds boards for a single run, on
  top of the persisted set.
- **Neutral by default:** every board ships an empty `board_blacklist`. Any
  discipline/geography exclusion is your own choice in
  `config/user_profile.md` (`exclude_title_keywords`), never baked into a board.

## Per-board env knobs

A few boards take optional narrowing via env vars:

| Board | Extra env |
| --- | --- |
| `arbeitnow` | `ARBEITNOW_VISA_ONLY=1` keeps only visa-sponsorship jobs |
| `remotive` | `REMOTIVE_CATEGORIES=product,marketing` narrows by category slug |
| `weworkremotely` | `WWR_CATEGORIES=product,marketing` picks the category RSS feeds |
| `linkedin` | queries come from your profile (`## LINKEDIN_QUERIES`, else derived from `## TARGET_ROLES` + geography) — the shipped config carries none |

## The boards

<!-- BEGIN AUTO-GENERATED BOARD TABLE (scripts/gen_board_table.py) -->
The pipeline ships **16 built-in job boards**. Every one is opt-in (off by default) and free (public API/feed, no key).

| ID | Board | Who it fits |
| --- | --- | --- |
| `80k_hours` | 80,000 Hours | Effective altruism, AI safety, global-priorities policy and research. |
| `reliefweb` | ReliefWeb | Humanitarian relief and international-development NGOs. |
| `arbeitnow` | Arbeitnow | European tech; remote roles and (when listed) visa-sponsorship jobs. |
| `remotive` | Remotive | Remote-first roles across engineering, product, design, marketing, support. |
| `weworkremotely` | We Work Remotely | Remote jobs across programming, design, ops, marketing, sales, support. |
| `hn_whoishiring` | HN Who is hiring | Startups, engineering-heavy; the monthly Hacker News hiring thread. |
| `impactpool` | Impactpool | Nonprofit, UN and multilateral / international-organisation roles. |
| `datadotorg` | data.org | Data-for-social-impact and AI-for-good: data science / analytics roles. |
| `idealist` | Idealist | Nonprofit and social-impact jobs: programmes, advocacy, community, ops. |
| `fast_forward` | Fast Forward | Tech-nonprofits: engineering, product and ops at mission-driven startups. |
| `linkedin` | LinkedIn | Any field — queries are built from your profile's target roles + geography. |
| `a16z` | a16z Portfolio | Venture-backed startup roles (a16z portfolio): engineering, product, GTM. |
| `sequoia` | Sequoia Portfolio | Venture-backed startup roles (Sequoia portfolio): engineering, product, GTM. |
| `consultants_for_impact` | Consultants for Impact | Strategy / management consultants moving into social-impact work. |
| `probablygood` | Probably Good | High-impact roles across global health, biosecurity, AI safety, and nonprofit operations. |
| `ea_opportunities` | EA Opportunities Board | Roles, fellowships and internships at effective altruism organisations, run by the Centre for Effective Altruism. |
<!-- END AUTO-GENERATED BOARD TABLE -->

> **Portfolio boards `a16z` and `sequoia`** are defined so they appear in the
> dashboard's Boards tab, but their `consider_board` fetcher is not wired in
> this repo — they stay off and enabling them fetches nothing until it is
> restored.

Boards behind a login (Devex and similar) ship no importer on purpose. If you
accept the terms-of-service risk, ask your agent to write a personal importer
that feeds `save_vacancies()`, and keep it out of public forks.
