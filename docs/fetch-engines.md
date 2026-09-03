# Fetch engines

Every fetch **strategy** the pipeline can dispatch on: what surface it hits, the
config keys it needs, its pagination/caps and politeness, its honest failure
signatures, and a read-only one-liner to test one source by hand. This is the
reference for the person debugging a dead fetch at 11pm and for an agent dropped
in with zero context.

Scope — this page is the **engine reference** (how fetching works, keyed by the
`strategy` string). It is **not** the board directory: which boards ship, who
each fits, and how to enable one live in
[`job-boards-catalogue.md`](job-boards-catalogue.md) (generated from
`config/defaults.toml`, so its count and audiences can never drift). One engine
can back several board IDs — the [board-ID → engine crosswalk](#board-id--engine-crosswalk)
below is the bridge. The pipeline these engines feed is in
[`ARCHITECTURE.md`](ARCHITECTURE.md); the design rules in [`STRATEGY.md`](../STRATEGY.md).

Two registries, filled at import by decorators in `scripts/fetchers/` (one file
per source):

- **`COMPANY_FETCHERS`** — `strategy → fn(org_name, config)`, one per ATS
  provider under `fetchers/ats/`. Registered with `@register_company("<strategy>")`.
- **`BOARD_FETCHERS`** — `strategy → fn(board_cfg)`, one per job board under
  `fetchers/boards/`. Registered with `@board_fetcher("<strategy>")`.

Counts here are asserted against those registries by
`tests/test_fetch_engines_doc.py` — a registered strategy with no section fails
CI (STRATEGY guardrail 4: a doc claim that doesn't match code is a crash-severity
bug).

## How a fetch is dispatched (shared mechanics)

**Company config shape.** `company_registry._build_companies_from_db()` turns a
`company` row into the `config` dict every company engine receives:

| Source | Becomes | Notes |
| --- | --- | --- |
| `fetch_strategy` column | `config["strategy"]` | selects the engine |
| `ats_slug` column | `config["slug"]` | the provider's board id (when set) |
| `careers_url` column | `config["careers_url"]` | also copied to `config["url"]` for `firecrawl_scrape` / `unops_widget` |
| `ats_config` JSONB | merged flat into `config` | every complex key below (`tenant`, `board`, `base_url`, `sf_backend`, `cid`, `queries`, `url_filter`, …) lives here |
| `tier`, `status` | `config["tier"]`, `config["status"]` | |

So when an engine below wants `sf_backend: "sitemap"` or `tenant`/`board`, that
key is a field of the company's `ats_config` JSON. `slug` is the `ats_slug`
column. Config validation lives in `company_registry.validate_company_config`.

**Board config shape.** A board engine receives the `[boards.<id>]` table from
`config/defaults.toml` verbatim (`board_cfg`); its `strategy` key picks the
engine. Every board also carries `name`, `url`, `ttl_days`, `tier` and an
(empty by default) `board_blacklist`.

**Error boundary — refusal ≠ empty (the "silent zero" rule, see `CONCEPTS.md`).**
Every engine is wrapped
by `fetchers.registry._guard`: on any exception it **prints, records a
`fetch_status`, and returns `[]`** — one dead source never crashes the run. The
reason is preserved so an *empty* result (`[]`, status `ok`) stays
distinguishable from a *failed* one:

- A typed `FetchError(reason, detail)` → status `error: <reason>` where `reason`
  is `timeout`, `http_<code>`, `network`, or an engine-specific code
  (`not_a_feed`, `sitemap_index`, `blocked`, `unparseable`, …).
- Any other exception → status `error: <exception>`.
- `get_fetch_errors()` returns the per-source map for the run.

The rule engines follow: if **nothing** was fetched *and* a request failed, they
re-raise so the boundary records the reason; a genuinely empty source returns
`[]` and reads as an honest `ok`.

**HTTP skeleton.** `fetchers.http.get/post` forward to `requests` (resolved
through `fetchers.requests` so tests monkeypatch one surface), raise `FetchError`
on transport errors, and with `check=True` (default) raise on HTTP ≥400. Pass
`check=False` to inspect status yourself (redirect probing, 429 back-off). A
browser-like `_LOCAL_UA` is sent where a site blank-pages bots.

**Blind engines + enrichment.** Several list feeds carry no job-ad body
(`smartrecruiters`, `adp_json`, `impactpool_html`, `cfi_board_json`, listing-only
`fastforward_board`, and `linkedin_guest` when detail is throttled). They return
rows with an empty `full_description` on purpose. Two enrichment surfaces fill
them later, both credit-guarded and both no-ops if `FIRECRAWL_API_KEY` is unset:

- **Inline** — `fetchers.firecrawl._enrich_blind_jobs`, called at fetch time by
  `workable` and `firecrawl_scrape`; scrapes each blind job URL, skipping
  blacklisted titles.
- **The `enrich` stage** — `scripts/enrich_blind_vacancies.py` (STAGE_ORDER step
  6), runs after fetch over blind DB rows. It routes server-rendered hosts to
  zero-credit direct fetchers and **skips `linkedin.com` hosts entirely**
  (`_is_unscrapable_host`; LinkedIn blocks scrapers — verified 2026-07-03 a guest
  page returns 0 chars, so spending credits is pure waste; those rows heal on the
  next fetch or age out via the stale-blind sweep).

## Reading an entry

Each engine below lists: **Surface / auth**, **Config keys** (req vs opt),
**Pagination & caps**, **Failure signatures** (what you actually see), and a
**Debug** call. Run any Debug snippet under the [debug harness](#debug-harness)
preamble — it forces the local SQLite demo backend at a throwaway path, so the
recipes read the network but write nothing to any real DB.

---

## Company engines (`COMPANY_FETCHERS`, 17)

### `greenhouse` — Greenhouse boards API
<!-- ENGINE: greenhouse -->
- **Surface / auth:** `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` (EU instance: `boards-api.eu.greenhouse.io`). Public, no auth. `?content=true` returns the description inline — no per-job fetch.
- **Config keys:** `slug` (req). `eu: true` (opt, `ats_config`) for EU-hosted boards.
- **Pagination & caps:** none — one request returns the whole board.
- **Failure signatures:** unknown slug / disabled board → HTTP 404 → `error: http_404`. Any ≥400 → `error: http_<code>`. Empty board → `[]`, `ok`.
- **Debug:** `from fetchers.ats.greenhouse import fetch_greenhouse; print(len(fetch_greenhouse("Example", "<slug>")))`

### `lever` — Lever postings API
<!-- ENGINE: lever -->
- **Surface / auth:** `GET https://api.lever.co/v0/postings/{slug}?mode=json` — a flat JSON array. Public, no auth. Descriptions inline (`descriptionPlain`).
- **Config keys:** `slug` (req).
- **Pagination & caps:** none (single array).
- **Failure signatures:** ≥400 → `error: http_<code>`; timeout → `error: timeout`.
- **Debug:** `from fetchers.ats.lever import fetch_lever; print(len(fetch_lever("Example", "<slug>")))`

### `ashby` — Ashby job-board API
<!-- ENGINE: ashby -->
- **Surface / auth:** `GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`. Public, no auth. Descriptions + comp inline.
- **Config keys:** `slug` (req).
- **Pagination & caps:** none.
- **Failure signatures:** ≥400 → `error: http_<code>`.
- **Debug:** `from fetchers.ats.ashby import fetch_ashby; print(len(fetch_ashby("Example", "<slug>")))`

### `workable` — Workable widget API
<!-- ENGINE: workable -->
- **Surface / auth:** `GET https://apply.workable.com/api/v1/widget/accounts/{slug}`. Public, no auth. The list carries only `shortDescription`, so rows are **blind** and get inline Firecrawl enrichment (`_enrich_blind_jobs`).
- **Config keys:** `slug` (req).
- **Pagination & caps:** none for the list; enrichment scrapes each non-blacklisted job URL.
- **Failure signatures:** ≥400 → `error: http_<code>`. No Firecrawl key → rows keep the short snippet, `full_description` stays empty (no crash).
- **Debug:** `from fetchers.ats.workable import fetch_workable; print(len(fetch_workable("Example", "<slug>")))` (enrichment fires only if `FIRECRAWL_API_KEY` is set)

### `recruitee` — Recruitee offers API
<!-- ENGINE: recruitee -->
- **Surface / auth:** `GET https://{slug}.recruitee.com/api/offers/`. Public, no auth. Full HTML descriptions, city/country, remote/hybrid, salary inline.
- **Config keys:** `slug` (req).
- **Pagination & caps:** none.
- **Failure signatures:** ≥400 → `error: http_<code>`.
- **Debug:** `from fetchers.ats.recruitee import fetch_recruitee; print(len(fetch_recruitee("Example", "<slug>")))`

### `pinpoint` — Pinpoint postings feed
<!-- ENGINE: pinpoint -->
- **Surface / auth:** `GET https://{slug}.pinpointhq.com/postings.json`. Public, no auth. One `data` array of live postings; each carries an inline HTML `description`, a structured `location` dict, a `job.department`, and a pre-formatted `compensation` string.
- **Config keys:** `slug` (req; the hosted-board subdomain, i.e. `{slug}.pinpointhq.com`).
- **Pagination & caps:** none — the feed returns every live posting in one request (`?page=` is ignored).
- **Failure signatures:** unknown/disabled board → HTTP 404 → `error: http_404`. Any ≥400 → `error: http_<code>`. Empty board → `[]`, `ok`. `compensation` is shown only when the posting's `compensation_visible` is true.
- **Debug:** `from fetchers.ats.pinpoint import fetch_pinpoint; print(len(fetch_pinpoint("Example", "<slug>")))`

### `adp_json` — ADP Workforce Now requisitions feed
<!-- ENGINE: adp_json -->
- **Surface / auth:** `GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions?cid={cid}` (`Accept: application/json` + `_LOCAL_UA`). Public, no auth. The feed has no body, so the snippet is built from location + pay range (no per-job detail fetch — these boards are low-fit/US-only).
- **Config keys:** `cid` resolved in order from `ats_slug` → `ats_config.cid` → `cid`. `careers_url`/`url` (opt) is reused to build the per-requisition portal link.
- **Pagination & caps:** none — single request.
- **Failure signatures:** no `cid` → prints "no cid configured", returns `[]` (empty, not an error). ≥400 → `error: http_<code>`.
- **Debug:** `from fetchers.ats.adp import fetch_adp_json; print(len(fetch_adp_json("Example", {"ats_slug": "<cid>"})))`

### `bamboohr` — BambooHR careers API (two-phase)
<!-- ENGINE: bamboohr -->
- **Surface / auth:** list `GET https://{slug}.bamboohr.com/careers/list` (`Accept: json`, `allow_redirects=False`), then per-job detail `GET .../careers/{id}/detail`. Public, no auth.
- **Config keys:** `slug` (req).
- **Pagination & caps:** none for the list; detail loop paced at 0.3 s/request.
- **Failure signatures:** a 3xx redirect on the list → prints "account likely disabled, update fetch_strategy" and returns `[]` (empty — the account moved off BambooHR). Non-JSON content-type → prints "expected JSON", `[]`. ≥400 → `error: http_<code>`. A single detail failure is caught (that job's description stays empty).
- **Debug:** `from fetchers.ats.bamboohr import fetch_bamboohr; print(len(fetch_bamboohr("Example", "<slug>")))`

### `smartrecruiters` — SmartRecruiters postings API
<!-- ENGINE: smartrecruiters -->
- **Surface / auth:** `GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?offset=&limit=100`. Public, no auth. List-only (no body) → **blind**, enriched later.
- **Config keys:** `slug` (req; the company id, same string as in `jobs.smartrecruiters.com/{slug}/...`).
- **Pagination & caps:** `offset`/`limit=100`, **hard cap 20 pages**, stops at `totalFound`.
- **Failure signatures:** no slug → prints, `[]`. ≥400 → `error: http_<code>`.
- **Debug:** `from fetchers.ats.smartrecruiters import fetch_smartrecruiters; print(len(fetch_smartrecruiters("Example", "<slug>")))`

### `teamtailor_rss` — Teamtailor RSS feed
<!-- ENGINE: teamtailor_rss -->
- **Surface / auth:** `GET https://{host}/jobs.rss` — RSS 2.0. `host` is the custom `careers_url` domain first (e.g. a `careers.<org>.org`), then the default `{slug}.teamtailor.com`. Public, no auth.
- **Config keys:** `slug` (req). `careers_url` (opt) — a custom career-site domain that publishes the feed.
- **Pagination & caps:** none.
- **Failure signatures:** a custom domain that answers `{host}/jobs.rss` with a 200 HTML SPA/marketing page (not a feed) is rejected and the next host is tried; if the **last** host is also a non-feed → `error: not_a_feed`. A transport error on a non-last host → try next; on the last → re-raised. A valid but **empty** feed is accepted (honest `[]`, not a failure).
- **Debug:** `from fetchers.ats.teamtailor import fetch_teamtailor_rss; print(len(fetch_teamtailor_rss("Example", "<slug>")))`

### `successfactors` — SAP SuccessFactors (two backends)
<!-- ENGINE: successfactors -->
- **Surface / auth:** picked by `sf_backend`:
  - default **`csb`** — Career Site Builder tile feed `GET <base>/tile-search-results/?q=&startrow=N` (25 tiles/page). The tile endpoint returns a 16-byte empty shell without a session cookie, so a `GET <base>/search/` runs first to establish one. Returns an **HTML fragment** of `<li class="job-tile …">` (despite the "tile-search" name), not JSON.
  - **`sitemap`** — for hosts whose CSB feed is a dead end (e.g. an org on the classic RCM backend): `GET <base>/sitemap.xml` then fetch each job detail page (schema.org `JobPosting` microdata).
- **Config keys:** `url` (opt — a full base host, e.g. a `career5.successfactors.eu` portal) **or** `ats_slug`/`slug` (builds `https://jobsearch.createyourowncareer.com/{site}`). `sf_backend: "sitemap"` (opt, `ats_config`) to switch backends.
- **Pagination & caps:** csb — `startrow += 25`, **hard cap 20 pages**, stops on an empty/all-seen page. sitemap — **caps detail fetches at 500**.
- **Failure signatures:** no url/slug → prints, `[]`. sitemap is a `<sitemapindex>` (nested) not a flat `<urlset>` → `error: sitemap_index` (fails loud rather than silently returning zero). A csb page error with no jobs yet → re-raised; after some jobs → stops and keeps what it has.
- **Debug:** `from fetchers.ats.successfactors import fetch_successfactors; print(len(fetch_successfactors("Example", {"url": "https://jobsearch.createyourowncareer.com/<Site>"})))`

### `workday_api` — Workday CXS JSON API (two-phase)
<!-- ENGINE: workday_api -->
- **Surface / auth:** list `POST {base_url}/wday/cxs/{tenant}/{board}/jobs` (JSON body, metadata only), then per-job detail `GET {base_url}/wday/cxs/{tenant}/{board}{externalPath}`. Undocumented but public, no auth; browser UA.
- **Config keys:** `tenant`, `board`, `base_url` (all req; from `ats_config`). `url_prefix`, `search_text` (opt) — `url_prefix` fixes per-job URLs when Workday omits the board segment (those 404 otherwise).
- **Pagination & caps:** `limit=20` (the API rejects more), `offset += len(page)` until `total`. Detail loop paced at 0.25 s; detail failures caught (description left empty, scored by title).
- **Failure signatures:** ≥400 on the list → `error: http_<code>`.
- **Debug:** `from fetchers.ats.workday import fetch_workday_api; print(len(fetch_workday_api("Example", "<tenant>", "<board>", "https://<tenant>.wd1.myworkdayjobs.com")))`

### `unops_widget` — UNOPS careers widget
<!-- ENGINE: unops_widget -->
- **Surface / auth:** `GET <url>` (the official careers widget), then per-job detail pages under `https://careers.unops.org/...`. Public, no auth.
- **Config keys:** `url` (req; defaults to `careers_url`). Opt (`ats_config`): `title_blacklist`, `seniority_filter`, `location_keywords`, `fetch_descriptions` (default `true`).
- **Pagination & caps:** widget returns all open cards; detail loop paced at 0.3 s.
- **Failure signatures:** expired postings are dropped by deadline; a gone job that 302s to `/careersmarketplace/Error` returns an empty description (guarded so the error page is never saved as a body). ≥400 on the widget → `error: http_<code>`.
- **Debug:** `from fetchers.ats.unops import fetch_unops_widget; print(len(fetch_unops_widget("Example", "<widget-url>", fetch_descriptions=False)))`

### `oracle_hcm` — Oracle HCM Recruiting Cloud REST API
<!-- ENGINE: oracle_hcm -->
- **Surface / auth:** `GET <host>/hcmRestApi/resources/latest/recruitingCEJobRequisitions?finder=findReqs;siteNumber="<SITE>",keyword="…",limit,offset` (paged search), then `recruitingCEJobRequisitionDetails?finder=ById;Id="<Id>",siteNumber="<SITE>"` per job. Public, no auth; the finder's inner quotes are literal (`%22`).
- **Config keys:** `url` (req; defaults to `careers_url`) — any careers URL of the site, e.g. `https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs?keyword=accelerator%20lab`; host and site number are parsed from it. Opt (`ats_config`): `keyword`, which overrides the URL's own `?keyword=`.
- **Pagination & caps:** `offset` in steps of 50 until `TotalJobsCount` is reached or a page comes back empty; hard cap 20 pages (1000 roles). Every listed job costs one detail GET, so a site without a keyword is expensive — UNDP lists hundreds.
- **Failure signatures:** a url with no host or no `/sites/<SITE>` → prints and returns `[]` without a request. A search page ≥400 → `error: http_<code>`. A single detail failing is printed with its reason and detail, and that job falls back to its search-result fields (empty description) instead of killing the listing.
- **Debug:** `from fetchers.ats.oracle_hcm import fetch_oracle_hcm; print(len(fetch_oracle_hcm("Example", {"url": "<careers-url>"})))`

### `amazon_jobs` — Amazon Jobs search API
<!-- ENGINE: amazon_jobs -->
- **Surface / auth:** `GET https://www.amazon.jobs/en/search.json?base_query=…&offset=&result_limit=100`. Public, no auth.
- **Config keys:** `queries` (or `base_query`) — a comma list; default `"nonprofit,social impact"` (`ats_config`). Results are AND-matched per query and deduped across queries.
- **Pagination & caps:** `offset`/`limit=100` per query, stops at `hits`.
- **Failure signatures:** a per-query error is caught and that query is skipped; only if **every** query fails and nothing was fetched → the last error is re-raised (`error: http_<code>` / `error: timeout`).
- **Debug:** `from fetchers.ats.amazon import fetch_amazon_jobs; print(len(fetch_amazon_jobs("Example", {"queries": "data"})))`

### `apple_jobs` — Apple Jobs API (CSRF + POST)
<!-- ENGINE: apple_jobs -->
- **Surface / auth:** `GET https://jobs.apple.com/api/csrfToken` → `X-Apple-CSRF-Token`, then `POST https://jobs.apple.com/api/role/search` with that token (a `requests.Session` carries cookies). Free but CSRF-gated.
- **Config keys:** `query` (opt, default `"social impact nonprofit"`; `ats_config`).
- **Pagination & caps:** `page++` until `totalRecords`.
- **Failure signatures:** CSRF request fails → `error: network`. No token in the response → prints, `[]`. A search page failing with nothing fetched → re-raised as `error: network`.
- **Debug:** `from fetchers.ats.apple import fetch_apple_jobs; print(len(fetch_apple_jobs("Example", {"query": "data"})))`

### `firecrawl_scrape` — Firecrawl careers-page scrape (+ zero-cost fallbacks)
<!-- ENGINE: firecrawl_scrape -->
- **Surface / auth:** the company's careers `url`, scraped via the Firecrawl SDK (JSON extraction + markdown + changeTracking, ~5 credits). Fallback chain when credits/SDK are unavailable: local `requests`→markdown scraper; PageUp XHR for `optionsFacetsDD`/`/filter/?` URLs; Wagtail API for `/api/v2/pages/` URLs; legacy CLI. **Auth:** `FIRECRAWL_API_KEY` for the SDK; the local fallbacks need no key.
- **Config keys:** `url` (req; defaults to `careers_url`). `url_filter` (opt regex, `ats_config`) to keep only matching job links.
- **Pagination & caps:** a once-per-run credit check (`GET /v2/team/credit-usage`) short-circuits to the local scraper when exhausted; blind enrichment paced at 0.5 s/job; a `changeTracking: same` page returns `[]` (skipped, unchanged).
- **Failure signatures:** these set a **scrape-status override** (`get_scrape_statuses()`), not a `FetchError` — an empty result stays honest: `credit_exhausted` (credits gone → local scraper) and `js_required` (a JS-shell: thin text / no links, or the parser finds no rows). A quota/rate error from the SDK (402/429) flips credits to 0 for the rest of the run.
- **Debug:** `from fetchers.firecrawl import fetch_firecrawl_scrape; print(len(fetch_firecrawl_scrape("Example", "<careers-url>")))` (no `FIRECRAWL_API_KEY` → exercises the local scraper)

---

## Board engines (`BOARD_FETCHERS`, 15)

Every board applies `GLOBAL_BLACKLIST` and drops generic pipeline titles; all
but `firecrawl_board` also apply their own (empty-by-default) `board_blacklist`.
None applies a role or geography filter — LLM scoring decides relevance
(STRATEGY guardrail 1). Board de-duplication lives only in the save layer, never
here.

### `algolia_api` — generic Algolia index (backs 80,000 Hours)
<!-- ENGINE: algolia_api -->
- **Surface / auth:** `POST https://{app_id}-dsn.algolia.net/1/indexes/{index}/query` with the board's public search key in headers. No Firecrawl.
- **Config keys:** `algolia_app_id`, `algolia_api_key`, `algolia_index` (all req), plus `name`/`url`. `board_blacklist` opt.
- **Pagination & caps:** `hitsPerPage=200`, pages through `nbPages`. **No cap, no keyword/location filter.**
- **Failure signatures:** a per-page error breaks the loop; if nothing was fetched → re-raised (`error: …`).
- **Debug:** `from fetchers.boards.algolia import fetch_algolia_board; print(len(fetch_algolia_board({"name":"80,000 Hours","url":"https://jobs.80000hours.org","algolia_app_id":"W6KM1UDIB3","algolia_api_key":"d1d7f2c8696e7b36837d5ed337c4a319","algolia_index":"jobs_prod_super_ranked"})))`

### `arbeitnow_api` — Arbeitnow job-board API
<!-- ENGINE: arbeitnow_api -->
- **Surface / auth:** `GET https://www.arbeitnow.com/api/job-board-api?page=N` — JSON. No key. European tech; `remote`/`visa_sponsorship` flags.
- **Config keys:** `pages` (opt, default 3), `board_blacklist`. Env `ARBEITNOW_VISA_ONLY=1` keeps only visa-sponsorship jobs.
- **Pagination & caps:** up to `pages`, stops when there's no `links.next`.
- **Failure signatures:** a page error breaks; nothing fetched + error → re-raised.
- **Debug:** `from fetchers.boards.arbeitnow import fetch_arbeitnow_board; print(len(fetch_arbeitnow_board({"name":"Arbeitnow","url":"https://www.arbeitnow.com","pages":1})))`

### `cfi_board_json` — Consultants for Impact static feed
<!-- ENGINE: cfi_board_json -->
- **Surface / auth:** `GET https://cfi-job-board.netlify.app/board-data.json` — a single static blob behind CFI's Netlify widget. No auth, no pagination. Listing-only (blind → enriched later). Aggregates + rescores 80,000 Hours / Probably Good, so expect overlap with `algolia_api`.
- **Config keys:** `data_url` (opt override if the widget moves), `max_jobs` (opt cap, default 150 — the feed is pre-ranked best-first so the cap keeps the top slice), `board_blacklist`.
- **Pagination & caps:** none (one blob); `max_jobs` caps rows. Drops `expired`.
- **Failure signatures:** ≥400 on the blob → `error: http_<code>`.
- **Debug:** `from fetchers.boards.consultants_for_impact import fetch_cfi_board; print(len(fetch_cfi_board({"name":"Consultants for Impact","url":"https://www.consultantsforimpact.org/job-board","max_jobs":20})))`

### `datadotorg_wp` — data.org WordPress aggregator
<!-- ENGINE: datadotorg_wp -->
- **Surface / auth:** list `GET https://data.org/wp-json/wp/v2/job`, then scrape each detail page (BeautifulSoup) for the **external employer's** apply URL, real name (`org_override`), salary, location, deadline. No auth.
- **Config keys:** `api_url` (opt, default the wp-json job type), `max_jobs` (opt, default 60), `request_delay` (opt, default 0.4 s), `board_blacklist`.
- **Pagination & caps:** WP `per_page=100` newest-first until `max_jobs`; detail loop paced at `request_delay`. Postings that 301 to the `data.org/jobs/` index are treated as expired.
- **Failure signatures:** a listing-page error breaks; nothing fetched + error → re-raised. (Needs `beautifulsoup4`.)
- **Debug:** `from fetchers.boards.datadotorg import fetch_datadotorg_board; print(len(fetch_datadotorg_board({"name":"data.org","url":"https://data.org/jobs/","max_jobs":5})))`

### `ea_opportunities_next_data` — EA Opportunities Board embedded in its page
<!-- ENGINE: ea_opportunities_next_data -->
- **Surface / auth:** `GET https://www.effectivealtruism.org/opportunities`. The EA Opportunities Board is server-rendered Next.js over Airtable: the whole board (~1000 rows, ~700 KB gzipped) arrives inside the page's `__NEXT_DATA__` script tag. No key, no login. The lighter JSON twin at `/_next/data/<buildId>/opportunities.json` is deliberately **not** used — the buildId changes on every deploy of their site and a stale one answers 404, while the page address never changes.
- **Config keys:** `page_url` (opt, default the board URL), `board_blacklist`.
- **Pagination & caps:** none — one request is the complete board. Uncapped on purpose: rows arrive newest-first with no relevance ranking, so a cap would trade completeness for nothing. If the payload ever carries fewer rows than its own `totalCount`, the shortfall is printed.
- **Filtering:** the board lists more than employment, so rows whose every `opportunityTypes` entry is Funding / Course / Event / Contest / Advising / Independent project are dropped as **not vacancies**. Unpaid and junior work (Volunteer, Internship, Fellowship, Part-time) stays and goes to scoring like any other row (STRATEGY guardrail 1).
- **Failure signatures:** a page without `__NEXT_DATA__` raises rather than returning `[]` — a board that changed shape must never read as a board with no jobs. Cloudflare answers 403 to a nameless agent, so the request sends a browser User-Agent.
- **Debug:** `from fetchers.boards.ea_opportunities import fetch_ea_opportunities_board; print(len(fetch_ea_opportunities_board({"name":"EA Opportunities Board","url":"https://www.effectivealtruism.org/opportunities","board_blacklist":[]})))`

### `fastforward_board` — Fast Forward on Getro
<!-- ENGINE: fastforward_board -->
- **Surface / auth:** Getro. List `POST https://api.getro.com/api/v2/collections/{id}/search/jobs` (paginated), detail `GET https://api.getro.com/api/v1/jobs/{slug}?collection_id={id}`. No auth.
- **Config keys:** `getro_collection_id` (opt, default 997), `max_pages` (opt, default 20), `fetch_descriptions` (opt, **default `false`** — listing-only so a run avoids ~1.5k detail calls; the enrich pass fills bodies), `board_blacklist`.
- **Pagination & caps:** `hits_per_page=100` up to `max_pages`, stops at the reported `count`.
- **Failure signatures:** a page error breaks; nothing fetched + error → re-raised.
- **Debug:** `from fetchers.boards.fastforward import fetch_fastforward_board; print(len(fetch_fastforward_board({"name":"Fast Forward","url":"https://jobs.ffwd.org/jobs","max_pages":1})))`

### `firecrawl_board` — generic Firecrawl-scraped board
<!-- ENGINE: firecrawl_board -->
- **Surface / auth:** scrapes the board `url` via `fetch_firecrawl_scrape(use_json=False)` (markdown-only, 1 credit), parses listings, infers the employer per row. Same Firecrawl/local fallback chain and scrape-status semantics as `firecrawl_scrape`.
- **Config keys:** `url` (req). Only `GLOBAL_BLACKLIST` is applied here — a `board_blacklist` on this board is **not** read; no cap.
- **Pagination & caps:** whatever one scrape returns.
- **Failure signatures:** `credit_exhausted` / `js_required` scrape-status overrides (see `firecrawl_scrape`), not `FetchError`.
- **Debug:** `from fetchers.boards.firecrawl_board import fetch_firecrawl_board; print(len(fetch_firecrawl_board({"name":"SomeBoard","url":"<board-url>"})))` (needs `FIRECRAWL_API_KEY` or falls to the local scraper)

### `hn_whoishiring` — HN "Who is hiring?" thread
<!-- ENGINE: hn_whoishiring -->
- **Surface / auth:** `GET https://hn.algolia.com/api/v1/search_by_date` to find the newest thread, then `GET https://hn.algolia.com/api/v1/items/{id}` and parse **top-level** comments (one = one posting). No auth.
- **Config keys:** `board_blacklist`. The thread is monthly, so `ttl_days` ships at ~30 to avoid daily refetches.
- **Pagination & caps:** one thread; comment `first line = "Company | Role | Location | …"`.
- **Failure signatures:** no thread found → prints, `[]`. ≥400 → `error: http_<code>`.
- **Debug:** `from fetchers.boards.hn_whoishiring import fetch_hn_whoishiring_board; print(len(fetch_hn_whoishiring_board({"name":"HN Who is hiring","url":"https://news.ycombinator.com/submitted?id=whoishiring"})))`

### `idealist_algolia` — Idealist via embedded Algolia key
<!-- ENGINE: idealist_algolia -->
- **Surface / auth:** `POST https://NSV3AUESS7-dsn.algolia.net/1/indexes/idealist7-production-published-desc/query` with the search-only key baked into idealist.org's page HTML (constants in the module). No login.
- **Config keys:** `remote_zone` (opt, default `"WORLD"`), `include_onsite` (opt, default `false`), `max_pages` (opt, default 20), `board_blacklist`.
- **Pagination & caps:** `hitsPerPage=200` up to `max_pages` / `nbPages`.
- **Failure signatures:** if the embedded key 403s, refetch it (`curl https://www.idealist.org/en/jobs | grep searchApiKey`) — the module comment documents this. A page error breaks; nothing fetched + error → re-raised.
- **Debug:** `from fetchers.boards.idealist import fetch_idealist_board; print(len(fetch_idealist_board({"name":"Idealist","url":"https://www.idealist.org/en/jobs","max_pages":1})))`

### `impactpool_html` — Impactpool server-rendered HTML
<!-- ENGINE: impactpool_html -->
- **Surface / auth:** `GET https://www.impactpool.org/search?page=N`, parsed with BeautifulSoup. No auth. Listing-only (blind → enriched later): title, org, location, seniority.
- **Config keys:** `url` (req, the search base), `max_pages` (opt, default 5), `board_blacklist`.
- **Pagination & caps:** up to `max_pages`, stops on a page with no new IDs. Location/seniority gates are intentionally neutral (accept all).
- **Failure signatures:** a page error breaks; nothing fetched + error → re-raised. (Needs `beautifulsoup4`.)
- **Debug:** `from fetchers.boards.impactpool import fetch_impactpool_board; print(len(fetch_impactpool_board({"name":"Impactpool","url":"https://www.impactpool.org/search","max_pages":1})))`

### `linkedin_guest` — LinkedIn public guest jobs API
<!-- ENGINE: linkedin_guest -->
- **Surface / auth:** the unauthenticated guest endpoints — list `GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=&location=&start=N` (HTML cards, 25/page), detail `GET .../jobPosting/{jid}`. No login; browser UA. This is the merged "LinkedIn" + "LinkedIn Non-profits" board (rebuilt in PR #51).
- **Config keys:** `queries` (list of `{keywords, location}`) — an explicit set (a one-off override) wins; otherwise resolved from the profile's `## LINKEDIN_QUERIES`, else derived from `## TARGET_ROLES` + geography via `profile_targeting.resolve_linkedin_queries`. **The shipped config carries none** (guardrail 1). `pages` (per-query cap, default 2), `request_delay` (default 3.0 s), `fetch_detail` (default `true`), `board_blacklist`.
- **Pagination & caps:** `pages` per query; every request spaced by `request_delay` with 429 back-off (`delay*(attempt+2)`). Once throttled/blocked, detail requests stop firing for the rest of the run (politeness).
- **Failure signatures (read these carefully — mostly honest, not silent):**
  - A **throttled detail** page returns nothing → the row is **saved with an empty `full_description` and a snippet built from the card's own title/org/location**; it **heals on the next fetch** when detail isn't throttled. (A 1–49 char body would trip the save-layer junk gate and drop the row; an *empty* body is explicitly allowed through so the listing survives.)
  - A 429 that survives 3 back-offs, or a listing body carrying a **block marker** (`authwall`, `captcha`, `unusual traffic`, `security verification`), → `error: blocked` / `error: http_429` — recorded as a failure, never as empty. Block markers are checked **only on the listing** endpoint (a normal detail page legitimately links to `/uas/login`).
  - A **substantial** listing page (>200 bytes) with no parseable card → `error: unparseable` (a markup/format break can't hide as an empty-but-ok run). A **tiny** no-match page (`<!---->`) is an honest empty → stop paging.
  - No queries configured → prints how to fix it, `[]`.
- **Debug:** `from fetchers.boards.linkedin import fetch_linkedin_board; print(len(fetch_linkedin_board({"name":"LinkedIn","url":"https://www.linkedin.com/jobs","queries":[{"keywords":"data analyst","location":"Remote"}],"pages":1,"fetch_detail":False})))`

### `probablygood_algolia` — Probably Good via embedded Algolia key
<!-- ENGINE: probablygood_algolia -->
- **Surface / auth:** `POST https://OJJMNNBR0H-dsn.algolia.net/1/indexes/jobs_prod/query` with a search-scoped secured key baked into jobs.probablygood.org's own GraphQL backend (constants in the module). No login.
- **Config keys:** `board_blacklist`.
- **Pagination & caps:** `hitsPerPage=200` until Algolia's `nbPages`; the index itself caps pagination at 1000 reachable hits (`paginationLimitedTo`) even when `nbHits` is larger — logged, not silently dropped.
- **Failure signatures:** if the embedded key 403s, refetch it (module comment documents the GraphQL query to re-run against `backend.jobs.probablygood.org/api/graphql`). A page error breaks; nothing fetched + error → re-raised.
- **Debug:** `from fetchers.boards.probablygood import fetch_probablygood_board; print(len(fetch_probablygood_board({"name":"Probably Good","url":"https://jobs.probablygood.org","board_blacklist":[]})))`

### `reliefweb_api` — ReliefWeb humanitarian RSS
<!-- ENGINE: reliefweb_api -->
- **Surface / auth:** `GET https://reliefweb.int/jobs/rss.xml?limit=20&offset={0,20}`. Public RSS, no registration. (The JSON API needs a pre-approved appname since Nov 2025, so RSS is used.)
- **Config keys:** `board_blacklist`.
- **Pagination & caps:** two requests (offsets 0, 20; server caps 20/request). Org/location/deadline parsed from the description HTML.
- **Failure signatures:** an RSS error breaks; nothing fetched + error → re-raised.
- **Debug:** `from fetchers.boards.reliefweb import fetch_reliefweb_board; print(len(fetch_reliefweb_board({"name":"ReliefWeb","url":"https://reliefweb.int/jobs"})))`

### `remotive_api` — Remotive remote-jobs API
<!-- ENGINE: remotive_api -->
- **Surface / auth:** `GET https://remotive.com/api/remote-jobs` — one request per run (Remotive asks for few requests/day), or one per category when `REMOTIVE_CATEGORIES` is set. No key. Remote-only.
- **Config keys:** `board_blacklist`. Env `REMOTIVE_CATEGORIES=product,marketing` narrows by category slug.
- **Pagination & caps:** single request (or one per category), deduped by id.
- **Failure signatures:** a category error is skipped; nothing fetched + error → re-raised.
- **Debug:** `from fetchers.boards.remotive import fetch_remotive_board; print(len(fetch_remotive_board({"name":"Remotive","url":"https://remotive.com"})))`

### `wwr_rss` — We Work Remotely category RSS
<!-- ENGINE: wwr_rss -->
- **Surface / auth:** `GET https://weworkremotely.com/categories/remote-{cat}-jobs.rss` per category (stdlib XML, no feedparser). No key. Remote-only; item titles are `"Company: Role"`.
- **Config keys:** `default_categories` (opt list; ships all public categories), `board_blacklist`. Env `WWR_CATEGORIES=product,marketing` picks the feeds.
- **Pagination & caps:** one request per category, deduped by link.
- **Failure signatures:** a feed error is skipped; every feed failing with nothing fetched → re-raised.
- **Debug:** `from fetchers.boards.wwr import fetch_wwr_board; print(len(fetch_wwr_board({"name":"We Work Remotely","url":"https://weworkremotely.com","default_categories":["product"]})))`

---

## Board-ID → engine crosswalk

The [catalogue](job-boards-catalogue.md) is keyed by **board ID** (who each
fits, how to enable); this page is keyed by **engine strategy** (how fetching
works). Several IDs can share one engine, and one configured strategy
(`consider_board`) has **no registered engine** at all. The bridge:

| Board ID (`config/defaults.toml`) | Engine strategy |
| --- | --- |
| `80k_hours` | `algolia_api` |
| `reliefweb` | `reliefweb_api` |
| `arbeitnow` | `arbeitnow_api` |
| `remotive` | `remotive_api` |
| `weworkremotely` | `wwr_rss` |
| `hn_whoishiring` | `hn_whoishiring` |
| `impactpool` | `impactpool_html` |
| `datadotorg` | `datadotorg_wp` |
| `idealist` | `idealist_algolia` |
| `fast_forward` | `fastforward_board` |
| `linkedin` | `linkedin_guest` |
| `consultants_for_impact` | `cfi_board_json` |
| `probablygood` | `probablygood_algolia` |
| `ea_opportunities` | `ea_opportunities_next_data` |
| `a16z`, `sequoia` | `consider_board` — **not registered** (fetcher not wired in this repo; enabling them fetches nothing, per the catalogue note) |

`firecrawl_board` and `algolia_api` are **generic** engines: `algolia_api` backs
`80k_hours` today, but a shipped `[boards.*]` block could point another board at
either engine by setting its `strategy`.

## Debug harness

Every **Debug** snippet above reads the network and writes nothing to any real
database. Run them under this preamble, which forces the local SQLite demo
backend at a throwaway path (mirrors `tests/conftest.py`), disables the repo
`.env`, and puts `scripts/` on the path:

```bash
export LLM_PIPELINE_DISABLE_DOTENV=1          # ignore the repo .env (no real SUPABASE_DB_URL)
unset SUPABASE_DB_URL SUPABASE_DIRECT_URL     # force the SQLite demo backend
export JOBSEARCH_DB_PATH="$(mktemp -d)/throwaway.db"   # a scratch DB nothing depends on
export PYTHONPATH=scripts
python3 -c 'from fetchers.boards.remotive import fetch_remotive_board; print(len(fetch_remotive_board({"name":"Remotive","url":"https://remotive.com"})))'
```

Why this is safe: importing `fetchers` connects the backend (for the registry),
but the fetch functions themselves only issue HTTP reads and **return a list** —
they never write vacancies. The throwaway `JOBSEARCH_DB_PATH` guarantees that
even the connect-on-import touches only a scratch file, never `data/jobsearch.db`
or Supabase. Firecrawl-backed engines (`firecrawl_scrape`, `firecrawl_board`,
`workable`) exercise the zero-credit local path when `FIRECRAWL_API_KEY` is
unset.

To reproduce a failing source end-to-end (with the real config, TTL and status
recording) instead of by hand, use the driver:
`python3 scripts/fetch_vacancies.py --companies "<Name>"` or
`--strategy <strategy>` for a whole ATS (see `ARCHITECTURE.md`). Unlike the
read-only recipes above, the driver **writes to whichever database your
environment points at** — keep `JOBSEARCH_DB_PATH` on a throwaway SQLite (and
`SUPABASE_DB_URL` unset) unless you intend to touch your real data.
