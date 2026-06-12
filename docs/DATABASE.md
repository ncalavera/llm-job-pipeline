# Database

The full SQL is in [sql/schema.sql](../sql/schema.sql). This page covers
what matters when working with it.

## Tables

Two main tables: `company` and `vacancy`. Everything else (triage, scores,
enrichment, fetch metadata) lives as columns on these two. A small third
table, `archived_hash`, holds tombstones for removed vacancies.

```
company  ─┬──── vacancy (via company_id FK)
          │
          ├── aliases TEXT[]        ── GIN index for resolve_canonical_name
          ├── status TEXT           ── active / candidate / inactive
          ├── website TEXT          ── official site (enrichment pipeline)
          └── ats_config JSONB      ── complex parser settings

vacancy
├── dedup_hash TEXT UNIQUE  ── md5(lower(canonical_name|title))
├── locations JSONB         ── array of {work_mode, region, country, city, url}
├── status TEXT             ── unseen / liked / passed / to_apply / ... (9)
├── llm_score INT           ── 0-100
├── digest_sent_at TIMESTAMPTZ ── set when pushed to the Telegram digest
└── triage JSONB            ── free-form notes

archived_hash
├── dedup_hash TEXT PK      ── tombstone for an archived/removed vacancy
├── reason TEXT             ── e.g. 'gone_from_source', 'low_score'
└── archived_at TIMESTAMPTZ ── cooldown window starts here
```

## Indexes

All hot-path indexes are created in `schema.sql`:

- `idx_company_aliases` (GIN) — for `aliases @> ARRAY['name']` in
  `resolve_company_id`.
- `idx_company_status` — the filter in `load_vacancies`.
- `idx_vacancy_status` — critical for `/api/statuses` (fast GET of all
  statuses).
- `idx_vacancy_dedup_hash` — dedup inside `merge_vacancies`.
- `idx_vacancy_llm_score` — dashboard sort by descending score.
- `idx_archived_hash_at` — TTL window scans on the tombstone table.

## Deduplication

Two levels:

1. **Exact:** `dedup_hash` — a stable md5 of
   `(canonical_name|title).lower()`. The same vacancy from the same source
   a second time just updates `last_seen`.
2. **Fuzzy:** `filter_vacancies.py --dedup` finds vacancies with a
   `difflib.SequenceMatcher` similarity ≥ 0.85 (configurable) across
   sources via `aliases`. The duplicate is marked `passed` with a
   `dup-of:<uuid>` note.

On top of that, `archived_hash` tombstones stop re-import of dead
postings: an archived hash is rejected at merge time for 90 days. The one
exception is `gone_from_source` records — the company's **own** ATS
re-listing a role is ground truth that it reopened, so a direct-ATS merge
ignores those tombstones (and resurrects the row to `unseen`), while job
board merges still honor them.

## Quality gate on descriptions

Every write path for `vacancy.full_description` (ATS merge, board merge,
blind re-enrichment) runs the text through `quality.clean_description()`
first. The gate strips a leading cookie/consent banner and rejects pure
boilerplate (cookie wall, HTTP-error page, navigation chrome) so junk never
overwrites a real description. `validate_db()` includes a second-line check
that counts descriptions still matching cookie-banner anchors.

## Pipeline gate

A company feeds scoring and the dashboard only when `status = 'active'`.

- `candidate` — a new company discovered by a job board parser. Awaits
  manual or automatic approval.
- `active` — approved, its vacancies are visible in the dashboard.
- `inactive` — rejected, its vacancies are hidden.

Auto-approval uses `alignment_score`: ≥ 60 → `active`, ≤ 25 → `inactive`.
Between the thresholds the company stays `candidate` for manual review.
Exact values live in `auto_review_candidates()` inside
`database_supabase.py`.

**Candidate rescue:** vacancies at `candidate` companies are normally
invisible to scoring, but `score_vacancies.py` pulls in a capped batch of
promising unscored ones (company alignment ≥ 30 or not yet enriched) per
run, so a strong role at a forgotten company still gets scored. On the
dashboard such companies get a 🔥 hot-vacancy badge in Pending Review.

## locations[] — an array, not a string

Historic reason: one vacancy is often published in several locations. They
used to be glued into one string ("Berlin / London / Remote"); now it's an
array of objects:

```json
[
  {"work_mode": "hybrid", "region": "europe", "country": "Germany",
   "city": "Berlin", "url": "https://..."},
  {"work_mode": "remote", "region": "europe", "country": null,
   "city": null, "compensation": "£60-80k"}
]
```

The `parse_location()` parser accepts any string and tries to extract the
structure. Fields it can't extract stay `null`.

For filtering, `geo.py` classifies each entry into a bucket
(`uk` / `germany` / `europe` / `us` / `cis` / `other` / `unknown`) on the
fly — country wins, then city, then free text. The pre-score filter deletes
vacancies whose every location is a geo-delete vote (USA-only, CIS
in-person, rest-of-world); geography therefore never reaches the LLM score.

## Vacancy statuses

Nine values:

| Value | When it's set |
| --- | --- |
| `unseen` | Default for new vacancies |
| `liked` | User liked it |
| `passed` | User declined |
| `to_apply` | Decided to apply |
| `to_research` | Needs deeper company research |
| `to_network` | Find an inside contact first |
| `skipped` | Dropped after triage |
| `applied` | Application sent |
| `archived` | Removed from the active catalog (low score, gone from source, or manual); shown read-only in the Archive tab |

`status_updated_at` is updated automatically in the API endpoints. There is
no SQL trigger — update it in code.

## RLS

`schema.sql` leaves Row-Level Security **disabled**. The logic:

- The dashboard reads data via `/api/statuses` and `/api/company-statuses`
  — Vercel functions using `SUPABASE_SERVICE_ROLE_KEY` (full access). The
  end user sees already-rendered HTML.
- Direct browser access to the Supabase REST API is not part of the design.

If you want to give someone direct access via the `anon` key, uncomment the
`ALTER TABLE ... ENABLE ROW LEVEL SECURITY` block at the end of
`schema.sql`.

## Egress

The Supabase free tier gives 2 GB of egress per month. A full vacancy
description can be up to 50 KB. A batch of 5,000 vacancies is ~250 MB out.
If you hit the limit — `load_vacancies(light=True)` drops
`full_description` from the SELECT (the flag exists in the DAL).

## Migrations

`schema.sql` is idempotent — `CREATE TABLE IF NOT EXISTS` and
`CREATE INDEX IF NOT EXISTS` won't fail if the objects already exist. To
change the schema on upgrade, add separate
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` statements in a new file
`sql/migrations/00X_<name>.sql`. There is no dedicated migration system in
this repo — the Supabase SQL Editor is the migration system.
