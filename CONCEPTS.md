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

## Flagged ambiguities

- "Expiring" had been used for both the stored protected status and for liked roles past their deadline (one Triage column mixed both) — these are distinct concepts; the derived display state is now called Expired.
