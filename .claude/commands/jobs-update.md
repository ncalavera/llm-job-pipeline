---
description: Update the pipeline — pull latest code, refresh dependencies, apply pending DB migrations, summarise what changed.
---

# /jobs-update

Bring an existing install up to date safely. `git pull` alone is not enough:
new code can expect schema changes, so this command pulls **and** runs the
migration runner so the database matches the code.

Run this from the project root whenever you want the latest version.

## Steps

1. **Confirm the working tree is safe to update.** Run `git status`.
   - If there are uncommitted changes to tracked files, stop and show them. Ask
     whether to commit, stash (`git stash`), or abort. Never discard the user's
     changes automatically.
   - `.env`, `config/user_profile.md` and the local `data/` database are
     gitignored, so a pull never touches them — reassure the user of that.

2. **Record the current version** so the changelog is meaningful:
   ```bash
   git rev-parse --short HEAD
   ```

3. **Pull the latest code** (fast-forward only, so a diverged local branch fails
   loudly instead of producing a surprise merge):
   ```bash
   git pull --ff-only
   ```
   If it fails because the branch diverged, stop and report — let the user
   decide how to reconcile.

4. **Refresh Python dependencies** only if they changed since the old HEAD:
   ```bash
   git diff --name-only <OLD_SHA> HEAD | grep -q requirements.txt && \
     pip install -r requirements.txt
   ```
   If `requirements.txt` did not change, skip this and say so.

5. **Apply pending database migrations.** This works for both backends — it
   reads `SUPABASE_DB_URL` from the environment to decide SQLite vs Supabase,
   and it takes an automatic backup (`data/backups/`) before touching anything:
   ```bash
   python3 scripts/migrate.py
   ```
   Show its output. If it reports "Up to date", say there were no schema
   changes.
   - If it exits with **"ABORTED — destructive statements"** (exit 2), STOP.
     Do not re-run with `--allow-destructive` on the user's behalf. Show the
     listed migrations and let the user decide — that flag can drop or overwrite
     their data.
   - On SQLite, a failed migration auto-restores the database from the backup,
     so the data is never left half-migrated. Relay that if it happens.

6. **Summarise what changed** between the old and new HEAD so the user knows
   what they got:
   ```bash
   git log --oneline <OLD_SHA>..HEAD
   ```

7. **Remind about anything that needs a restart:**
   - If the local dashboard (`scripts/dashboard_local.py`) is running, it must be
     restarted to pick up code changes.
   - If full mode is in use and the change touched `public/` or the dashboard,
     redeploy the Node server on the VPS (pull the branch, `npm install --omit=dev`,
     restart the systemd unit — see MIGRATION.md).

## What this command never does

- Never `git add .` or commit on the user's behalf without asking.
- Never force-pull, reset, or discard local changes.
- Never touch `.env` or `config/user_profile.md`.
