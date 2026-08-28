# The digest poller is gone (2026-08-28)

`jobsearch-digest-poller.service` used to run `telegram_digest.py poll --loop`
so 👍/👎 taps in Telegram became `liked` / `passed` on a vacancy.

Nikita asked for the buttons to be removed from the bot. The digest now carries
no buttons, nothing calls `getUpdates`, and the `poll` subcommand no longer
exists — so the unit had nothing left to run. It is deleted here rather than
left in place, because a deploy that copies `deploy/forge/*.service` would
otherwise quietly resurrect a service that immediately crashes on an unknown
subcommand.

On forge the unit is already stopped and disabled. If a copy is still on disk:

    systemctl --user disable --now jobsearch-digest-poller.service
    rm ~/.config/systemd/user/jobsearch-digest-poller.service
    systemctl --user daemon-reload

Verdicts are recorded on the dashboard (jobs.nikitasolovev.com). Telegram is
send-only.
