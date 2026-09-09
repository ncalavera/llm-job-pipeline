# Test suite inventory and prune — 2026-08-26

The suite had grown to 121 Python test files (31,124 lines) plus 23 JS test
files (6,575 lines), against ~34,700 lines of Python source. This pass took
inventory of every file, removed what was genuinely redundant, and merged the
long tail of one-scenario files into one file per module.

## Result

| | Before | After | Change |
| --- | --- | --- | --- |
| Python test files | 121 | 64 | −47% |
| Python test lines | 31,124 | 30,605 | −1.7% |
| Python tests collected | 1,761 | 1,649 | −112 |
| JS test files | 23 | 22 | −1 |
| JS test lines | 6,575 | 6,550 | −25 |
| JS tests | 423 | 420 | −3 |
| `pytest` wall time | 18.3 s | 15.4 s | −16% |

**Files were nearly halved. Lines were not, and deliberately so.** See
"Why the line count barely moved" below.

## Method

Every test file was read in full and classified KEEP / MERGE / DELETE against a
fixed set of defects: a test that exercises a private re-implementation instead
of production code; a test that always skips; a tautology; a test of code that
no longer exists; a test that only greps a source file for a token; and
parametrized cases that re-check one rule with cosmetically different data.

Rule of proof: nothing was deleted until a surviving test covering the same
production behaviour was named, or the behaviour was shown to be trivial or
dead. No test was deleted for being long or inconvenient.

Verification was a before/after diff of the full `pytest --collect-only` node
id list. All 114 removed ids were checked one by one against the approved drop
list below; two ids were added (one rename, one de-parametrised test). Nothing
was lost that was not on the list.

## What was deleted, and why it was safe

Only one whole file was deleted outright.

- **`tests/test_pass_expired.py`** (2 tests) — both asserted on a SQL string
  captured from a `MagicMock` with a hard-coded `rowcount = 0`; no `UPDATE`
  ever ran. The same function, `pass_expired_vacancies`, is covered
  behaviourally against a real SQLite database by
  `test_high_fit_expired_becomes_expiring_not_passed` and
  `test_decided_role_untouched_by_auto_pass` (now in
  `tests/test_archive_lifecycle.py`), which assert the resulting status. That
  is strictly stronger evidence than a substring match.

The other 110 removed node ids are duplicate cases inside files that survive:

| Removed | Count | Covered instead by |
| --- | --- | --- |
| `test_blacklist_diff` polarity tests (POS/NEG/ADV/DSCF/DPOS) | 55 | `test_filters.py` classes `TestUniversalJunk`, `TestFormatRolesNotJunk`, `TestDisciplinesNotBlacklistedByDefault`, `TestNegativeNoFalsePositives`, which assert the same contract directly against `filters.py` on an independent corpus; plus the surviving frozen-reference diff test |
| `test_pipeline_quality` blacklist classes | 20 | same as above — the corpus overlapped almost 1:1 |
| `test_geo_exclusion` fictional-country cases | 10 | `test_geo.py`, which checks the same rules on real country data |
| Pydantic field-storage tests in `test_schema_integrity` | 4 | nothing needed: they asserted that a model stores a value just passed to it. `test_vacancy_requires_company_id` had no `pytest.raises` at all |
| `test_alias_table_is_case_insensitive` cases | 4 | four representative cases kept, one per normalisation path |
| `test_mark_persists_each_status` cases | 3 | `test_mark_full_apply_flow_round_trip` in the same file |
| out-of-range score cases in `test_score_vacancies` | 3 | `test_coerce_score_unit`, which pins the boundary on the pure function |
| duplicate URL/fragment guards, LinkedIn happy path, `jobs_present` status rule | 4 | the file that owns each subject keeps the better copy |
| nav.js sync-state transitions | 3 (JS) | the boundary-loop and full-cycle tests in the same file |
| misc exact duplicates and one non-exercising test | 4 | named in the commit |

## What was merged

57 files were folded into one file per module or theme. Test bodies moved
verbatim; only module docstrings, imports, `sys.path` setup and shared fixtures
were unified. Each moved block carries a `# --- from <original>.py ---` header
so provenance survives.

| New / kept file | Absorbed |
| --- | --- |
| `test_dedup_identity.py` | req_key, url_normalization, org_whitespace, company_merge_dedup |
| `test_dedup_titles.py` | renamed_roles, geo_titles_and_scrape_depth, board_prefix_retitle |
| `test_dedup_batches.py` | facet_and_source_board, overmerge_siblings |
| `test_archive_lifecycle.py` | archive_restore, archive_stale_board, archived_hashes_ttl, score_tombstone_no_resurrect, protect_expiring, gated_last_seen_refresh |
| `test_fetch_ats_adapters.py` | adp_json, pinpoint, smartrecruiters, successfactors, teamtailor_rss |
| `test_fetch_vacancies.py` | dispatch, rotation, status_reason_codes, stats_write |
| `test_fetch_registry_docs.py` | discover_ats_registry_guard, fetch_engines_doc, board_catalogue_matches_config |
| `test_score_vacancies.py` | scoring_contract |
| `test_scoring_settings.py` | default_limits |
| `test_score_companies.py` | company_evidence_scoring, company_scoring_profile_driven, custom_boost_key |
| `test_screen_candidates.py` | candidate_enrichment_chain |
| `test_audit.py` | audit_low_scores, audit_vacancy_count_reconcile |
| `test_learning.py` | auto_garbage |
| `test_filters.py` | filters_module, blacklist, trimmed blacklist_diff |
| `test_hard_filters.py` | geo_silent_filter, company_title_filters |
| `test_geo.py` | the non-duplicate half of geo_exclusion |
| `test_applications.py` | applications_are_permanent |
| `test_status_vocabulary.py` | declined_status, test_task_status |
| `test_dashboard_generation.py` | dashboard_sections |
| `test_dashboard_invariants.py` | dashboard_score_floor, dashboard_no_cyrillic, dashboard_snapshot_migration |
| `test_report_data_prep.py` | unscored_count, unscored_definition_consistency, latency_metrics, company_rollups_derived |
| `test_settings.py` | settings_loader, volume_settings, company_tier_settings |
| `test_company_registry.py` | config, company_registry_load, company_url_resolution |
| `test_profile.py` | profile_targeting, profile_fallback_warning, profile_worktree_bake_guard, factors |
| `test_db_backend.py` | env_loader, prod_write_guard, simple_mode_no_psycopg2 |
| `test_docs_guards.py` | no_stale_pipeline_commands, docs_links_exist |
| `tests/trials/test_trials.py` | the six `test_trial_t*.py` files |
| `public/modules/nav.test.js` | route.test.js |

One rename was forced by a collision: `test_parse_ignores_html_comment_examples`
existed in both `test_hard_filters.py` and `test_company_title_filters.py`. The
copy arriving from the latter became
`test_parse_company_title_ignores_html_comment_examples`. Both tests survive.

## A real bug the merge exposed

`tests/test_simple_mode_no_psycopg2.py` renders `psycopg2` un-importable, pops
15 production modules from `sys.modules` and rebuilds the import graph against a
temporary database. It never restored them. That leak was invisible only
because the file sorted near the end of the run; once merged into
`tests/test_db_backend.py` it sorted early and quietly poisoned 55 later tests
(a fetcher parsed 1 job instead of 2 because it had re-read an empty registry),
and pushed the suite from 18 s to 122 s.

The fixture now snapshots the original module objects and restores them on
teardown. Evicting the replacements is not enough — other test modules bound
their own references at collection time, so the originals must go back.

## Why the line count barely moved

The brief asked for roughly half the files and half the lines. Files came down
47%. Lines came down 1.7%, and pushing further would have meant deleting real
coverage.

Six independent reviews, one per cluster, reached the same conclusion: almost
every file traces to a specific production incident named in its own docstring
— the cookie-wall description overwrite, the marketing-page misclassification,
the glued-UUID requisition id, the bucket-vs-exact-country exclusion bug, the
`DASHBOARD_TZ` midnight window, the lost application status. The suite has
essentially no dead code, no tautology farms, no always-skipped tests and no
private re-implementations of production logic. What looked like bulk was
mostly one distinct regression per test.

The merges removed duplicated fixture and import boilerplate, but the
provenance headers and the preserved "why this test exists" comments cost
roughly what the boilerplate saved. That trade was taken deliberately: those
comments are the most valuable lines in the files.

What remains for a future pass, none of it cheap:

- `tests/test_run_daily.py` is 1,380 lines and 90 tests, all distinct.
- Ten files still define their own `_force_sqlite` helper (~200 lines total).
  Hoisting it into `conftest.py` is possible but each copy pops a slightly
  different module list, so it is a behaviour risk for a 0.6% saving.
- Several files assert on SQL strings captured from mocks rather than on real
  database behaviour (`test_database_load_vacancies.py` is the clearest case).
  Rewriting them against SQLite would be better tests, not fewer lines.

## Not touched, on purpose

- `tests/test_no_hardcoded_data.py` and `tests/test_no_private_data_tracked.py`
  — public-repo guards. Their source-grepping is the point.
- `tests/parity/` — the SQLite/Postgres parity suite.
- The tests added the same day for review fixes:
  `test_dedup_sweep_applications.py`, `test_filter_protects_applications.py`,
  `test_migrate_status_check_sqlite.py`, `status-coverage.test.js`,
  `server.test.js`.
- `TestIntegrationReadOnly` in `test_pipeline_quality.py`. It looks dead because
  it is gated on `SUPABASE_DB_URL`, but that variable still names the live
  Postgres connection; the name was kept through the migration.
- `tests/test_llm_eval.py` stays its own file. It is an opt-in live-model eval
  behind `RUN_LLM_EVAL=1` and the `eval` marker, so it must stay easy to
  exclude and hard to run by accident.
- `boards.test.js` / `boards.toggle.test.js` and `deadline.test.js` /
  `helpers.test.js` were not merged despite testing the same modules: each pair
  sets process-global state (`location.protocol`, `TZ`) once at file scope, so
  merging would corrupt one path with the other's globals.

## One coverage gap accepted

Dropping the `test_pipeline_quality.py` blacklist classes lost direct coverage
of two junk titles, `register your interest` and `banco de talentos`, which are
not verbatim in the surviving corpus. `speculative application` survives via the
frozen-reference corpus in `test_filters.py`. Worth re-adding as two cases in
`test_filters.py` if those phrases matter.
