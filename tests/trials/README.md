# Persona trials

Six end-to-end trials, one per failure a first real user hit when a smart
non-engineer ran the pipeline cold. Each trial drives a synthetic persona
through the production loaders (a profile fixture via `USER_PROFILE_PATH`, a
temp SQLite DB, recorded/synthetic data) and asserts the persona-level outcome.
They are offline: no network, no live model. Scoring cost is measured as request
counts and prompt sizes, never a real API call.

```bash
python3 -m pytest tests/trials -q      # ~0.5s locally
```

Each trial is the **integration** slice: it ties a real fixture file to the
shipped behaviour and reproduces the original failure as one scenario. The
underlying mechanics stay unit-tested where they already are — the trials
compose those guards rather than re-implement them. A trial fails if the product
regresses to the first-test failure; it does not assert broken behaviour to stay
green.

Personas (invented, neutral — `tests/fixtures/`): `profile_engineer.md`,
`profile_designer.md`, `profile_ops_impact.md` (plus `profile_medic.md`, reused
from earlier work). Shared harness: `trial_harness.py`.

The manual half of each trial — the parts that need a live agent, a browser, or a
real Supabase project — lives in `docs/manual-trial-protocol.md`.

## Coverage map

Legend: **here** = asserted by this trial · **ref** = mechanics already
unit-tested elsewhere, composed here · **manual** = deferred to
`docs/manual-trial-protocol.md`.

### T1 — engineer from scratch (`test_trial_t1_engineer.py`)
Failure: public-good boards shipped as defaults + an EA worldview in the rubric
steered an engineer wrong.

- **here** No board auto-enabled on a fresh migrated clone; `JOB_BOARDS` unset → zero boards.
- **here** The engineer's board proposals exclude every impact/EA-only board (80k_hours, reliefweb, impactpool, idealist, consultants_for_impact) and include LinkedIn + an engineering board.
- **here** The rendered vacancy prompt is built from the engineer's field and carries no worldview frame (`WORLDVIEW_TOKEN`).
- **here** The same render path resolves a third disjoint field (the designer persona) to design, not the engineer's or medic's field — so "scored against the candidate's own field" is not a two-way special case.
- **ref** Board-recommendation mechanics — `test_profile_targeting.py`. EA-free company prompt — `test_company_scoring_profile_driven.py`.
- **manual** First onboarding via the questionnaire; a live fetch + score of real companies matching the field.

### T2 — $20 plan, peak day (`test_trial_t2_peak_day.py`)
Failure: scoring cost exhausted a $20 plan on a heavy day.

- **here** A seeded 988-vacancy day on a budget persona (Sonnet, shipped cap) stops at the spike-day cap (150).
- **here** "Scoring 150 of 988" + "Per-run cap reached (150)" shown — nothing silent.
- **here** Requests × prompt size stay under a per-run input-token budget; per-message description truncation holds.
- **ref** Cap-cut message + model defaults — `test_scoring_settings.py`. `[volume]` dial — `test_volume_settings.py`.
- **manual** Actual token spend against a live $20 plan; confirming Sonnet is chosen at onboarding.

### T3 — launch and walk away (`test_trial_t3_walk_away.py`)
Failure: operating the tool needed stage-order knowledge in the maintainer's head.

- **here** `STAGE_ORDER` is the canonical fixed sequence and every stage has a handler.
- **here** With nothing to decide, the judgment stages advance/skip — no gate; a clean run may publish.
- **here** The end-of-run summary explains every number in words, with real DB counts (no `?`).
- **ref** Gate/resume state machine — `test_run_daily.py`.
- **manual** A full real cycle start-to-finish with no interaction beyond the sanctioned gates.

### T4 — honest demo (`test_trial_t4_honest_demo.py`)
Failure: the SQLite-vs-Supabase split read as trivia with no guidance.

- **here** No `.env`, no Supabase → simple mode runs and round-trips a vacancy.
- **here** The backend banner names SQLite honestly (no false parity, no warning) and matches INSTALL-EASY.md verbatim.
- **here** The move-to-Supabase failure message ("psycopg2 is not installed") is present in both the code and the doc.
- **ref** Loader / no-psycopg2 mechanics — `test_env_loader.py`, `test_simple_mode_no_psycopg2.py`.
- **manual** Standing up a real Supabase project and following the upgrade path end to end.

### T5 — Russian user (`test_trial_t5_russian_user.py`)
Requirement: one language choice at onboarding drives the whole product.

- **here** A `## OUTPUT_LANGUAGE: Russian` profile flips all four surfaces at once — agent language, run banner + summary, digest, dashboard chrome.
- **here** Every English string key exists in Russian; load-bearing surfaces are genuinely translated.
- **ref** Resolver order + per-surface translation — `test_product_language.py`. Cyrillic-free English shell — `test_dashboard_no_cyrillic.py`.
- **manual** A real run in Russian: the agent's chat replies, a rendered digest, and the dashboard read as Russian to a native reader.

### T6 — overflow (`test_trial_t6_overflow.py`)
Failure: a flood of content with no obvious levers to shrink it.

- **here** A wide impact profile fans out to many boards, including the impact ones an engineer never gets (profile-driven both ways).
- **here** Under an overflow backlog, the run banner shows the volume dials AND the three cut levers together — suggestion only.
- **here** The "Today" cockpit is gated (`NEW_HIGH_FIT`) strictly above the catalog floor, so a big day can't dump into it.
- **ref** Banner volumes + overload lever copy — `test_volume_settings.py`.
- **manual** The dashboard under an overflow day: Today stays short, the learning screen renders its cut proposals.
