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

## Fetching & save layer

### Silent zero
The failure class this project's guards exist to kill: a fetch or pipeline stage that fails but reports a successful empty result, indistinguishable from "genuinely nothing there". A blocked endpoint, an unregistered strategy, or a fallback value that a downstream gate rejects can all produce one; the required behavior is an explicit recorded error instead. The dishonesty often lives in the composition of healthy components, so it is hunted at the end-to-end level, not per unit.
*Avoid:* healthy zero, empty-but-ok (same concept).

### Junk gate
The save layer's content classification: descriptions that are boilerplate (cookie walls, error pages, navigation chrome) or too short to be a real posting are rejected or blanked before a vacancy row is stored. An empty description with a live URL deliberately passes the gate — that produces a blind vacancy — while a short one does not; the two bands have opposite outcomes, which is what makes a wrong fallback value dangerous.

### Blind vacancy
A saved vacancy with no usable description — the listing was real but the detail content could not be obtained. Blind rows keep their title, company, location, and link, remain visible and scoreable in degraded form, and are queued for an enrichment sweep that tries to fetch the missing description; hosts the scraper provably cannot reach are excluded from the sweep. A blind row heals when a later fetch supplies the description, or ages out after staying blind past its window.

### Cross-variant dedup
The additive matching layer that folds retitled re-listings of one role onto its existing row: seniority renames, punctuation and plural variants, trailing geo/work-mode/continent decorations, language copies sharing a description body, and same-apply-URL retitles where one normalized title contains the other or the titles differ only by connective words. Apply URLs compare in normalized form — tracking-only query params (utm and friends) and the fragment are stripped, job-identifying params kept — so the same requisition linked from two boards with different decorations reads as one req. Additive means the exact dedup hash formula never changes — stored rows and tombstones keep matching by their old hash. Stripping is vocabulary-driven on purpose: only segments provably not part of role identity (known cities, countries, continents, work modes) are removed, so a distinguishing qualifier like a portfolio name survives.
*Avoid:* fuzzy dedup (implies similarity scoring; this layer is exact keys over normalized forms).

### Sibling vacancy
A second, genuinely distinct role that shares company and title with an already-stored row. The first-seen role keeps the canonical dedup hash; the sibling is stored under a hash salted with its description fingerprint so both coexist and each re-matches its own row on later fetches. Fingerprints only count as a distinct-role signal when both bodies are comparably sized — a shallow board scrape of the same posting folds instead of forking a false sibling.

### Archived-hash tombstone
A recorded dedup hash of an archived vacancy that blocks the same role from being re-saved as new on a later fetch. Tombstones are exact-hash only by contract: tombstoning a normalized (cross-variant) key would also block the live spelling of the role, silently skip its refresh, and get it swept as stale.

## Applications

### Application dossier
The application entity's reason to exist (2026-07-04 decision): one record per submitted application that catalogues everything done for it — the stage history, timestamped free-text notes, and links to artifacts (CV version, cover-letter answers, research). A status-only applications view would merely duplicate Triage; the dossier is what the entity adds. Reachable from the company page, the vacancy page, and Triage.

### Stage machine
The application lifecycle: sent → interview (repeatable; each round is a timeline entry) → offer / rejected / ghosted (terminal). "Sent" is created automatically by "mark applied"; every other transition is a manual user action. Follow-ups are notes on the timeline, not a stage.

### Ghosted
Terminal application stage meaning the employer went silent and the user gave up waiting. Manual-only: an application silent for more than 21 days shows a derived "mark ghosted?" nudge, but the system never sets the stage itself.
*Avoid:* auto-ghosting, ghosted as a computed state.

## Company scoring

### Earned candidate
A board-discovered candidate company that has justified paid research: at least one of its vacancies scored at/above the `company_paid_min_vacancy_score` floor (60) or was liked. Only earned candidates enter the paid enrichment chain (URL search, about-page scrape, evidence collection) and the cheap relevance screen; unearned candidates are free name-only rows that simply wait. Replaced the company-first queue that once sent 97 strangers into paid research at once (2026-07-08).

### Money valve
The failure rule for the cheap relevance screen (2026-07-08): if the screen crashed — as opposed to running and keeping everything — ALL paid enrichment is withheld that cycle, the run records a blocking warning, and the publish gate keeps the previous dashboard snapshot. Inverts the old fail-open behavior where a crashed screen meant "research everyone". Candidates are never dropped by the valve; they wait for the next healthy run.

### Banded verdict (WANT total)
The WANT total is a holistic banded judgment (90–100 exceptional, 80–89 strong, …) made across all seven dimensions at once — deliberately NOT the arithmetic mean of the dimension scores. The bands plus the spread mandate exist to prevent score compression in a curated pool; drift between the total and the dimension average is expected behavior, and the UI says so.
*Avoid:* "fixing" the total to equal the dimension mean.

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

### Board-disable archive
The board lifecycle rule (2026-07-04): disabling a board immediately archives its undecided rows (unseen and unscored) with `status_reason='board_disabled'`; decided rows are untouched; each archived row is individually restorable; re-enabling the board refetches fresh listings. Exists because board rows are outside gone-detection, so a disabled board's leftovers could otherwise never resolve.

## Dashboard design

### Shell and sheet
The dashboard's two-layer visual anatomy (2026-07 redesign): the **shell** is the continuous tinted background material — sidebar and header chrome sit directly on it with no borders — and each screen's data floats on a **sheet**, a rounded panel seated into the material with a hairline rim and whisper shadow. Separation comes from spacing and soft fills, never 1px rules or large drop shadows.

### Quality scale (color = meaning)
The fixed rule that every color on the dashboard means exactly one thing. Fit/quality reads green (≥70) → ochre (50–69) → crimson (<50) everywhere it appears — score tiles, tier badges, bars, distribution strips — and cobalt marks interaction only (active nav, selection, primary actions), never quality. New hues are not invented per feature.

### Calm coach (guardrail #10)
The UX stance adopted 2026-07-04: the dashboard is a calm coach, not a control panel — job search is stressful, so every screen must lower stress. Concretely: fewest visible decisions per screen (Hick's Law), one consistent row template with no redundant info (Cognitive Load), at most 1–2 highlighted elements per view (Von Restorff), empty and completion states that encourage rather than blame (Peak-End Rule), and writes that confirm instantly (Doherty threshold). Blocks and strips hide entirely when empty.

## Flagged ambiguities

- "Expiring" had been used for both the stored protected status and for liked roles past their deadline (one Triage column mixed both) — these are distinct concepts; the derived display state is now called Expired.
