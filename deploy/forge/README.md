# Nightly run on the forge server

The pipeline runs unattended every night on a server the user controls
(STRATEGY.md guardrail 5). This directory holds the systemd units; everything
secret is placed on the server by hand and never enters git or any sync.

Layout on the server (user `nikita`):

| Path | What | Mode |
|---|---|---|
| `/home/nikita/Projects/personal/llm-job-pipeline` | checkout (HTTPS clone, no stored credential) + `.venv` | — |
| `/home/nikita/jobsearch/.env` | `SUPABASE_DB_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `FIRECRAWL_API_KEY` | 600 |
| `/home/nikita/jobsearch/claude-token` | long-lived token from `claude setup-token` | 600 |
| `config/user_profile.md` in the checkout | copied from the laptop (gitignored) | 600 |
| `vacancies/nightly/<date>/` | night logs and transcripts, pruned after 7 days | 700 |
| `~/.claude` | Claude Code state; session logs live under `projects/` | 700 |

## Install

1. Clone the repo over HTTPS as `nikita`, create the venv, install
   `requirements.txt`.
2. Create `/home/nikita/jobsearch/` with `.env` and `claude-token` (mode 600).
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
2. Replace the contents of `/home/nikita/jobsearch/claude-token` (mode 600).
3. Record the creation date below — rotate before a year passes.

| Token | Created |
|---|---|
| claude-token | (fill on install) |

The Telegram bot token rotates the same way via BotFather (`/revoke`), landing
in `.env`.

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
