---
description: End-of-session finalization — regenerate public/data.js, commit changes, push to Vercel.
---

# /jobs-finish

Run at the end of any meaningful session (added a company, scored a batch, reconfigured a prompt).

## Steps

1. Regenerate the dashboard snapshot:
   ```bash
   python3 scripts/fetch_vacancies.py --report-only
   ```
   This rebuilds `public/data.js` with fresh data from Supabase.

2. Check `git status`. If there are no changes — stop and report.

3. Show `git diff --stat`. If more than 10 files changed — ask whether to commit everything in one commit.

4. Draft a commit message using conventional commits:
   - `feat:` — added a company, added a feature.
   - `fix:` — fixed a parser, fixed a script error.
   - `chore:` — updated dependencies, reformatted.
   - `docs:` — updated README or docs.
   - `data:` — only `public/data.js` changed (triage, scoring).

5. Stage specific files (never `git add .` — it risks capturing local caches and `.env` files).

6. Push to origin:
   ```bash
   git push
   ```

7. If the repository is connected to Vercel — deployment runs automatically. Show the URL: `https://<project>.vercel.app`.

## What NOT to commit

- `.env` — secrets.
- `config/user_profile.md` — personal data.
- `.firecrawl/` — scraper cache (gitignored).
- `vacancies/` — old archives (gitignored).
- `.claude-session-acceptance.md` — current session acceptance criteria.

All these paths are already in `.gitignore` — but if you accidentally staged them via `git add -A`, always check `git diff --staged` before committing.

## If push fails

- **Pre-commit hook failed**: fix the issue and make a new commit (not `--amend` — that risks losing work).
- **Conflict with remote**: `git pull --rebase` then push again.
- **Vercel did not deploy**: check the Vercel dashboard logs — usually a missing environment variable or a broken build step.
