# Requirements — Dashboard reads live from Supabase (full mode)

**Date:** 2026-06-24
**Linear:** DHA-284
**Scope:** Standard / Deep-feature
**Status:** brainstorm complete → ready for planning

## Problem

The dashboard renders a **static snapshot**. The pipeline regenerates `public/data.js`
(an ~8.7 MB `var VACANCY_DATA = {...}` blob, 1500+ vacancies) and ships it with
`vercel --prod`. Every data change (fetch, score, verdict, archive) needs a regen +
redeploy to appear. Slow, and fragile — a regen from the wrong working dir can wipe the
snapshot (the worktree-clobber incident).

## Goal / Outcome

In **full mode** (Supabase backend), a browser refresh shows current data with **no
redeploy**. Deploy happens only when the dashboard *code* changes, never when *data*
changes. **Simple mode** (local SQLite, no Supabase) keeps the static `data.js` path
unchanged.

## Chosen approach

**Snapshot row in Supabase + thin read endpoint, full payload, client-side filtering
kept as-is.** (Architecture revised in eng review — see "Why snapshot" below.)

- The dashboard payload is NOT a thin DB dump: `scripts/report/__init__.py:76-185`
  (`generate_dashboard`) assembles it with heavy Python transforms (grouping, company
  tiers, enrichment stats, i18n strings, illustration packs). Reimplementing that in JS
  would duplicate complex logic across two languages and drift. So:
- **Pipeline writes a snapshot.** In full mode, `generate_dashboard` upserts the same
  `vacancy_data` payload it builds today into a Supabase row (one-row table, e.g.
  `dashboard_snapshot(id, payload JSONB, updated_at)`) instead of (or in addition to)
  writing `public/data.js`. Same transform, new sink. Simple mode keeps writing
  `public/data.js`.
- **Endpoint is thin.** `GET /api/vacancies` SELECTs that one row and returns
  `payload` as JSON — same shape as today's `VACANCY_DATA`. Mirrors the
  `api/statuses.js` + `api/_supabase.js` (`getSupabase()`) pattern.
- **Front-end: bootstrap + dynamic import.** `public/modules/state.js:5-21` destructures
  `window.VACANCY_DATA` **synchronously at module-eval**, and every module reads those
  bindings. So the data must exist before the module graph loads. A small bootstrap in
  `public/index.html` (replacing the bare `<script src="data.js">`): fetch
  `/api/vacancies` → assign `window.VACANCY_DATA = await res.json()` → then dynamic
  `import("./app.js")`. This keeps `state.js`, `catalog.js`, all consumers and the
  client-side filter/sort/basket (`catalog.js:103-138`) **unchanged**.
- **Source selection with honest error states:**
  - `200` → use the live payload.
  - `404` (endpoint absent, i.e. simple/local mode) → fall back to baked `data.js`.
  - `401` / `500` / `503` → a real error (auth challenge, misconfig, server down). Do
    **not** silently fall back to a possibly-stale or absent `data.js`; surface an
    explicit error state. (In full mode `data.js` is no longer shipped, so a silent
    fallback would just show a broken page anyway.)

### Why snapshot (not JS reimplementation)

Acceptance #1 is "data change visible on **refresh**, no **redeploy**" — it targets the
deploy, not the regen. Today every data change already triggers the `--report-only`
regen step; that's what makes the change visible. Snapshot keeps that step (now a fast DB
upsert, no deploy, no file to clobber) and reuses the single, tested Python transform.
A raw DB edit isn't reflected until the next regen — but that's already true today (baked
structural data + live status overlay via `/api/statuses`). No new staleness, no drift.

**Why this and not the alternatives:**

- *Client + Supabase anon key + RLS* — rejected. RLS is deliberately disabled
  (`sql/schema.sql:148-159`, schema comment: dashboard reaches the DB through Vercel API
  endpoints using the service-role key). Going client-direct means enabling RLS, issuing
  an anon key, and exposing PII columns (`llm_reasoning`, `llm_summary`, `triage`) to the
  browser layer. More work, weaker PII posture.
- *Server-side pagination + filtering* — rejected for now. Would require rewriting all
  in-browser filter / sort / basket logic. 1500 rows don't justify it; the endpoint will
  already exist if scale later demands it.
- *Trim heavy fields + lazy-load on card open* — deferred. A traffic optimization, not
  required by acceptance. Revisit if payload size becomes a problem.

## Requirements

1. **Snapshot row + thin endpoint.** `generate_dashboard` (full mode) upserts the assembled
   `vacancy_data` payload into a Supabase one-row table (`dashboard_snapshot`). `GET
   /api/vacancies` SELECTs that row and returns `payload` as JSON — same keys/shape as
   `VACANCY_DATA`. No reimplementation of the Python transform in JS.
2. The endpoint sits behind `middleware.js` Basic Auth. Its matcher
   (`/((?!_next/static|favicon.ico).*)`, `middleware.js:13`) already covers `/api/*` — no
   change needed, but the protection must be verified, not assumed.
2a. **Auth hardening (in scope — highest-risk path).** Two guards, not a manual check:
   - An automated test asserting `/api/vacancies` returns **401** when `AUTH_USER`/`AUTH_PASS`
     are set and no/invalid credentials are sent.
   - The endpoint **fails closed**: if `AUTH_USER`/`AUTH_PASS` are *absent* in the Vercel
     environment (the opt-in-auth gap at `middleware.js:88-94`), `/api/vacancies` must
     refuse to serve PII rather than returning it openly. Confirm those env vars are set in
     the Vercel project as part of shipping.
2b. The endpoint must **not** copy `api/statuses.js`'s `Access-Control-Allow-Origin: *`
   (`api/statuses.js:4`) — that payload is id+status only; `/api/vacancies` carries PII and
   must be same-origin (no wildcard CORS).
3. **Front-end bootstrap.** A small loader in `public/index.html` (replacing the bare
   `<script src="data.js">` at `index.html:355`) fetches `/api/vacancies`, assigns
   `window.VACANCY_DATA`, then dynamic-`import("./app.js")`. `state.js`, `catalog.js`, and
   all consumers stay unchanged (they keep reading `window.VACANCY_DATA` synchronously).
   `200` → live; `404` → fall back to baked `data.js`; `401/500/503` → explicit error
   state, never a silent stale fallback.
4. Simple mode is untouched: no `/api/vacancies` on the local server, `generate_dashboard`
   still writes `public/data.js`, served by `scripts/dashboard_local.py`. The bootstrap's
   `404` branch loads it.
5. `/jobs-new` and `/jobs-review` runbooks: in **full mode** the `--report-only` regen now
   upserts the snapshot row (fast, no deploy) instead of writing `data.js` + `vercel
   --prod`. The regen step stays; the per-data-change deploy is dropped. Simple mode keeps
   its local `--report-only` → `data.js`.

## Acceptance criteria (from the issue)

- [ ] Full mode: a data change is visible on browser refresh with **no deploy**.
- [ ] Live endpoint is auth-protected; **no PII reachable unauthenticated**.
- [ ] Simple mode still renders from the static snapshot.
- [ ] `/jobs-new` / `/jobs-review` publish step updated for full mode (no per-run deploy).

## Scope

**In:** the `/api/vacancies` endpoint; the front-end try-API-then-fallback wiring; the
two runbook edits; the auth-hardening guards (req. 2a — 401 test + fail-closed on missing
auth env).

**Out:** server-side filtering/pagination; field trimming/lazy-load; enabling RLS;
anon-key client access; any simple-mode behavior change.

## Outstanding questions (for planning)

- Does full mode **stop generating `data.js` entirely**, or keep baking it as an offline
  fallback? Leaning stop (it's the whole point), accepting "API down → no data" for a
  personal single-user dashboard. [NEEDS CLARIFICATION: confirm during planning]
- The `<script src="data.js">` tag (`public/index.html:355`) will 404 in full mode if
  `data.js` is no longer shipped — harmless console error, or make the tag conditional?
- Caching: the fetch must not serve a stale response on refresh — set no-store / cache
  headers on `/api/vacancies`.
- Does `dashboard_local.py` need any change, or does the fallback handle it for free
  (it never serves `/api/vacancies`, so the front-end falls back automatically)?

## Risks

- **PII leak** if the endpoint is ever reachable without auth — but auth is *opt-in*
  (`middleware.js:88-94`: no `AUTH_USER`/`AUTH_PASS` → open). Plan must confirm those env
  vars are set in the Vercel project, or the live endpoint exposes PII publicly. This is
  the single highest-risk item.
- Regressions in the existing client-side filter/sort if the API payload shape drifts
  from `VACANCY_DATA` even slightly. Mitigate: endpoint returns byte-compatible shape;
  test by diffing API output against a freshly generated `data.js`.

## Grounded file pointers

- Front-end load: `public/index.html:355-356`, `public/modules/state.js:5-33`,
  `public/modules/catalog.js:103-138`, `public/app.js:283-299`
- Auth: `middleware.js:13,88-94`, `vercel.json`
- Existing API pattern: `api/_supabase.js`, `api/statuses.js`
- Mode detection: `scripts/db_backend.py:33-38`, `api/statuses.js:12-17`
- Publish step: `scripts/report/__init__.py:77-184`,
  `.claude/commands/jobs-new.md:436-479`, `.claude/commands/jobs-review.md:390-416`
- Schema + RLS: `sql/schema.sql:75-120,148-159`
- Servers: `scripts/dashboard_local.py:242-245`
