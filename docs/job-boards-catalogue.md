# Job boards catalogue

Built-in job-board aggregators you can enable as vacancy sources, alongside the
companies you track directly. Boards are **opt-in** and **off by default** — the
pipeline fetches only your tracked companies unless you turn boards on.

## How to enable

Boards are selected per run via the `JOB_BOARDS` environment variable (a
comma-separated list of board ids, or `all`):

```bash
JOB_BOARDS=80k_hours,idealist python3 -u scripts/fetch_vacancies.py   # these two
JOB_BOARDS=all                python3 -u scripts/fetch_vacancies.py   # every board
# JOB_BOARDS unset → no boards, only your tracked companies
```

All boards below are **free** (no API key, no login, no paid scraping). Each
ships with an empty `board_blacklist` on purpose — narrow a board to your
discipline/geography through your own profile's `exclude_title_keywords` and
`exclude_countries`, not by editing the board. Tune per-board knobs (pages,
queries, TTL) in `config/defaults.toml` under `[boards.*]`.

> **Fit is personal.** The "good for / skip if" notes below describe each
> board's *content*, not its quality. A board that is noise for one search is
> the main channel for another. Enable the ones whose coverage matches what
> you're looking for, then let scoring + your profile filters do the rest.

---

## Impact / mission-driven boards

### `80k_hours` — 80,000 Hours
High-impact / EA-aligned roles (global health, biosecurity, AI governance,
policy, effective philanthropy, operations at impact orgs). Curated and
generally senior. Powered by the board's own public Algolia search.
- **Coverage:** global, remote + onsite; impact sector only.
- **Good for:** operations, programme, research, and policy roles at
  mission-driven / EA organisations.
- **Skip if:** you want commercial-tech or location-specific mainstream roles.

### `idealist` — Idealist
The largest US-rooted nonprofit job board; broad social-sector coverage. This
fetcher defaults to **globally-open remote** roles (`remote_zone = "WORLD"`);
set `include_onsite = true` or change `remote_zone` to widen it.
- **Coverage:** nonprofit / social sector; default = worldwide-remote only
  (the wider corpus skews US-based).
- **Good for:** nonprofit operations, fundraising, programme, and grants roles
  that are open to remote candidates anywhere.
- **Skip if:** you need onsite roles in a specific country (without retuning) or
  commercial-sector jobs.
- **Note:** the public search key is embedded in idealist.org's page HTML and
  can rotate; if fetches start 403-ing, refresh it from the page source.

### `fast_forward` — Fast Forward
Tech-for-good board (Getro-hosted) spanning Fast Forward's accelerator network
and other nonprofit-tech employers worldwide. Listing-only by default
(`fetch_descriptions = false`) so a run doesn't make ~1.5k per-job calls; the
enrich pass fills descriptions later.
- **Coverage:** tech-enabled nonprofits, global; ~1.5k live roles.
- **Good for:** product, programme, engineering, and operations roles at
  social-impact tech organisations.
- **Skip if:** you only want a single country or non-tech sectors.

### `impactpool` — Impactpool
Multilateral / UN / international-development board (server-rendered HTML scrape,
listing-only; descriptions filled by the enrich pass).
- **Coverage:** UN agencies, NGOs, development banks, humanitarian orgs; global.
- **Good for:** international-development, humanitarian, and multilateral roles.
- **Skip if:** you want private-sector or purely remote tech roles. Expect a lot
  of M&E / field postings — filter by title if those aren't for you.

### `reliefweb` — ReliefWeb
UN OCHA's humanitarian jobs feed (official API).
- **Coverage:** humanitarian response, relief, and field roles; global.
- **Good for:** humanitarian / emergency-response careers.
- **Skip if:** you want HQ strategy/product roles — the feed is field- and
  M&E-heavy.

---

## General / commercial boards

### `linkedin` — LinkedIn (guest API)
LinkedIn's public guest job search — no login. Driven by a configurable list of
`{keywords, location}` queries in `config/defaults.toml`, so you point it at
exactly the roles and places you want (this single board replaces the old
separate "LinkedIn" and "LinkedIn Non-profits" sources — add nonprofit-slanted
queries to cover the latter). LinkedIn throttles aggressively, so the fetcher
spaces requests (`request_delay`) and keeps `pages` low.
- **Coverage:** anything on LinkedIn, scoped entirely by your queries.
- **Good for:** targeted searches by exact title + city/region; the most
  flexible board here.
- **Skip if:** you want a hands-off firehose — it needs a curated query set, and
  salary is rarely exposed.
- **Cost:** each query × page is one request, plus one per job for descriptions
  (`fetch_detail`). Keep the query list and `pages` modest to avoid rate limits
  and long runs.

### `arbeitnow` — Arbeitnow
Aggregator of German-market jobs.
- **Coverage:** ~100% Germany; ~3/4 German-language; trades, IT, sales, finance,
  DACH startups.
- **Good for:** German-speaking candidates targeting the German market.
- **Skip if:** you don't speak German or aren't targeting Germany.

### `remotive` — Remotive
Curated remote-work board (official API).
- **Coverage:** fully-remote roles, heavily software/QA/data/support, US- and
  LATAM-leaning; some gig/contract listings.
- **Good for:** remote engineers, data, and support pros open to global/contract
  work.
- **Skip if:** you need onsite/EU-located or non-tech leadership roles.

### `weworkremotely` — We Work Remotely
Remote-only board, fetched via per-category RSS (free, no key). Restrict which
categories are pulled with the `WWR_CATEGORIES` env var (comma-separated slugs,
e.g. `product,management-and-finance`) — the single biggest noise reducer.
- **Coverage:** remote-anywhere, engineering-heavy commercial tech; relocation /
  sponsorship essentially never offered.
- **Good for:** remote-first ICs and team leads in engineering, design,
  marketing, support, or commercial product who don't need sponsorship.
- **Skip if:** you need sponsorship/relocation or target mission-driven /
  public-sector roles.

### `hn_whoishiring` — Hacker News "Who is hiring?"
The monthly HN hiring thread (30-day TTL — refetches only when a new thread
appears).
- **Coverage:** overwhelmingly software-engineering at startups (often YC /
  seed / Series-A), US-heavy.
- **Good for:** software engineers and technical founders open to US-remote /
  relocation, applying direct-to-founder.
- **Skip if:** you want non-engineering, EU-located, or sponsorship-friendly
  roles, or clean structured metadata — postings are free text and parse
  imperfectly.

---

## Adding your own board

A board is four small pieces:
1. a `[boards.<id>]` block in `config/defaults.toml` (`strategy`, `name`, `url`,
   `ttl_days`, `tier`, `free`, `board_blacklist = []`);
2. a `fetch_<id>_board(board_cfg) -> list[dict]` function in
   `scripts/fetchers.py` returning dicts with keys `title, location, department,
   url, external_id, snippet, full_description, compensation, org_override,
   org_url`;
3. one `elif strategy == "<id>"` branch in the board dispatch in
   `scripts/fetch_vacancies.py`;
4. a unit test in `tests/test_board_fetchers.py` with mocked HTTP.

See the existing fetchers for templates (`fetch_idealist_board` for a JSON API,
`fetch_fastforward_board` for a paginated POST API, `fetch_linkedin_board` for
HTML-card scraping with rate-limit handling).
