# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Vacancy triage

### Triage
The review flow where the user turns liked vacancies into decisions — apply, research, network, or skip. Runs on the dashboard's Triage board (and a thin terminal equivalent); each decision is recorded as a stored vacancy status.

### Liked basket
The set of vacancy statuses that mean active interest: liked itself plus the decision statuses that follow it (to apply, to research, to network, applied). Distinct from the passed basket (declined or skipped) and from unseen (never reviewed). Basket membership, not the individual status, decides which side of the dashboard a vacancy appears on.

### Expired (derived state)
A display-only triage classification: a liked-basket vacancy that is no longer actual, because its deadline has passed or its source stopped confirming it. Computed at render time from the vacancy's own fields and never written to the database — the underlying status survives, nothing can be dragged into the state, and the classification reverses itself if the role reappears at the source.
*Avoid:* expired as a stored status.

A vacancy in this state is a review queue item, not a verdict: staleness has false positives (see Stale), so the user dismisses each one explicitly.

### Expiring (protected status)
A stored vacancy status for a high-scoring role that disappeared from its source before the user made a decision on it. Protection keeps it visible for an explicit decision (surfaced in Today) instead of letting it be silently archived. Distinct from Expired: expiring is stored and pre-decision; expired is derived and applies after the user has already liked the role.

### Stale
A source-freshness signal: the vacancy's source has not re-confirmed the role for longer than the stale window. Staleness suggests the role is likely closed, but it is weaker evidence than a passed deadline — a source also goes stale when a fetcher misses a role that is still live.

## Storage & migrations

### Full mode
The canonical way to run the pipeline: a hosted Postgres database is the source of truth, and the hosted dashboard and messaging digest are available. Product behavior must never branch on which mode is active — mode differences are limited to infrastructure.

### Simple mode
The zero-signup demo path: a local SQLite database that auto-creates on first use. Parity with full mode is a tested promise, not an aspiration — remaining differences are documented explicitly, and a crash on this path counts as a real bug, not a demo limitation.

### Dialect pair
A single logical schema migration shipped as two files, one per SQL dialect, sharing a version number. Not every version has both halves — a version may legitimately exist for only one dialect.

Behavioral rule: when a version exists for only one dialect, each database of the other dialect permanently records that version in its migration ledger as an applied no-op. A version number that has shipped for either dialect may therefore never be reused to add the missing counterpart later — upgraded databases would skip it forever. The counterpart takes the next number free in both dialect trees, even though the pair then diverges cosmetically.

### Migration ledger
The per-database record of which migration versions are resolved for that database — either genuinely applied, or marked not-applicable because the version belongs to the other dialect. Resolution is permanent: the runner never revisits a recorded version, which is what makes ledger state (not just schema state) part of the upgrade contract.

### Tombstone
A record that keeps a deliberately removed vacancy from coming back. Written when a vacancy is archived for scoring below threshold or for disappearing from its source; on re-encounter, the save layer sees the tombstone and skips the row instead of resurrecting, re-scoring, and re-archiving it. A renamed variant of a buried role inherits the block, so retitling does not resurrect it. Distinct from Expiring: an expiring role is protected and awaits a decision; a tombstoned role has been decided against or dropped.

## Learning cycle

### Factor strength
The declared force of a user taste factor: **filter** (a hard block — the role is dropped before scoring), **penalty** (subtracts points during scoring), or **note** (display-only — never reaches the scorer, never changes passage or score). The same factor is a filter for one user and a penalty for another; the strength is the user's choice. In the profile: filters live in `## HARD_FILTERS`, penalties in `## EXCLUDE_PATTERNS`, notes in `## NOTES`. A note fed to the scorer would silently become a penalty, so the scoring prompt is rendered without the notes section by construction.

### Learning review
The verdict-driven gate at the START of a run (before the fetch): it turns the verdicts accumulated since last time into PROPOSED corrections to the filters, scoring and board set. Skippable in a hurry. Every proposal is a yes/no; nothing edits itself, and each applied change is logged. Deterministic mechanics (proposals, backtest, rollover) are Python; the agent supplies only the user's yes/no.

### Rollover
The skip semantics of the learning review. A completed review writes a `reviewed` ledger row whose timestamp is the cursor; verdicts decided after the cursor are the undiscussed ones. Skipping writes no row, so the cursor does not move and the same verdicts reappear next run together with new ones — a skipped verdict is never lost.

### Not-mine vs garbage
Two distinct pass signals. **Not mine** is a plain `passed` status: the role was real and in-scope but not for this user — it calibrates scoring. **Garbage** is a filter hole: the role should never have reached scoring at all (it burned a scoring request for nothing) — it is recorded separately and feeds filter-word proposals, not scoring calibration.

### Backtest (clean)
The safety check every filter-word proposal must pass before it is offered. A candidate word is **clean** when it matches (whole-word) no title in the liked history AND no title of a vacancy scored ≥ 40 — i.e. adding it to the filter would have killed nothing good. A dirty candidate is not proposed; the exact roles it would have wrongly killed (its collisions) are shown instead. Pure string matching — no LLM.

## Flagged ambiguities

- "Expiring" had been used for both the stored protected status and for liked roles past their deadline (one Triage column mixed both) — these are distinct concepts; the derived display state is now called Expired.
