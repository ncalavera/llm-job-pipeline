---
description: Review hub for the vacancy pipeline. Bare = status menu; `apply` = deep liked-vacancy interview; `archive` = interactive low-score archival; `vac` = thin triage CLI. Auto-publishes after state changes (deploy only, never git).
---

# /jobs-review

One entry point for reviewing vacancies after scoring. The first argument picks the mode:

| Invocation | Mode |
| --- | --- |
| `/jobs-review` (no arg) | **Status menu** — print counts, let the user pick |
| `/jobs-review apply` | Deep structured interview of `liked` vacancies |
| `/jobs-review archive` | Interactive archival of low-scoring unseen vacancies |
| `/jobs-review vac [list\|show\|mark\|open\|companies] ...` | Thin triage CLI (pass-through to `scripts/vac.py`) |

**Never auto-drop into `apply`.** With no argument, show the menu and wait.

**Reply in the user's product language.** Read `## OUTPUT_LANGUAGE` from
`config/user_profile.md` (resolve with `python3 -c "import sys;sys.path.insert(0,'scripts');import product_language as p;print(p.resolve())"` → `en`/`ru`) and write ALL of your
chat — menus, prompts, interview questions, summaries — in that language.

After any mode that mutates state (`apply`, `archive`) — run the **Publish** step at the end (see bottom). `vac` mutations the user makes by hand can also publish on request, but the menu/CLI itself does not auto-publish.

---

## Mode: status menu (no argument)

Print the current review backlog so the user knows what is worth doing, then ask which mode to enter.

```python
from config import LLM_SCORE_THRESHOLD
from database_supabase import load_vacancies, get_protected_ids

v = load_vacancies()
threshold = LLM_SCORE_THRESHOLD  # default = 20

liked = [x for x in v.values() if x.get('status') == 'liked']

protected_ids = get_protected_ids()
archivable = [
    (vid, vac) for vid, vac in v.items()
    if vid not in protected_ids
    and vac.get('llm_score') is not None
    and vac['llm_score'] < threshold
]
```

Render:

```
JOBS REVIEW
============================================================
  {len(liked):3d} liked awaiting apply-decision   → /jobs-review apply
  {len(archivable):3d} low-score archivable (< {threshold})    → /jobs-review archive
  {len(v):3d} vacancies in DB total              → /jobs-review vac list
============================================================
```

Then ask (one question, via the picker): **apply / archive / vac / nothing.** Route to the matching mode below. Do not run the long interview unless the user picks `apply`.

---

## Mode: apply

Deep review of `liked` vacancies — walks through them one by one and helps make an informed decision.

### Steps

1. Load vacancies with `status = 'liked'` via `scripts/triage.py:get_liked_vacancies()`.

2. Filter out vacancies past their deadline. Show closed vacancies briefly, then proceed with open ones only.

3. Group by company. If a company has 5+ open positions, ask: review all or just the top-scoring ones.

4. For each vacancy — structured interview (one question at a time):

   **Q1: First impression**
   - Interested / Uncertain / Skip

   **Q2: Requirements match**
   - Yes / Partially / No
   - If "No" — suggest Skip (hard requirements are critical).

   **Q2.5: Growth and complexity**
   - The next role should offer tasks complex enough to produce real growth — a good fit on paper is not sufficient on its own.
   - Options: Yes, it's new/building and I'll grow / Mixed, partly growth partly maintenance / No, mostly maintenance
   - If "No" — suggest Skip.

   **Q3: Verdict**
   - `to_apply` — will apply
   - `to_research` — needs more investigation before deciding
   - `to_network` — long-term target, need a warm contact first
   - `skipped` — decided not to apply

   **Q4: Verdict-specific follow-up**
   - Apply: CV adaptation notes, preparation steps, deadline
   - Skip: reason (for scoring calibration)
   - Research: what to investigate, deadline
   - Network: who could be a contact

5. Save the decision immediately after each vacancy (do not defer):
   ```python
   triage.update_status(vacancy_id, 'to_apply')   # one vacancy
   # or, for several at once:
   # triage.batch_update_statuses_and_commit({vac_id_1: 'to_apply', vac_id_2: 'skipped'})
   ```
   Keep free-text notes in `triage/session-notes-{date}.md` via the Edit/Write tool after every single vacancy.

6. Save incrementally after every company (Step 1d) — prevents data loss if the session is interrupted.

7. If `to_apply` — create a tracking issue in your issue tracker:
   - Title: `{COMPANY} — {ROLE} (score {SCORE})`
   - Include: location, deadline, CV notes, preparation checklist, apply link
   - One issue per company (multiple roles in one issue), not one per vacancy.

7a. **Record the application as a DB entity** (only for `to_apply`, once the user
    decides to actually prepare/submit). An application is a first-class row in
    the `application` table — vacancy, company, channel, status and the attached
    artifacts — so nothing lives loose in folders. The artifact FILES stay in the
    private zone (`config.APPLICATION_ARTIFACTS_DIR`), gitignored; only their
    references live in the DB.

    ```python
    import applications
    from config import CASE_BANK_DIR, APPLICATION_ARTIFACTS_DIR

    app_id = applications.record_application(
        company_id,               # from the vacancy's company_id
        vacancy_id,               # the liked vacancy
        channel="site",           # site | email | form | referral | other
        status="applied",         # or "draft" while you are still preparing
        artifacts={
            "cv_version": "cv_ops_v3.pdf",        # which CV version was sent
            "cover_letter_path": "cover_acme.md", # file in the private zone
            "answers": {"why_us": "..."},          # saved question answers
            "research_urls": ["https://…"],        # links used to prepare
        },
        notes="drafted from the case bank",
    )
    ```

    - **Drafting the letter / answers** — the case bank (personal stories, cases,
      typical answers) lives in `CASE_BANK_DIR`, next to `user_profile.md` in the
      private zone. Read it there when you help write a cover letter or question
      answers; never hardcode a personal path — always go through the config key.
      Do NOT write the CV from scratch — only link which version was sent and help
      with the letter/answers (STRATEGY: not an auto-applier; the human submits).
    - **Idempotent per vacancy** — re-running merges new artifacts into the same
      row rather than duplicating.
    - **Move the status later** as the process advances:
      `applications.set_status(app_id, "interview")` (or `offer` / `rejected` /
      `withdrawn`). Attach more artifacts anytime with
      `applications.attach_artifacts(app_id, {...})`.

7b. **Pre-application research → `company_evidence`.** Anything you dig up about
    the company while preparing is saved into the SAME evidence table that feeds
    WANT-scoring and is shown in the company profile — not a throwaway note:

    ```python
    from database_supabase import save_company_evidence
    save_company_evidence(company_id, "manual_url",
                          url="https://example.org/impact-report",
                          content="<primary-source text you read>")
    ```

    Use `manual_url` for research you want the scorer to weigh; a custom label
    (e.g. `application_research`) keeps it profile-only. Both surface in the
    company profile's "Applications & Research" block and on the vacancy card.

8. After all companies are reviewed — update vacancy statuses in the database:

   | Verdict | Status |
   |---------|--------|
   | apply | `to_apply` |
   | skip | `skipped` |
   | research | `to_research` |
   | network | `to_network` |

9. Run the **Publish** step (state changed).

### Calibration loop

At the end of the session, ask three calibration questions (one at a time):

1. **Scoring accuracy** — were any LLM scores significantly off (too high or too low)?
2. **Pipeline calibration** — any new blacklist patterns to add? Companies to stop monitoring?
3. **Strategy insights** — patterns, direction shifts, new priorities?

If the user provides feedback:
- Proposed scoring prompt changes → show exact diff, ask for confirmation before editing.
- New blacklist patterns → show them, ask for confirmation before editing `scripts/config.py`.
- Strategy updates → propose updating `strategy.md`, ask for confirmation.

### Auto-archive note

The auto-archive by score threshold is currently paused under pure-fit scoring — do not archive automatically from within `apply`. Use `/jobs-review archive` explicitly after reviewing.

### Important rules (apply)

- **One question at a time** — never ask multiple questions at once.
- **Save after every vacancy** — append to `triage/session-notes-{date}.md` immediately, never defer.
- **Save after every company** — incremental DB save is mandatory, not optional.
- **1 tracker issue = 1 company** — not one per vacancy, to avoid tracker spam.
- **Calibration changes need confirmation** — never auto-edit the scoring prompt or blacklist.
- **The `vacancy.status` column is the source of truth** — `triage.json` stores metadata only.

### Files (apply)

- `scripts/triage.py` — helpers (load, group, persist).
- `scripts/applications.py` — the `application` DB entity (create, status move, artifacts).
- `application` table — one row per application (vacancy, company, channel, status, artifact refs).
- `company_evidence` table — where pre-application research lands (via `save_company_evidence`).
- `config.CASE_BANK_DIR` / `config.APPLICATION_ARTIFACTS_DIR` — the gitignored private zone
  (`JOBSEARCH_PRIVATE_DIR`) for the case bank + application artifacts. Never a hardcoded path.
- `vacancy.triage` JSONB column — stores notes and decision metadata.
- `triage/jobs-apply.json` — session history (metadata only; The `vacancy.status` column is the source of truth).
- `triage/session-notes-{date}.md` — running Markdown notes for the session.

---

## Mode: archive

Clean the dashboard of vacancies that scored low but were not touched by auto-archive (or where scoring was unreliable in either direction).

This is the **score-threshold** archival path. It is distinct from the **gone-from-source** archival, which happens automatically inside `/jobs-new` fetch (vacancies that disappeared from the ATS get `status='archived'` there) — different mechanism, do not conflate. This mode only sets `status='archived'` on low-scoring unseen vacancies after your explicit review.

The pipeline has an "Archive" tab in the dashboard showing vacancies with `status = 'archived'`. This mode sets that status interactively after your review.

### Step 0: Load current state

```python
from config import LLM_SCORE_THRESHOLD
from database_supabase import load_vacancies, get_protected_ids

v = load_vacancies()
threshold = LLM_SCORE_THRESHOLD  # default = 20

protected_ids = get_protected_ids()

candidates = [
    (vid, vac) for vid, vac in v.items()
    if vid not in protected_ids
    and vac.get('llm_score') is not None
    and vac['llm_score'] < threshold
]
```

Show a preview:

```
ARCHIVE PREVIEW
============================================================
  Default threshold:  score < {threshold} (unseen only)
  Total DB:           {len(v)} vacancies

  Archive candidates: {len(candidates)} vacancies
    Score  0-9:       {len(b0):3d} vacancies (clearly irrelevant)
    Score 10-14:      {len(b10):3d} vacancies
    Score 15-19:      {len(borderline):3d} vacancies  <- borderline, review these

  After archive: {remaining} vacancies remain in DB
============================================================
```

Flag borderline vacancies that were scored without a description ([BLIND]):

```
  Top borderline (score 15-19):
    [18] ACME Corp — Field Coordinator [BLIND]
         Remote
```

If any blind-scored borderlines exist, warn the user before asking for confirmation.

### Step 1: Ask for confirmation

```
Archive {CANDIDATES} vacancies (score < {THRESHOLD}, unseen only)?
  1. Archive all
  2. Raise threshold to 25 (more vacancies)
  3. Lower threshold to 15 (fewer vacancies)
  4. Exclude blind-scored borderlines  <- RECOMMENDED if blind borderlines > 0
  5. Review past archives
  6. Cancel
```

Wait for user response before proceeding. If user picks "Raise/Lower threshold" — re-run Step 0 with the new value.

### Step 2: Execute archive

Run `archive_vacancies(threshold)` from `database_supabase.py`. For option 4 (exclude blind borderlines), skip vacancies in the 15–19 range that have no description.

After archiving, show: how many were archived and the current DB size.

### Step 3: Confirm result

```
ARCHIVE COMPLETE
=======================================================
  DB now: {total} vacancies ({unseen} unseen, {scored} scored)
=======================================================
```

Then run the **Publish** step (state changed).

### Restoring vacancies

If you archived something by mistake — nothing is deleted, `status = 'archived'` is all that changed.

Restore a single vacancy:

```bash
python3 scripts/vac.py mark <uuid> unseen
```

Restore multiple vacancies via SQL:

```sql
UPDATE vacancy SET status = 'unseen', status_updated_at = NOW()
WHERE id IN ('<uuid1>', '<uuid2>', ...);
```

The Archive tab in the dashboard always shows archived vacancies if you need to review them.

### Step 4: Review past archives (optional)

If user picks "Review past archives":

```bash
python3 -c "
import json
from pathlib import Path

archive_dir = Path('vacancies/archive')
archives = sorted(archive_dir.glob('archived_*.json'), reverse=True)

if not archives:
    print('No archives found.')
    exit()

for i, path in enumerate(archives):
    with open(path) as f:
        meta = json.load(f)
    ts = meta.get('archived_at', path.stem)[:16].replace('T', ' ')
    print(f'[{i+1}] {path.name}')
    print(f'     {meta[\"count\"]} vacancies | threshold {meta[\"threshold\"]} | {ts}')
"
```

Ask: browse a specific archive or restore vacancies?

To restore specific vacancies, update their `status` back to `unseen` in the database via `database_supabase.get_conn()`.

### Important rules (archive)

- **Never auto-archive** — always show preview and wait for explicit confirmation.
- **Flag blind borderlines** — vacancies scored without a description in the 15–19 range need special attention.
- **Default threshold is 20** — from `LLM_SCORE_THRESHOLD` in `config.py`.
- **Only archive unprotected vacancies** — never archive liked/passed/applied.
- **This is the score-threshold path only** — gone-from-source archival lives in `/jobs-new` fetch, not here.

---

## Mode: vac

Thin triage CLI that runs against whatever backend is active — local SQLite by default (simple mode), or Postgres when `SUPABASE_DB_URL` is set (legacy name; any Postgres). Use it when you do not want to open the dashboard or are on a server without a browser. Pass the sub-command and flags straight through to `scripts/vac.py`.

```bash
python3 scripts/vac.py <command>
```

If a `vac` alias is configured in your shell — you can write `vac <command>` directly.

| Goal | Command |
| --- | --- |
| Top by score | `python3 scripts/vac.py list` |
| Only liked | `python3 scripts/vac.py list --status liked` |
| Only from a specific company | `python3 scripts/vac.py list --org "GiveDirectly"` |
| Sort by date | `python3 scripts/vac.py list --sort recent` |
| Filter by geo bucket | `python3 scripts/vac.py list --geo uk` |
| Full details | `python3 scripts/vac.py show <uuid>` |
| Change status | `python3 scripts/vac.py mark <uuid> liked` |
| Open URL in browser | `python3 scripts/vac.py open <uuid>` |
| Company summary | `python3 scripts/vac.py companies` |

### Flags

`list` flags:

- `--limit N` — number of rows to show.
- `--status <name>` — filter by a single status (e.g. `liked`, `unseen`).
- `--min-score N` — minimum LLM score.
- `--tier S|A|B|C` — filter by company tier.
- `--org "Name"` — filter by company-name substring.
- `--sort score|recent|company` — sort order (default `score`).
- `--include-candidates` — also show vacancies from non-approved companies.
- `--geo {bucket}` — filter by geographic bucket. Accepted values: `uk`, `germany`, `europe`, `us`, `cis`, `other`, `unknown`. Buckets are assigned by `geo.py` based on vacancy location data.

`mark` takes the status as a **positional** argument: `mark <uuid> <status>`.

`companies` takes `--status active|candidate|inactive` and `--limit N`.

### Supported statuses

`unseen`, `liked`, `passed`, `to_apply`, `to_research`, `to_network`, `skipped`, `applied`, `archived`

The `archived` status covers vacancies moved to the Archive tab (low-scoring or gone from source) — they remain in the database but are hidden from the main catalog view.

### Typical workflows

**Morning review of liked vacancies:**
```bash
python3 scripts/vac.py list --status liked
python3 scripts/vac.py show <id>     # read the most interesting one
python3 scripts/vac.py open <id>     # check the original page
python3 scripts/vac.py mark <id> to_apply
```

**Scan new high-scoring unseen:**
```bash
python3 scripts/vac.py list --status unseen --min-score 70 --sort score
```

**Find all vacancies for a company:**
```bash
python3 scripts/vac.py list --org "Acme Foundation" --include-candidates
```

**Filter by UK-only locations:**
```bash
python3 scripts/vac.py list --geo uk
```

### When NOT to use vac

- Bulk operations (>10 vacancies) — use the dashboard.
- Triage with long notes — use `/jobs-review apply`, which writes to `vacancy.triage` JSONB.
- Full-text search in descriptions — there is no full-text search here. Use the
  dashboard's search box, or query the database directly (SQLite: `sqlite3
  data/jobsearch.db`; Supabase: the SQL Editor).

### Architecture (vac)

- Code: `scripts/vac.py` (stdlib only; no direct DB driver — it goes through the DAL).
- Database: the active backend — local SQLite by default, Supabase when
  `SUPABASE_DB_URL` is set. Shared with `/jobs-new`. No local caches.
- DAL: `database_supabase.py` — `load_vacancies()`, `update_vacancy_status()`.

---

## Publish (after `apply` / `archive` state changes)

The dashboard reads its data live, so publishing a data change is just a
regenerate — **no deploy.** Redeploying the dashboard server is only for
dashboard code changes, and git stays fully manual (never `git add` /
`git commit` / `git push`).

1. **Regenerate from the current DB:**
   ```bash
   python3 scripts/fetch_vacancies.py --report-only
   ```
   - **Full mode** (Supabase): upserts the `dashboard_snapshot` row that
     `/api/vacancies` serves — the deployed dashboard is live, refresh the
     browser, **no deploy.**
   - **Simple mode** (no Supabase / no `DATABASE_URL` for a hosted server):
     writes `public/data.js` locally, served by the local dashboard server.
     Never deploy. Do NOT run `--report-only` from a git worktree.

2. **Auth assertion (full mode):** the dashboard carries PII, so it must sit
   behind the reverse proxy's Basic Auth — `server.js` binds to `127.0.0.1` and
   does not authenticate on its own. Confirm the proxy actually challenges:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://<your-dashboard-host>/api/vacancies
   # expect 401 without credentials
   ```
   See [MIGRATION.md](../../MIGRATION.md) for the Caddy site block.

3. **Debounce.** If several `/jobs-review` actions run in one session, only
   regenerate when the data actually changed since the last publish.

### Publish rules

- **NEVER `git add` / `git commit` / `git push`** — git stays fully manual.
- **Data changes never deploy** — `--report-only` regen makes them live (full
  mode) or local (simple mode). Redeploying the server is for dashboard CODE only.
- **Full mode — regenerate only a clean change.** The regen writes the live
  snapshot directly, so a bad/aborted run would corrupt the live dashboard with
  no deploy gate to catch it. If a mode aborted or archived a suspicious amount,
  do NOT regenerate — the previous good snapshot stays live.
- **Simple mode** — local `data.js` regen is the whole publish.
