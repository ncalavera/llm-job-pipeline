# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Daily review language

### Vacancy
One unique opening. Copies from different sources are the same vacancy; different openings on one careers page are not duplicates.
*Avoid:* row, result, card, or batch as a synonym for vacancy.

### Facts
What the posting says, supported by its wording. Missing information stays unknown.
*Avoid:* facts as a synonym for a model's opinion.

### Fit
How the facts compare with the candidate's profile: matches, gaps, and unknowns. Fit is an explanation, not a numerical score or a decision.

### Decision
The user's choice: Like or Pass. Preparation, filtering, and archiving do not manufacture a decision.
*Avoid:* verdict, keep, skipped, or put aside for these same user-facing choices.

### Inbox
The main scored catalogue of collected vacancies, with Undecided, Liked and Passed views. Rows sort by score by default; a missing score stays blank, never becomes zero. Preparation is a labelled property, never an inclusion gate. Bulk review uses the same records and content; filtered or paginated views are subsets of that table. Low-score reason batches are optional subsets: current, quoted required conditions with a possible profile conflict, grouped first by location/language/authorisation, otherwise experience/qualifications. Unclassified roles remain in All. Passing requires explicit selection; it preserves decided duplicate members and supports Undo. Application outcomes do not manufacture a Pass. Archives preserve historical records outside the active table.

### History
Older records and past decisions retained for reference and recovery. Moving a record out of the active inbox does not mean the user passed on it.

### Daily update
A short message linking to the inbox and stating its vacancy count. Preparation details belong to Health; research documents remain Reports.

## Dashboard entities and states

Each state answers one named question. States are mutually exclusive **within that question**; different questions are independent. A company can be tracked while its connection is missing. A vacancy can be liked while its preparation needs updating. Neither is a contradiction.

### Company
The organisation offering a vacancy. A company can have many vacancies and a direct careers source. Discovering a company on a job board does not connect its careers site.

**Personal selection:** Selected or Not selected. Only an explicit personal selection changes this; discovery and scores never select a company. Selection does not enable direct collection.

**Eligibility:** Eligible or Excluded. A personal block excludes all incoming vacancies and disables collection. Historical invalid/operational exclusions remain labelled with their reason; they must not be presented as personal decisions.

**Direct collection:** Configured separately from selection. Existing active sources keep their settings; board-only candidates are never fetched separately just because they are selected.

### Job board
A website listing vacancies from multiple companies. A board publishes a vacancy; the company offers it. The same vacancy may appear on several boards and on the company's careers site.

**Collection:** Enabled or Disabled. Enabled participates in scheduled collection; Disabled does not. A one-off explicit collection can override the schedule.

**Visibility:** Shown or Hidden. Hiding a board only changes the catalogue view; it does not disable collection or delete vacancies.

*Avoid:* board alone when referring to job sources; use Progress for the application columns.

### Source
The place from which a vacancy posting was collected: a job board or a company's careers site. A source is not the company itself.

**Connection:** Automatic (configured collector), Manual (requires a person), or Not connected. Being configured does not imply a successful check.

**Last check:** Never checked, Succeeded, or Failed. A successful check may find zero vacancies; zero is a result, not an error or missing data.

**Check freshness:** Unknown (no successful timestamp), Current (within the source's check interval), or Overdue (outside it). This describes checking the source, not whether an individual vacancy is open.

### Preparation
The saved Facts and Fit for one vacancy and one candidate profile.

**State:** Not prepared (no attempt), Needs update (saved preparation predates a posting/profile change), Ready (saved preparation matches both), or Failed (latest attempt failed). Processing details belong to Health. A failure never becomes a human Pass decision.

### Vacancy decision
**State:** Undecided, Liked, or Passed. Like records interest; Pass records the user's choice not to pursue. Undo restores the previous decision. Choosing a next step or submitting an application is later progress, not another synonym for Like.

Historical imports may contain automatic passes. Without a recorded human action, the stored legacy value alone is not evidence that the user personally rejected a vacancy.

### Progress
One current stage per vacancy: **Backlog → In progress → Applied → Interviewing**, followed by **Offer / invitation**, **Rejected**, or **Passed**. Not every application visits every stage.

Like places a vacancy in Backlog. In progress covers research, contacting people, drafting and preparing to submit. Applied means submitted and awaiting a response. Interviewing includes interviews and test tasks. Offer / invitation records an employer or programme accepting the application, not the candidate accepting an offer. Rejected means the employer declined; Passed means the user stopped pursuing it. Silence never becomes rejection.

Legacy storage aliases remain readable: `to_research` and `to_network` display as In progress; `test_task` displays as Interviewing. These are not additional user-facing stages.

### Application
The user's attempt to obtain a vacancy, programme place, grant, or another opportunity. Applying is a human action; the tool does not submit automatically. A submitted date is preserved through later progress changes.

Custom employer-specific steps and source-backed historical events belong to the application record; they do not create more Kanban columns. The vacancy page provides a plain-text editor for steps and dated notes; previous notes are preserved. Automatic status history starts when migration 0030 is installed and records when the system learned each change.

### Vacancy availability
**State:** Deadline passed (a known deadline is past), Not recently confirmed (no passed deadline, but the source has stopped confirming it), or No closure signal (neither condition). No closure signal is not a guarantee that applications are open. Availability never overwrites a decision or an application stage.

### Archive
Records removed from active collection/review, with a reason where available. Archived is a retention state, not Passed. Restoring a record returns it to review eligibility; it does not fabricate a Like decision. History is the wider collection of previous decisions and activity, including archives; the words are not interchangeable.

### Contact
A person the user may contact about the search. Companies and vacancies can link to contacts; a contact is not an application.

**Conversation state:** To contact, Awaiting reply, Replied, Met, Declined contact, or No longer following up. A later reply may reopen a conversation. These are contact states, not vacancy outcomes.

### Review note
A user's reason or correction accompanying a decision. **Review state:** Pending review or Reviewed. Saving a decision and reviewing its note are separate events.

### Score
An optional numerical model estimate, labelled Score everywhere. Older company scores describe company preference; vacancy scores describe vacancy preference. A score is neither a Fact, a Fit explanation, nor a human Decision. Unscored rows remain visible; daily discovery adds scores for ranking.

### Discovery
The nightly per-vacancy preparation pass. One request may return both a numeric
Score and the independent Facts/Fit preparation: unscored vacancies request
both, scored vacancies below 40 request only missing or stale Facts/Fit, and
vacancies scored at 40 or above are left untouched. Discovery never changes a
human Decision and never creates an exclusion rule.

### Views and counts
Inbox and Catalog refer to the same main scored vacancy table; filters can make it smaller. Preparation never determines inclusion. Progress groups liked vacancies and applications by current progress. Applications is the submitted-application table. Companies and Job boards count their own entities, never vacancies. Reports contains research documents; Contacts contains people; Health contains processing and connection details; Settings contains preferences.

A list count names its entity and uses that list's filters. A page count says how many are shown, not how many exist. Function groups partition the inbox, with mixed/unknown functions in Other. Attribute filters (language, location, seniority) may overlap and must never be presented as additive totals. Database record counts include source copies and history; they are not unique-vacancy counts. Cumulative funnel counts overlap and are not current states.

## Vacancy triage (legacy storage vocabulary)

### Triage
The review flow where the user turns liked vacancies into decisions — apply, research, network, or skip. Runs on the dashboard's Triage board (and a thin terminal equivalent); each decision is recorded as a stored vacancy status.

### Liked basket
The set of vacancy statuses that mean active interest: liked itself plus the decision statuses that follow it (to apply, to research, to network, applied, test task, interview, accepted). Distinct from the passed basket (declined or skipped) and from unseen (never reviewed). Basket membership, not the individual status, decides which side of the dashboard a vacancy appears on. Accepted sits here rather than in the passed basket: it is a closed outcome like declined, but the opposite answer, and counting a win as a rejection would teach scoring to downrank exactly the roles the search is for.

### Applications table
The Triage tab's second view of the same data: one row per application ever sent, newest first, with a send date, a stage, and how long it has been waiting. The board answers "where is each application"; the table answers "what have I sent, and what is waiting on whom" — questions a card layout cannot answer, because a card has no room for a date and the columns order by score, not time. Both views read one dataset through one dedupe, so they can never disagree about how many applications exist.

### Report
A research document written for this search, stored in the database and read on the dashboard's Reports tab: sector research, grant write-ups, company dossiers, the research done for one application. Its identity is the slug, derived from the source markdown filename, so re-importing an edited file updates that report instead of forking a second copy. The markdown is stored, never the rendered HTML — the source stays the thing that was written, and the renderer stays free to improve without a re-import.
*Avoid:* report as a synonym for the generated dashboard.

### Report kind
Which group a report appears under: research, sector, company, grant, or other. Inferred from the directories the source file sits in — never from its filename, which routinely contains a word like "research" for a document that is about a company. Unmatched is 'other', not a guess: a wrong kind hides a report in the wrong group, and 'other' at least tells the truth.

### Kind
What was applied to: job, programme, advising, consulting, grant, or course. Every scraped role is a job; the rest are applications the user sent that are not vacancies, recorded by hand (`vac add`) and stored as ordinary vacancy rows so the funnel counts them with everything else.
*Avoid:* a separate table for non-job applications.

### Send date
When an application actually went out (`vacancy.applied_at`), written once when a row first enters the application funnel and never overwritten. Distinct from `status_updated_at`, which moves with every stage — on a declined row that one holds the date of the rejection, so it can never stand in for a send date without lying. Where no send date was recorded, the display falls back to the stage date and marks the cell as an estimate.

### Expired (legacy term)
A past deadline or a source that has stopped confirming a vacancy. These are availability signals displayed on its card; neither moves a vacancy into a different progress column or changes its decision basket.

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
The additive matching layer that folds retitled re-listings of one role onto its existing row: seniority renames, punctuation and plural variants, trailing geo/work-mode/continent decorations, language copies sharing a description body, same-apply-URL retitles where one normalized title contains the other or the titles carry the same significant words (order-insensitive), and board-prefix retitles — one row's full title equals a substantial (>= 3 significant words) dash/comma segment of the other's, the way Idealist lists "Non-profit Entrepreneur — X" where the org's own page lists the bare "X". Board-prefix matches ignore the apply URL (each board links its own posting page) and let the description body guard alone decide distinctness; segment-vs-segment never matches (two roles decorated with one program name are two roles). Apply URLs compare in normalized form — tracking-only query params (utm and friends) and the fragment are stripped, job-identifying params kept — so the same requisition linked from two boards with different decorations reads as one req. Additive means the exact dedup hash formula never changes — stored rows and tombstones keep matching by their old hash. Stripping is vocabulary-driven on purpose: only segments provably not part of role identity (known cities, countries, continents, work modes) are removed, so a distinguishing qualifier like a portfolio name survives.
*Avoid:* fuzzy dedup (implies similarity scoring; this layer is exact keys over normalized forms).

### Sibling vacancy
A second, genuinely distinct role that shares company and title with an already-stored row. The first-seen role keeps the canonical dedup hash; the sibling is stored under a hash salted with its description fingerprint so both coexist and each re-matches its own row on later fetches. Fingerprints only count as a distinct-role signal when both bodies are comparably sized — a shallow board scrape of the same posting folds instead of forking a false sibling.

### Archived-hash tombstone
A recorded dedup hash of an archived vacancy that blocks the same role from being re-saved as new on a later fetch. Tombstones are exact-hash only by contract: tombstoning a normalized (cross-variant) key would also block the live spelling of the role, silently skip its refresh, and get it swept as stale.

## Applications

### Application dossier
The application entity's reason to exist (2026-07-04 decision): one record per submitted application that catalogues everything done for it — the stage history, timestamped free-text notes, and links to artifacts (CV version, cover-letter answers, research). A status-only applications view would merely duplicate Triage; the dossier is what the entity adds. Reachable from the company page, the vacancy page, and Triage.

### Application storage
Vacancy status is the canonical dashboard progress state. The application dossier stores private notes, custom steps, prior note versions and artifact references. Its legacy status field is not displayed as a second stage. Original submissions remain immutable in the materials catalogue.

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
The explicit board cleanup operation archives its undecided rows (unseen and unscored) with `status_reason='board_disabled'`; decided rows are untouched; each archived row is individually restorable; re-enabling the board refetches fresh listings. The dashboard collection toggle only changes collection; cleanup is a separate operation. Exists because board rows are outside gone-detection, so a disabled board's leftovers could otherwise never resolve.

## Dashboard design

### Shell and sheet
The dashboard's two-layer visual anatomy (2026-07 redesign): the **shell** is the continuous tinted background material — sidebar and header chrome sit directly on it with no borders — and each screen's data floats on a **sheet**, a rounded panel seated into the material with a hairline rim and whisper shadow. Separation comes from spacing and soft fills, never 1px rules or large drop shadows.

### Quality scale (color = meaning)
The fixed rule that every color on the dashboard means exactly one thing. Fit/quality reads green (≥70) → ochre (50–69) → crimson (<50) everywhere it appears — score tiles, tier badges, bars, distribution strips — and cobalt marks interaction only (active nav, selection, primary actions), never quality. New hues are not invented per feature.

### Calm coach (guardrail #10)
The UX stance adopted 2026-07-04: the dashboard is a calm coach, not a control panel — job search is stressful, so every screen must lower stress. Concretely: fewest visible decisions per screen (Hick's Law), one consistent row template with no redundant info (Cognitive Load), at most 1–2 highlighted elements per view (Von Restorff), empty and completion states that encourage rather than blame (Peak-End Rule), and writes that confirm instantly (Doherty threshold). Blocks and strips hide entirely when empty.

## Flagged ambiguities

- "Expiring" had been used for both the stored protected status and for liked roles past their deadline (one Triage column mixed both) — these are distinct concepts; the derived display state is now called Expired.

## Inbox contract (2026-09-08)

Inbox is one view over every retained vacancy, before relevance filtering. Screening preparation is optional content, never a visibility gate. Like and Pass are user decisions; employer rejection remains an application outcome. Main navigation is Inbox, Applications, Companies and Sources; Materials belongs under Applications.
### Company list views

- **Selected:** companies with an explicit personal dashboard selection. Automatic activation or board discovery does not count as selection. Historical records without confirmed personal selection remain in the catalogue.
- **Catalogue:** other eligible companies, initially showing those with relevant vacancies or application history. Search or “Include companies without relevant vacancies” exposes preserved research entries.
- **Excluded:** inactive records, including personal exclusions and retained invalid records.

These views do not change collection configuration: restoring a company to board eligibility does not enable direct collection.
