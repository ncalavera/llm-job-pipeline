---
description: Send or preview the daily screening summary in the configured Telegram bot.
---

# /jobs-digest

The daily driver sends one short message: new arrivals, roles ready to review,
roles awaiting preparation, failed preparations, and a link to the Screen view.
Keep, Put aside, Undo, and optional reasons live in the dashboard.

## Preview or send

```bash
python3 scripts/telegram_digest.py send --dry-run
python3 scripts/telegram_digest.py send
```

Preview does not send messages or update delivery state. A successful send advances
`last_digest_at`; a failed send leaves it unchanged for retry. The summary does not
mark individual vacancies as delivered.

Configuration comes from the existing environment: `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, the database connection, and `DASHBOARD_BASE_URL` (or the
`dashboard_base_url` setting). Change the shared bot configuration to route all
JobSearch notifications together, including nightly failures and mail notifications.

## Optional legacy reports

Only when explicitly requested:

```bash
python3 scripts/telegram_digest.py send --details --dry-run
python3 scripts/telegram_digest.py send --details
python3 scripts/telegram_digest.py alert --dry-run
```

`--details` retains scored vacancy lists and their delivery claims. `alert` checks
expiring scored roles. Neither is required for daily screening. The digest has no
response buttons or polling process; decisions are made in the dashboard.
