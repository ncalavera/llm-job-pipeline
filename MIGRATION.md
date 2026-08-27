# Dashboard migration: Vercel + Supabase → self-hosted Node + Postgres

Target: one Node HTTP server (`server.js`) on a Hetzner VPS behind Caddy.
Caddy terminates TLS and enforces Basic Auth (replacing `middleware.js`).
Postgres 17 runs on localhost; the server talks to it via `DATABASE_URL`
with the `pg` package.

**The Vercel path is removed.** `api/` (nine serverless handlers),
`vercel.json` and `middleware.js` were deleted once `server.js` reimplemented
every endpoint: two copies of the same nine contracts drifted (the ETag
helpers were already duplicated by hand), and nothing imported the Vercel
files any more. `server.js` is the only server. There is no DNS rollback to
Vercel — see Rollback below.

Prerequisite: the target database has ALL migrations under `sql/migrations/`
applied. The retired Vercel handlers carried defensive fallbacks for
partially-migrated databases (unknown-column retries in board-statuses and
health-detail); `server.js` deliberately drops them and assumes the full
schema.

## Endpoint contract map

Every endpoint below is served by `server.js`. The file name in parentheses
is the retired Vercel handler the contract was ported from — kept as
provenance for anyone reading the old code in git history, not a live path.
Nothing under `public/` changed across the port.

### GET /api/vacancies (`api/vacancies.js`)

The live dashboard payload (full mode) — the single `dashboard_snapshot`
row's `payload` JSONB, same shape as the baked `public/data.js` VACANCY_DATA.

- Same-origin only: NO `Access-Control-Allow-Origin` header. Carries PII.
- `Cache-Control: no-store` on every response.
- `OPTIONS` → 204. Any method other than GET → 405 `{"error":"Method not allowed"}`.
- Conditional GET (the dashboard polls every 60s, see
  `public/modules/bootstrap.js`):
  - Metadata-first, two-query split: a cheap `SELECT updated_at` runs first;
    the multi-MB `payload` JSONB is pulled ONLY when the client's copy is
    stale. On a 304 the payload never leaves Postgres.
  - `ETag: "<updated_at>"` — the row's `updated_at` is bumped on every
    pipeline write, so it is a stable version token (no payload hashing).
  - `If-None-Match` is compared weakly (RFC 9110): a `W/` prefix on either
    side is stripped, a comma-separated list is accepted, `*` matches.
    Match → 304 with no body.
- Snapshot row absent → 503 `{"error":"Snapshot not generated yet"}` — NOT
  404, because 404 is the front-end's "endpoint absent → load static
  data.js" signal (`resolveSource()` in bootstrap.js).
- DB error → 500 `{"error":"Database error"}`.
- 200 → the raw payload object as JSON.

### GET /api/companies (`api/companies.js`)

Live company rows for the Companies tab (the snapshot's company list goes
stale between pipeline runs).

- Same-origin only (no CORS header), `Cache-Control: no-store`. Carries PII.
- `OPTIONS` → 204; non-GET → 405.
- Response: `{"companies": [...]}` where each element is the exact mapped
  shape the ported handler built — `company_id` (stringified uuid),
  `name`, `slug` (lowercased, spaces→`-`, dots stripped), `status`
  (lowercased), `review_status` (active→approved, candidate→pending,
  inactive→rejected, else pending), `calculated_tier`, `alignment_score`
  (column, falling back to `mission_fit.alignment_score`, Number or null),
  `website`/`careers_url` (OMITTED when empty — undefined keys are dropped
  so the client's snapshot merge can fill older values), `offices`,
  `category`, `strategy`, `fetch_status`, `last_fetched`,
  `is_manual_check`, `needs_source`, `is_archived`, live vacancy rollups
  (`vacancy_count`, `liked_count`, `new_count`, `vacancy_ids` — built from
  non-archived vacancy rows; "liked" = any status except unseen/passed),
  and the enrichment fields from the `about`/`mission_fit` JSONB
  (`is_enriched`, `experience_match`, `personal_interest`, `notes`,
  `description`, `sector`, `founded_year`, `employee_count`,
  `funding_status`, `hq_location`, `alignment_label`, `fit_dimensions`
  (OMITTED when absent/empty), `fit_strengths`, `fit_risks`,
  `fit_approach`, `experience_reasoning`, `mission_verdict`).
- DB error → 500 `{"error":"Database error"}`.

### POST /api/save (`api/save.js`, via the `withHandler` preamble)

Persist a vacancy triage status.

- `withHandler` preamble (shared by every endpoint marked "wrapped" below):
  `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods:
  <METHOD>, OPTIONS`, `Access-Control-Allow-Headers: Content-Type,
  Authorization` (POST) / `Authorization` (GET); `OPTIONS` → 204; wrong
  method → 405; missing DB config → 500 `{"error":"Server misconfigured"}`.
- Body: `{"id": <vacancy uuid>, "status": <one of unseen, liked, passed,
  to_apply, to_research, to_network, skipped, applied, test_task, interview,
  declined, accepted, expiring, archived>}`.
- Missing field → 400 `{"error":"Missing id or status"}`; unknown status →
  400 `{"error":"Invalid status"}`.
- Updates `vacancy.status` + `status_updated_at`. When the new status is an
  application (applied, test_task, interview, declined, accepted) it also sets
  `applied_at` — but only if it is still NULL, so the first send date survives
  every later stage. Update-only: unknown id →
  404 `{"error":"Vacancy not found","id":...}`.
- 200 → `{"ok":true,"ts":<ISO timestamp>}`. DB error → 500
  `{"error":"Database error"}`.

### GET /api/statuses (`api/statuses.js`, wrapped)

All vacancy status overrides the client merges over the snapshot.

- Response: `{"statuses": {<id>: <status>}, "timestamps": {<id>:
  <status_updated_at>}}` for every vacancy whose status is neither `unseen`
  nor `archived`; `timestamps` only carries ids with a non-null timestamp.
- Note: the Supabase version inherited PostgREST's silent 1000-row cap
  here; the plain-SQL reimplementation returns all rows (strictly more
  correct, same shape).

### POST /api/company-review (`api/company-review.js`, wrapped)

- Body: `{"company_id": <uuid>, "action": "approve"|"reject"}`.
- Missing field → 400 `{"error":"Missing company_id or action"}`; bad
  action → 400 `{"error":"Invalid action — must be 'approve' or 'reject'"}`.
- approve → `company.status='active'`, reject → `'inactive'`; also writes
  `status_reason` (`"approved via dashboard"` / `"rejected via dashboard"`).
- Unknown id → 404 `{"error":"Company not found","company_id":...}`.
- 200 → `{"ok":true,"action":...,"company_id":...,"ts":<ISO>}`.

### GET /api/company-statuses (`api/company-statuses.js`, wrapped)

- Response: `{"statuses": {<company id>: "approved"|"pending"|"rejected"}}`
  for EVERY company row (active→approved, candidate→pending,
  inactive→rejected, anything else→pending).

### GET /api/board-statuses (`api/board-statuses.js`, wrapped)

Live status of monitored job boards.

- Response: `{"boards": [...]}`, one element per `board` row: `id`, `name`,
  `strategy`, `tier`, `ttl_days`, `url`, `last_fetched`, `enabled`
  (normalised: null→true), `hidden` (normalised: null→false), `vac_total`
  (count of vacancies with `source_board` = the board's name), `vac_recent`
  (same, `last_seen` within 14 days), `overdue` (true when `last_fetched`
  is null or `ttl_days` is null or age ≥ ttl).

### POST /api/board-toggle (`api/board-toggle.js`, wrapped)

- Body: `{"board_id": <string>, "enabled": <boolean>}` — `board_id` must be
  a non-empty string (400 `{"error":"Missing or invalid board_id"}`),
  `enabled` a real boolean (400 `{"error":"Missing or invalid enabled —
  must be a boolean"}`).
- Update-only on `board.enabled` (+ `updated_at`); unknown id → 404
  `{"error":"Board not found","board_id":...}`.
- 200 → `{"ok":true,"board_id":...,"enabled":...,"ts":<ISO>}`.

### GET /api/health (`api/health.js`)

- Non-GET (including OPTIONS) → 405.
- Probes the DB (a count on `vacancy`); always answers 200
  `{"ok":<bool>,"ts":<ISO>,"backend":"postgres"}` — `ok` is false when the
  probe fails. Deliberately minimal (no env/prefix/row-count leakage).
- Contract delta vs Vercel: `backend` reads `"postgres"` instead of
  `"supabase"`. Nothing in `public/` reads this field.

### GET /api/health-detail (`api/health-detail.js`, wrapped)

The Health tab's aggregate, `Cache-Control: no-store`. Response
`{"boards":[...],"companies":{...},"waiting":{...},"learning":{...}}`:

- `boards` — per ENABLED board: `id`, `name`, `last_fetched`,
  `last_success`, `consecutive_failures`, `vacancy_count`,
  `presumed_broken` (3+ consecutive failures, or fetched yet zero
  vacancies); sorted broken-first.
- `companies` — `{failing: [{name, fetch_status, consecutive_failures,
  last_fetched}], manual_check: [{name, strategy}]}` over ACTIVE companies:
  coverage `board_only`/`manual` or strategy `manual_check` → manual_check
  (sorted by name); else failing when 3+ consecutive failures or
  `fetch_status` ∈ {error, render_ok_zero, no_data, js_required} (sorted by
  failures desc).
- `waiting` — `{candidates_pending, unseen_scored,
  oldest_unseen_age_days}` (unseen + scored = `llm_score IS NOT NULL`).
- `learning` — `{last_review, last_review_age_days, applied_since,
  verdicts_pending}` mirroring `scripts/learning.py`'s cursor math; when
  the `learning_log` table is absent the whole block degrades to nulls plus
  `"unavailable": true` instead of failing the endpoint.

### Static files (was: Vercel `public/` + `vercel.json` headers)

- `public/` served at the site root; `/` → `public/index.html`.
- `Cache-Control: public, max-age=0, must-revalidate` on every static
  response (vercel.json set this for `/`, `/index.html` and `*.js|css`;
  the server applies it uniformly — same effect where it mattered).
- Unknown `/api/*` path → 404 `{"error":"Not found"}`; a missing static
  file → plain 404. (`public/data.js` does not exist in full mode — a 404
  there is what bootstrap.js expects.)

### Auth (was: `middleware.js` on Vercel Edge)

Basic Auth moves to Caddy (`basic_auth` directive, bcrypt-hashed password).
Consequences, all invisible to `public/` code:

- The HMAC `__Host-session` cookie flow is dropped; browsers cache Basic
  credentials per-realm instead, and Caddy verifies them per request. An
  expired/cleared credential still yields 401 → `bootstrap.js` reloads →
  the browser re-prompts, exactly as before.
- The handlers' fail-closed `AUTH_USER`/`AUTH_PASS` checks (503 "Auth not
  configured") are Vercel-specific and are not carried over; instead
  `server.js` binds to 127.0.0.1 by default so it is unreachable except
  through Caddy. Do not set `HOST=0.0.0.0` unless something else enforces
  auth in front of it.

## Environment variables

| Var | Required | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | yes | Postgres connection string, e.g. `postgres://dashboard:…@127.0.0.1:5432/jobsearch`. Without it the server still starts and serves static files; API routes answer 500 `{"error":"Server misconfigured"}`. |
| `PORT` | no | Listen port, default `3000`. |
| `HOST` | no | Bind address, default `127.0.0.1`. Keep it loopback — Caddy is the only intended client. |

## systemd unit

`/etc/systemd/system/job-dashboard.service`:

```ini
[Unit]
Description=Job pipeline dashboard
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=dashboard
Group=dashboard
WorkingDirectory=/opt/llm-job-pipeline
ExecStart=/usr/bin/node server.js
Environment=NODE_ENV=production
Environment=PORT=3000
# Keep secrets out of the unit file:
EnvironmentFile=/etc/job-dashboard/env
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadOnlyPaths=/opt/llm-job-pipeline
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

`/etc/job-dashboard/env` (mode 0600, owner root):

```
DATABASE_URL=postgres://dashboard:CHANGE_ME@127.0.0.1:5432/jobsearch
```

Enable: `systemctl daemon-reload && systemctl enable --now job-dashboard`.

## Caddy site block

`caddy hash-password` produces the bcrypt hash. `/etc/caddy/Caddyfile`:

```caddyfile
dashboard.example.com {
    basic_auth {
        # caddy hash-password --plaintext 'the-real-password'
        nikita $2a$14$REPLACE_WITH_REAL_BCRYPT_HASH
    }
    reverse_proxy 127.0.0.1:3000
    encode zstd gzip
}
```

Caddy obtains and renews the TLS certificate automatically. `basic_auth`
answers 401 with a `WWW-Authenticate: Basic` challenge — the same contract
`middleware.js` gave the front-end.

## Cutover steps

1. **Provision** — Postgres 17 on the VPS; create role + database; apply
   `sql/schema.sql` and every `sql/migrations/*.postgres.sql` (or restore a
   dump, step 2, which carries the schema).
2. **Copy data** — `pg_dump` the Supabase database (use the direct
   connection string, not the pooler) and `pg_restore`/`psql` it into the
   local Postgres. Re-run just before the DNS flip so the snapshot row and
   statuses are current, or freeze pipeline runs during the flip.
3. **Deploy the app** — clone the repo to `/opt/llm-job-pipeline` at this
   branch, `npm install --omit=dev`, install the systemd unit + env file,
   start, check `curl -s localhost:3000/api/health` → `{"ok":true,...}`.
4. **Caddy** — install the site block, reload, verify
   `https://dashboard.example.com` prompts for Basic Auth and renders.
5. **Repoint the pipeline** — the Python pipeline writes through
   `SUPABASE_DB_URL` (see `scripts/db_backend.py`); point it at the new
   Postgres so `generate_dashboard()` upserts the snapshot the new server
   reads. Until then the new dashboard serves data frozen at the dump.
6. **Flip DNS** — move the dashboard hostname to the VPS. Nothing to keep
   warm on the other side: the Vercel path is gone.
7. **Verify** — login, Companies tab, a status save (`/api/save` 200), the
   Boards and Health tabs, and one 60s poll cycle returning 304
   (`curl -H 'If-None-Match: <etag>'`).

## Rollback

There is no rollback to Vercel: `api/`, `vercel.json` and `middleware.js` are
deleted, so the serverless deployment cannot be rebuilt from this branch. Roll
back inside the VPS instead — check out the previous tag, `npm ci --omit=dev`,
restart the systemd unit. Recovering the Vercel path at all means restoring
those files from git history AND re-provisioning Supabase; treat that as a
rebuild, not a switch.
