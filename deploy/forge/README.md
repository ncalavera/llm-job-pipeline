# Nightly run on the forge server

The pipeline runs unattended every night on a server the user controls
(STRATEGY.md guardrail 5). This directory holds the systemd units; everything
secret is placed on the server by hand and never enters git or any sync.

The unit files ship with a placeholder Linux user, `jobsearch`. Before
copying them, run the `sed` command in step 1 of Install below to swap in
your actual Linux user — do this first, since every path in this doc uses
the placeholder.

Layout on the server (user `jobsearch` — replace with your own):

| Path | What | Mode |
|---|---|---|
| `/home/jobsearch/Projects/personal/llm-job-pipeline` | checkout (HTTPS clone, no stored credential) + `.venv` | — |
| `/home/jobsearch/jobsearch/.env` | `SUPABASE_DB_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `FIRECRAWL_API_KEY` | 600 |
| `/home/jobsearch/jobsearch/claude-token` | long-lived token from `claude setup-token` | 600 |
| `config/user_profile.md` in the checkout | copied from the laptop (gitignored) | 600 |
| `vacancies/nightly/<date>/` | night logs and transcripts, pruned after 7 days | 700 |
| `~/.claude` | Claude Code state; session logs live under `projects/` | 700 |

## Install

1. Clone the repo over HTTPS as your dedicated Linux user, create the venv,
   install `requirements.txt`. Then replace the `jobsearch` placeholder in
   the unit files with that user's actual name:
   `sed -i "s|/home/jobsearch|/home/$USER|g; s|^User=jobsearch|User=$USER|" deploy/forge/*.service deploy/forge/*.timer`.
2. Create `/home/$USER/jobsearch/` with `.env` and `claude-token` (mode 600).
   The token comes from `claude setup-token` run on any logged-in machine.
3. Copy `config/user_profile.md` from the laptop into the checkout.
4. Apply migrations: `.venv/bin/python scripts/migrate.py`.
5. Set Claude Code retention and privacy on forge:
   `chmod 700 ~/.claude` and `"cleanupPeriodDays": 7` in `~/.claude/settings.json`.
6. Copy both unit files to `/etc/systemd/system/`, then
   `sudo systemctl daemon-reload`.
7. Smoke-test one night by hand: `sudo systemctl start jobsearch-nightly.service`,
   watch `journalctl -u jobsearch-nightly -f` (one-line summaries only) and the
   night directory.
8. Enable the timer only after the standalone test protocol
   (`docs/runbooks/nightly-standalone-test.md`) passes:
   `sudo systemctl enable --now jobsearch-nightly.timer`.

## Token rotation

The `claude setup-token` token is long-lived but not eternal, and a leaked or
expired token shows up as the "login failure" alert in the morning message.

1. Run `claude setup-token` on the laptop; copy the printed token.
2. Replace the contents of `/home/$USER/jobsearch/claude-token` (mode 600).
3. Record the creation date below — rotate before a year passes.

| Token | Created |
|---|---|
| claude-token | (fill on install) |
| gmail token (`~/Projects/tools/google-vibe-api/.secrets/token.json`) | (fill on install) |

The Telegram bot token rotates the same way via BotFather (`/revoke`), landing
in `.env`.

## Mail watcher (recruiter mail → Telegram)

`scripts/mail_watch.py` polls Gmail every 10 minutes (all incoming mail of
the last two days, not only the inbox — filters archive most mail on arrival) and sends one Telegram
message per incoming hiring email (recruiter, applications team, ATS
platform). Rules, not a model, decide: sender domains and subject phrases in
a small TOML file. Design: `docs/plans/2026-09-04-1340-feat-recruiter-mail-telegram-alert-plan.md`.

Install (after the nightly run is in place):

1. `uv pip install --python .venv/bin/python google-api-python-client google-auth`
   (the venv has no pip of its own).
2. The Gmail token comes from `google-vibe-api` on the laptop: copy only
   `~/Projects/tools/google-vibe-api/.secrets/token.json` to the same path on
   the server (directory 700, file 600). Nothing else from that checkout is
   needed; the unit reads the file through `LoadCredential=`.
3. `cp config/mail_watch_rules.example.toml /home/$USER/jobsearch/mail_watch_rules.toml`
   (mode 600), then edit it: your own addresses in `own_addresses`, the
   organisations you applied to in `org_domains`. One line per entry; no code
   change, the next run picks it up.
4. Substitute the user placeholder in both unit files (same `sed` as step 1
   of the nightly install), copy them to `/etc/systemd/system/`,
   `sudo systemctl daemon-reload`.
5. Replay the last 90 days by hand and fix the rules until every real hiring
   email shows a reason and no newsletter does:
   `MAIL_WATCH_RULES=~/jobsearch/mail_watch_rules.toml .venv/bin/python scripts/mail_watch.py --dry-run --since-days 90`
6. Seed: `sudo systemctl start jobsearch-mail-watch.service`. The first run
   with no state file records the current inbox and sends nothing.
7. Enable: `sudo systemctl enable --now jobsearch-mail-watch.timer`.

Runbook:

| Sign | Meaning | First move |
|---|---|---|
| No alerts for a day while hiring mail arrived | timer stopped, or the rules miss the sender | `systemctl list-timers jobsearch-mail-watch*`, `journalctl -u jobsearch-mail-watch -n 30`; add the domain to the rules |
| "mail watcher is failing (N runs in a row)" | three consecutive runs failed; the message carries the error | `journalctl -u jobsearch-mail-watch -n 30` |
| "... failing" with `invalid_grant` or 401 | Gmail token expired or revoked | re-authorise `google-vibe-api` on the laptop, copy `token.json` to the server (mode 600), note the date in the token table |
| "rules file ... missing keys" or a TOML parse error | a hand edit broke the rules file | fix the file; every run until then counts as failed |

State: `/home/$USER/jobsearch/mail_watch_state.json` — seen message ids with
their dates (pruned after 7 days), failure counter, last error. Delete the
file to re-seed; do not delete it to "re-send", that sends nothing.

## Night sizing

The shipped `[nightly]` defaults size a scoring-only night:
`max_items_per_night` 120 (one budget shared by every gate, spent by scoring
first), `vacancy_gate_minutes` 120 and `run_deadline_minutes` 225. Screening
preparation (`[screening] nightly_limit`, 400 roles over `window_days` 14)
runs after scoring inside the same budget and deadline, so the defaults
starve it. Raise them on the server only, never in the tracked file:

1. Copy `config/defaults.toml` to `/home/$USER/jobsearch/defaults.toml` and
   edit `[nightly]`: `max_items_per_night = 600`; `vacancy_gate_minutes` and
   `run_deadline_minutes` from the live slice (minutes per role × cohort ÷
   concurrency, plus 20 percent).
2. Add `DEFAULTS_TOML_PATH=/home/$USER/jobsearch/defaults.toml` to
   `/home/$USER/jobsearch/.env` (the unit's `EnvironmentFile`). The loader
   reads that file INSTEAD of the checkout's copy — it is a full replacement,
   not a merge — so re-copy it after every pull that touches `defaults.toml`.
3. Raise the unit's hard ceiling to match the new deadline with a drop-in,
   `sudo systemctl edit jobsearch-nightly.service`:

   ```ini
   [Service]
   TimeoutStartSec=5h30m
   ```

   then `sudo systemctl daemon-reload`. Start time plus ceiling must stay
   before the 03:35 UTC database backup: from the 22:00 UTC timer that is
   5h30m at most; a run started by hand later in the evening gets less.

## Database user

The night session runs with the `jobsearch_app` role. Its grants are the real
security boundary for anything the headless session could do: SELECT, INSERT,
UPDATE on the pipeline tables — **no DELETE, no TRUNCATE, no DDL**. Record any
grant change here. The prod-write guard in `db_backend.py` is a convenience
fence for scripts, not a boundary against a shell.

## Failure signals (runbook)

| Morning sign | Meaning | First move |
|---|---|---|
| No message at all by 09:00 Tbilisi | wrapper never ran or alert send failed | `systemctl status jobsearch-nightly`, `journalctl -u jobsearch-nightly` |
| "Night run failed at <stage>" | driver aborted; data before the stage is safe | read `vacancies/nightly/<date>/driver.log` |
| "login failure" | Claude token expired or invalid | rotate the token (above) |
| "no progress" header | session ran but saved nothing | read `claude-<gate>.err` and the transcript |
| Counts show rising carry-over two nights running | scoring starved (usage window, cap) | check `[nightly] max_items_per_night`, Claude usage; "daytime session throttled" is the named signal that night runs ate the shared Max-plan window |
| "scoring skipped" | whole-run deadline hit before a gate | usually a long fetch; check timings in `wrapper.log` |
