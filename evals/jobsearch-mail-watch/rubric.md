# jobsearch-mail-watch — rubric

The job reads new mail every 10 minutes and sends one Telegram alert per
hiring email it recognizes (`scripts/mail_watch.py`). This eval runs the
matcher (`classify` + `run_once`) against a fixed slice of fake messages —
same input, old code vs new code.

What matters, in order:

1. **No missed alert.** Every message the old output marked with a reason
   (`platform_domain`, `org_domain`, `subject:...`) must still get a reason
   in the new output. A message that flips to `no match` is the worst
   regression — a real recruiter email would go unseen.
2. **No false alert.** A message the old output marked `no match` (or
   excluded via `own_addresses`/`exclude_domains`) must not gain a reason in
   the new output — that would spam Telegram with noise (job boards,
   newsletters, the user's own replies).
3. **Same reason, or an equally correct one.** The specific reason string
   changing (e.g. `platform_domain` -> a different matched phrase) is only a
   regression if it points at the wrong rule, not if it is a harmless
   relabeling of the same correct verdict.
4. **Summary counts** (`listed`/`matched`/`sent`) should match the per-message
   verdicts one for one. A mismatch there (without a per-message change) is a
   bug in the run loop, not the rules — treat it as a regression.

Not a regression: comment/whitespace changes, log wording, or performance.
