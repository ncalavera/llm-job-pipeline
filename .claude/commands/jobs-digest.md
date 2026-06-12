---
description: Send a daily Telegram digest of top unseen vacancies and poll button responses back into Supabase.
---

# /jobs-digest

Sends the top unseen vacancies to a Telegram chat with inline Like/Pass buttons, then polls button presses back into `vacancy.status`.

## Prerequisites

Environment variables required (load via your secrets manager before running):
- `TELEGRAM_BOT_TOKEN` — bot token from BotFather
- `TELEGRAM_CHAT_ID` — target chat or user ID
- `SUPABASE_DB_URL` — Postgres connection string for Supabase

## Commands

### Send digest

```bash
source ~/.zshrc 2>/dev/null && python3 scripts/telegram_digest.py send
```

Picks the top 5 unseen vacancies by score, formats each as a message with inline buttons (Like / Pass), and sends them to the configured Telegram chat. Sets `vacancy.digest_sent_at` on each sent vacancy so they are not re-sent.

### Poll responses

```bash
source ~/.zshrc 2>/dev/null && python3 scripts/telegram_digest.py poll
```

Calls the Telegram `getUpdates` endpoint and processes callback query responses from the inline buttons. Writes button presses to `vacancy.status` (`liked` for Like, `passed` for Pass).

Run this once per polling cycle (e.g. every 5 minutes via cron) after the digest has been sent.

## Typical setup

Run `send` once a day at a scheduled time, then run `poll` on a short interval to catch responses:

```bash
# Example cron entries
0 9 * * * cd /path/to/project && source ~/.zshrc 2>/dev/null && python3 scripts/telegram_digest.py send
*/5 * * * * cd /path/to/project && source ~/.zshrc 2>/dev/null && python3 scripts/telegram_digest.py poll
```

Only one process should consume `getUpdates` at a time — running two pollers on the same bot token will cause missed updates.

## Notes

- `digest_sent_at` is set at send time — re-running `send` will not re-send already-sent vacancies.
- Inline button presses update `vacancy.status` directly in Supabase; no intermediate storage.
- The bot must have permission to send messages to the target chat.
