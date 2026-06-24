---
title: Worktree --report-only clobbers main's public/data.js (dashboard stuck on "Sorting vacancies…")
category: integration-issues
date: 2026-06-24
tags: [worktree, data.js, dashboard, report-only, git-common-dir, fetch_vacancies]
component: dashboard / data-pipeline
symptom: Dashboard stuck on the "Sorting vacancies…" spinner with Liked/Unreviewed/Passed all 0
root_cause: A data.js regeneration run from inside a git worktree (empty DB) writes to the MAIN checkout's public/data.js
---

## Problem

The local dashboard hung on the **"Sorting vacancies…"** overlay, with Liked 0 / Unreviewed 0 / Passed 0. `public/data.js` in the main checkout was 20 bytes — `var VACANCY_DATA={}` — instead of the ~8 MB snapshot with 1500+ vacancies.

## Root cause

`python3 scripts/fetch_vacancies.py --report-only` was run from inside a **git worktree** (`.claude/worktrees/...`) during a smoke test. Two facts combine:

1. The report writer resolves the **main** repo root (via `git rev-parse --git-common-dir`, the project's worktree-cache rule), so it writes `public/data.js` into the **main** checkout, not the worktree.
2. A worktree has no `.env` / real database, so `--report-only` reads an **empty** local SQLite and regenerates `data.js` as `var VACANCY_DATA={}`.

Result: a worktree smoke test silently overwrote the real dashboard snapshot in main. The database itself was never touched — only the generated static snapshot.

## Solution

Regenerate `data.js` from the real database, **in the main checkout** (where `.env` / the populated DB live):

```bash
cd /path/to/main/checkout        # NOT the worktree
python3 scripts/fetch_vacancies.py --report-only
# -> public/data.js rebuilt from the real DB (e.g. 1530 vacancies)
```

Then refresh the browser — the dashboard server already serves the regenerated file.

## Prevention

- **Never run `--report-only` (or anything that regenerates `public/data.js`) from a worktree.** It writes to main with the worktree's empty DB and wipes the snapshot. Do data-pipeline runs from the main checkout only.
- `public/data.js` is a disposable generated artifact (gitignored) — losing it is always recoverable by regenerating from the DB, so check the DB is intact before panicking.
- Worktrees are for editing code (runbooks, scripts, tests), not for running the live pipeline against shared state.
