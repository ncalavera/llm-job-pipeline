#!/usr/bin/env python3
"""
Vacancy Monitoring System
Fetches jobs from tracked orgs, scores relevance, generates HTML dashboard.

Usage:
    python3 scripts/fetch_vacancies.py                                  # Full run (all orgs + boards)
    python3 scripts/fetch_vacancies.py --free-only                      # Free ATS only (no credits)
    python3 scripts/fetch_vacancies.py --report-only                    # Regenerate dashboard only
    python3 scripts/fetch_vacancies.py --companies "Acme Foundation,Example Org"  # Specific companies
    python3 scripts/fetch_vacancies.py --tier S --no-boards             # S-tier companies only
    python3 scripts/fetch_vacancies.py --strategy greenhouse            # All Greenhouse companies
    python3 scripts/fetch_vacancies.py --force-all                      # Fetch ALL companies (ignore TTL)
    python3 scripts/fetch_vacancies.py --auto-score                     # Auto-filter + score after fetch
    python3 scripts/fetch_vacancies.py --boards-only                    # Job boards only

Module structure (for parallel-safe development):
    config.py       — paths, COMPANIES, keywords
    fetchers.py     — Greenhouse API, Firecrawl scraper, markdown parser
    database.py     — scoring, DB persistence, merge logic
    report/         — Dashboard generation
        __init__.py     — assembler (generate_dashboard)
        data_prep.py    — grouping, stats
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime

import run_status  # lightweight progress heartbeat (vacancies/run_status.json)


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser. Defined before the heavy project imports so
    ``--help`` / ``-h`` prints usage without connecting to the database or
    loading the user profile (those happen only when a real command runs)."""
    parser = argparse.ArgumentParser(description="Fetch job vacancies from configured sources")
    parser.add_argument(
        "--free-only",
        action="store_true",
        help="Only fetch from Greenhouse orgs (no Firecrawl credits)",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Regenerate the dashboard data (public/data.js) from the database",
    )
    parser.add_argument(
        "--list-manual",
        action="store_true",
        help="Print manual-check companies and exit (no fetching)",
    )
    parser.add_argument(
        "--companies",
        type=str,
        default="",
        help="Comma-separated company names (case-insensitive, alias-aware)",
    )
    parser.add_argument(
        "--strategy", type=str, default="", help="Fetch only companies using this ATS strategy"
    )
    parser.add_argument(
        "--force-all", action="store_true", help="Fetch ALL companies (ignore TTL cooldown)"
    )
    parser.add_argument(
        "--tier",
        type=str,
        default="",
        help="Fetch only companies of this calculated tier (S, A, B, C)",
    )
    parser.add_argument(
        "--boards-only", action="store_true", help="Fetch only job boards, skip companies"
    )
    parser.add_argument(
        "--no-boards", action="store_true", help="Skip job boards, fetch companies only"
    )
    parser.add_argument(
        "--auto-score",
        action="store_true",
        help="Auto-run filter + score after fetch (if new vacancies found)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help=(
            "Fetch only — do NOT regenerate the dashboard. Used by the run_daily "
            "driver, which owns the gated publish step (see scripts/run_daily.py)."
        ),
    )
    parser.add_argument(
        "--no-auto-enrich",
        action="store_true",
        help=(
            "Skip the inline candidate-company enrichment/scoring after a fetch. "
            "The run_daily driver passes this because its company_scoring stage "
            "owns the canonical chain (find site → collect evidence → WANT-score); "
            "running it here too would be a degraded parallel path."
        ),
    )
    return parser


# Print help and exit BEFORE importing anything that touches the DB or profile.
from cli_help import wants_help

if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

from config import (
    COMPANIES,
    JOB_BOARDS,
    PUBLIC_DIR,
    FETCH_LOG_DIR,
    resolve_canonical_name,
)
from fetchers import (
    fetch_greenhouse,
    fetch_firecrawl_scrape,
    fetch_workday_api,
    fetch_algolia_board,
    fetch_firecrawl_board,
    fetch_lever,
    fetch_ashby,
    fetch_workable,
    fetch_reliefweb_board,
    fetch_impactpool_board,
    fetch_datadotorg_board,
    fetch_arbeitnow_board,
    fetch_remotive_board,
    fetch_wwr_board,
    fetch_hn_whoishiring_board,
    fetch_idealist_board,
    fetch_fastforward_board,
    fetch_linkedin_board,
    fetch_unops_widget,
    fetch_recruitee,
    fetch_teamtailor_rss,
    fetch_bamboohr,
    fetch_amazon_jobs,
    fetch_successfactors,
    fetch_adp_json,
    get_firecrawl_change_statuses,
    get_scrape_statuses,
)
from database_supabase import (
    get_conn,
    save_vacancies,
    archive_gone_vacancies,
    should_fetch_board,
    mark_board_fetched,
    save_board_vacancies,
    sync_boards,
    update_source_tracking,
    print_reconciliation_report,
    pass_expired_vacancies,
    is_fetch_error,
)

# Strategies whose fetch returns the company's COMPLETE current listing —
# safe to treat "absent from fetch" as "closed at source". Excluded:
# firecrawl_scrape (change tracking returns [] when the page is unchanged)
# and amazon_jobs (search-based, may return a subset).
GONE_DETECTION_STRATEGIES = {
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "teamtailor_rss",
    "bamboohr",
    "workday_api",
    "unops_widget",
}
from report import generate_dashboard

# Per-run fetch telemetry the run_daily driver reads for its publish gate: a
# truncated ATS fetch (HTTP 200, partial list) mass-archives an org's live roles,
# so the gate blocks publishing when gone-from-source archival is a large share
# of any single org this run. gitignored (vacancies/), pure runtime state.
FETCH_STATS_PATH = PUBLIC_DIR.parent / "vacancies" / "fetch_stats.json"


def _write_fetch_stats(stats: dict) -> None:
    """Persist per-run fetch telemetry for the driver's publish gate.

    Best-effort: a failed stats write must never abort the real fetch (the gate
    treats a missing file as "no gone-archive signal", i.e. it does not block on
    telemetry it cannot read)."""
    try:
        FETCH_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = FETCH_STATS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(FETCH_STATS_PATH)
    except Exception:
        pass


def _load_enrichment_tiers() -> dict[str, str]:
    """Load company tiers from Supabase enrichment data.

    Returns dict: canonical_name → tier_letter (or missing key if no data).
    """
    from database_supabase import load_all_enrichment

    enrichment = load_all_enrichment()

    tiers: dict[str, str] = {}
    for name, data in enrichment.items():
        mission = data.get("mission_fit", {})
        alignment = mission.get("alignment_score")
        if alignment is None:
            continue
        composite = alignment
        if composite >= 65:
            tiers[name] = "S"
        elif composite >= 50:
            tiers[name] = "A"
        elif composite >= 35:
            tiers[name] = "B"
        else:
            tiers[name] = "C"
    return tiers


def _save_fetch_log(source_key: str, jobs: list, fetch_status: str = "ok") -> None:
    """Save raw fetch results to vacancies/fetch_log/{date}/{source_key}.json.

    Enables debugging bad parses and comparing responses across days.
    Large files — excluded from git via .gitignore.
    """
    log_dir = FETCH_LOG_DIR / datetime.now().strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{source_key}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "fetch_status": fetch_status,
                "count": len(jobs),
                "jobs": jobs,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )


def _resolve_fetch_status(fetch_status: str, has_jobs: bool, scrape_status: str | None) -> str:
    """Disambiguate an empty fetch into a specific reason code (U9 / WS6).

    A successful fetch that yielded no jobs is not automatically "broken":

      * scraper flagged a JS-rendered shell   -> ``js_required``   (unchanged)
      * firecrawl skipped because credits == 0 -> ``credit_exhausted``
      * a real, successful, empty listing      -> ``render_ok_zero``

    Only rewrites the generic ``ok``; an ``error:``/other status and any run
    that returned jobs pass through untouched. This replaces the old ambiguous
    ``no_data`` so a monitor can tell genuinely-empty (``render_ok_zero``) from
    broken (``credit_exhausted``/``js_required``/``error:``).
    """
    if fetch_status != "ok" or has_jobs:
        return fetch_status
    if scrape_status == "js_required":
        return "js_required"
    if scrape_status == "credit_exhausted":
        return "credit_exhausted"
    return "render_ok_zero"


def _auto_enrich_candidates():
    """Auto-enrich + auto-review new candidate companies from board fetches.

    Pipeline: filter_companies (structural junk) → fetch_companies (scrape about)
              → score_companies (alignment) → auto_review (approve/reject by threshold).
    Grey-zone companies stay as candidate for user review on dashboard.
    """
    from database_supabase import get_conn, auto_review_candidates

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM company
        WHERE status = 'candidate' AND enriched_at IS NULL AND alignment_score IS NULL
          AND website IS NOT NULL AND website != ''
    """)
    unenriched = cur.fetchone()[0]
    cur.close()

    if unenriched == 0:
        return

    print(f"\n{'─' * 60}")
    print(f"  AUTO-ENRICH: {unenriched} new candidate companies")
    print(f"{'─' * 60}")

    # Step 1: Pre-filter structural junk (gov, university, aggregator, etc.)
    subprocess.run(
        [sys.executable, "scripts/filter_companies.py", "--apply"],
        check=False,
    )

    # Step 2: Scrape about pages via Firecrawl
    subprocess.run(
        [sys.executable, "scripts/fetch_companies.py", "--limit", str(unenriched)],
        check=False,
    )

    # Step 3: Score alignment via API (Sonnet — fast, cheap)
    subprocess.run(
        [sys.executable, "scripts/score_companies.py", "--api", "--limit", str(unenriched)],
        check=False,
    )

    # Step 4: Auto-review by threshold (approve ≥60, reject ≤25)
    result = auto_review_candidates()
    approved = result.get("approved", [])
    rejected = result.get("rejected", [])
    pending = result.get("pending", [])
    conn.commit()

    if approved or rejected or pending:
        print(
            f"  Auto-review: {len(approved)} approved, {len(rejected)} rejected, "
            f"{len(pending)} pending your review",
            flush=True,
        )


def _filter_companies(args) -> dict:
    """Filter COMPANIES dict based on CLI arguments.

    All filters combine with AND logic. Returns a subset of COMPANIES.
    """
    if args.boards_only:
        return {}

    filtered = dict(COMPANIES)

    # --companies "X,Y" → resolve via canonical names
    if args.companies:
        requested = [s.strip() for s in args.companies.split(",") if s.strip()]
        resolved = {}
        for name in requested:
            canonical = resolve_canonical_name(name)
            if canonical in COMPANIES:
                resolved[canonical] = COMPANIES[canonical]
            else:
                print(f"  WARNING: unknown company '{name}' (resolved: '{canonical}')")
        filtered = resolved

    # --strategy greenhouse → only that ATS strategy
    if args.strategy:
        filtered = {n: c for n, c in filtered.items() if c["strategy"] == args.strategy}

    # --tier S/A/B/C → filter by calculated tier (from enrichment data)
    enriched_tiers = _load_enrichment_tiers()
    if args.tier:
        tier_filter = args.tier.upper()
        filtered = {n: c for n, c in filtered.items() if enriched_tiers.get(n) == tier_filter}

    # TTL cooldown: skip companies fetched recently (default behavior)
    # --force-all or --companies disables this
    if not args.force_all and not args.companies:
        _TTL_BY_TIER = {"S": 3, "A": 5}  # days; default = 7
        conn = get_conn()
        cur = conn.cursor()
        stale = {}
        skipped_count = 0
        for n, c in filtered.items():
            cur.execute("SELECT last_fetched FROM company WHERE canonical_name = %s", (n,))
            row = cur.fetchone()
            if not row or not row[0]:
                stale[n] = c  # never fetched
            else:
                last_dt = row[0]
                days = (datetime.now(last_dt.tzinfo) - last_dt).days
                tier = enriched_tiers.get(n)
                ttl = _TTL_BY_TIER.get(tier, 7)
                if days >= ttl:
                    stale[n] = c
                else:
                    skipped_count += 1
        cur.close()
        if skipped_count:
            print(f"  Skipped {skipped_count} companies (fetched recently), fetching {len(stale)}")
        filtered = stale

    return filtered


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.boards_only and args.no_boards:
        parser.error("--boards-only and --no-boards are mutually exclusive")

    print("=" * 60)
    print("  Job vacancy fetch")
    print("=" * 60)

    from db_backend import print_backend_banner

    print_backend_banner()

    # --list-manual: show manual-check companies and exit
    if args.list_manual:
        manual = [
            (name, cfg) for name, cfg in COMPANIES.items() if cfg["strategy"] == "manual_check"
        ]
        if manual:
            print("\n⚠️  Manual check needed for:")
            for name, cfg in manual:
                print(f"  - {name}: {cfg['careers_url']}")
            print("\nVisit the sites and check for new vacancies.")
        else:
            print("\n✓ No companies require a manual check.")
        return

    conn = get_conn()
    total_new = 0
    # Per-run telemetry for the driver's publish gate (see FETCH_STATS_PATH).
    fetch_stats = {"orgs": {}, "errors": {}, "total_new": 0}

    if not args.report_only:
        filtered = _filter_companies(args)

        # Progress heartbeat: total = companies to fetch + boards to fetch. The
        # /jobs-new runbook backgrounds this run and polls run_status.json, since
        # foreground stdout stays invisible until the command exits.
        companies_total = 0 if args.boards_only else len(filtered)
        boards_total = len(JOB_BOARDS) if (JOB_BOARDS and not args.no_boards) else 0
        run_status.begin("fetch", companies_total + boards_total)

        if args.boards_only:
            print("\nSkipping companies (--boards-only mode)")
        elif not filtered:
            if not COMPANIES:
                print("\nNo companies tracked yet — nothing to fetch.")
                print("  Run /jobs-new to discover your first companies,")
                print("  or /jobs-add to add a company by name.")
            else:
                print(
                    "\nNo companies match these filters. "
                    "Relax --companies / --strategy / --tier, or drop them to fetch all."
                )
        else:
            print(f"\nFetching vacancies from {len(filtered)}/{len(COMPANIES)} organizations...\n")

        # Collect manual-check companies to show at the end
        manual_companies = []

        for org_idx, (org_name, config) in enumerate(filtered.items()):
            run_status.step(org_name, org_idx, new=total_new)
            strategy = config["strategy"]
            tier = config.get("tier")

            if strategy == "manual_check":
                manual_companies.append((org_name, config.get("careers_url", "")))
                continue

            if args.free_only and strategy not in (
                "greenhouse",
                "lever",
                "ashby",
                "workable",
                "unops_widget",
                "recruitee",
                "teamtailor_rss",
                "bamboohr",
                "amazon_jobs",
                "apple_jobs",
                "workday_api",
                "successfactors",
                "adp_json",
            ):
                print(f"  [{org_name}] Skipped (--free-only mode)")
                continue

            print(f"\n--- {org_name} (Tier {tier}) ---")

            jobs = []
            fetch_status = "ok"
            try:
                if strategy == "greenhouse":
                    jobs = fetch_greenhouse(org_name, config["slug"], eu=config.get("eu", False))
                elif strategy == "firecrawl_scrape":
                    jobs = fetch_firecrawl_scrape(
                        org_name, config["url"], url_filter=config.get("url_filter", "")
                    )
                elif strategy == "workday_api":
                    jobs = fetch_workday_api(
                        org_name, config["tenant"], config["board"], config["base_url"], config
                    )
                elif strategy == "lever":
                    jobs = fetch_lever(org_name, config["slug"])
                elif strategy == "ashby":
                    jobs = fetch_ashby(org_name, config["slug"])
                elif strategy == "workable":
                    jobs = fetch_workable(org_name, config["slug"])
                elif strategy == "unops_widget":
                    jobs = fetch_unops_widget(
                        org_name,
                        config["url"],
                        title_blacklist=config.get("title_blacklist"),
                        seniority_filter=config.get("seniority_filter"),
                        location_keywords=config.get("location_keywords"),
                        fetch_descriptions=config.get("fetch_descriptions", True),
                    )
                elif strategy == "recruitee":
                    jobs = fetch_recruitee(org_name, config["slug"])
                elif strategy == "teamtailor_rss":
                    jobs = fetch_teamtailor_rss(org_name, config["slug"])
                elif strategy == "bamboohr":
                    jobs = fetch_bamboohr(org_name, config["slug"])
                elif strategy == "amazon_jobs":
                    jobs = fetch_amazon_jobs(org_name, config)
                elif strategy == "successfactors":
                    jobs = fetch_successfactors(org_name, config)
                elif strategy == "adp_json":
                    jobs = fetch_adp_json(org_name, config)
            except Exception as exc:
                print(f"  [{org_name}] Fetch error: {exc}")
                fetch_status = f"error: {exc}"

            # Honest marking: disambiguate an empty result into a reason code
            # (js_required / credit_exhausted / render_ok_zero) — see U9.
            scrape_status = get_scrape_statuses().get(org_name)
            fetch_status = _resolve_fetch_status(fetch_status, bool(jobs), scrape_status)

            # Save raw fetch log
            _save_fetch_log(f"{strategy}_{org_name.lower().replace(' ', '_')}", jobs, fetch_status)

            # Full pre-filter listing — gone-detection must diff against what
            # the source actually lists, not the department-filtered subset.
            raw_jobs = jobs

            # Department exclusion filter (configured per-company in ats_config)
            dept_exclude = config.get("department_exclude", [])
            if dept_exclude and jobs:
                dept_exclude_lower = [d.lower() for d in dept_exclude]
                before = len(jobs)
                seen_depts = {j.get("department", "") for j in jobs if j.get("department")}
                new_depts = seen_depts - {d for d in dept_exclude}
                if new_depts:
                    print(f"  [{org_name}] new departments (not excluded): {sorted(new_depts)}")
                jobs = [
                    j for j in jobs if j.get("department", "").lower() not in dept_exclude_lower
                ]
                excluded = before - len(jobs)
                if excluded:
                    print(
                        f"  [{org_name}] department filter: {excluded}/{before} excluded, {len(jobs)} remaining"
                    )

            new_count = save_vacancies(org_name, tier, jobs)
            update_source_tracking(org_name, tier, strategy, new_count, fetch_status)
            total_new += new_count
            if new_count > 0:
                print(f"  [{org_name}] {new_count} NEW vacancies added")

            if is_fetch_error(fetch_status):
                fetch_stats["errors"][org_name] = fetch_status

            # Gone-from-source: a complete direct-ATS listing is ground truth.
            # An unseen vacancy missing from a successful fetch was closed.
            # 'render_ok_zero' is a successful empty listing (U9) and must still
            # trigger gone-detection — otherwise closed roles stay stale.
            if fetch_status in ("ok", "render_ok_zero") and strategy in GONE_DETECTION_STRATEGIES:
                gone = archive_gone_vacancies(org_name, raw_jobs)
                # Share = vanished-from-source / (vanished + still-live-this-fetch).
                # "Vanished" is archived PLUS protected-expiring (high-fit roles
                # that flip to 'expiring' instead of archived, but still left the
                # source) — otherwise a truncated fetch that happens to hit mostly
                # protected roles would undercount and slip past the gate. A
                # truncated fetch returns few live jobs but loses many → high
                # share → the driver's publish gate blocks a corrupting publish.
                protected = getattr(gone, "protected", 0)
                fetch_stats["orgs"][org_name] = {
                    "gone": int(gone or 0) + protected,
                    "live": len(raw_jobs),
                }

            # Commit per company so an interrupted run keeps finished orgs.
            get_conn().commit()

        # --- Job Board Aggregators ---
        manual_boards = []
        if JOB_BOARDS and not args.no_boards:
            print(f"\n{'─' * 60}")
            print("  JOB BOARDS")
            print(f"{'─' * 60}")

            # Keep the board catalog (name/strategy/tier/ttl/url) in sync with
            # config so the dashboard's Boards tab has a single source of truth.
            sync_boards(JOB_BOARDS)

            for board_idx, (board_id, board_cfg) in enumerate(JOB_BOARDS.items()):
                strategy = board_cfg["strategy"]
                board_name = board_cfg["name"]
                run_status.step(board_name, companies_total + board_idx, new=total_new)

                if strategy == "manual_check":
                    manual_boards.append((board_name, board_cfg["url"]))
                    continue

                is_free = board_cfg.get("free", False)
                if args.free_only and not is_free:
                    print(f"  [{board_name}] Skipped (--free-only, Firecrawl board)")
                    continue

                ttl_days = board_cfg.get("ttl_days", 3)
                if not should_fetch_board(board_id, ttl_days):
                    print(f"  [{board_name}] Skipped (recent, ttl={ttl_days}d)")
                    continue

                print(f"\n--- {board_name} (board, tier {board_cfg.get('tier', 'C')}) ---")

                jobs = []
                board_fetch_status = "ok"
                try:
                    if strategy == "algolia_api":
                        jobs = fetch_algolia_board(board_cfg)
                    elif strategy == "firecrawl_board":
                        jobs = fetch_firecrawl_board(board_cfg)
                    elif strategy == "reliefweb_api":
                        jobs = fetch_reliefweb_board(board_cfg)
                    elif strategy == "impactpool_html":
                        jobs = fetch_impactpool_board(board_cfg)
                    elif strategy == "datadotorg_wp":
                        jobs = fetch_datadotorg_board(board_cfg)
                    elif strategy == "arbeitnow_api":
                        jobs = fetch_arbeitnow_board(board_cfg)
                    elif strategy == "remotive_api":
                        jobs = fetch_remotive_board(board_cfg)
                    elif strategy == "wwr_rss":
                        jobs = fetch_wwr_board(board_cfg)
                    elif strategy == "hn_whoishiring":
                        jobs = fetch_hn_whoishiring_board(board_cfg)
                    elif strategy == "idealist_algolia":
                        jobs = fetch_idealist_board(board_cfg)
                    elif strategy == "fastforward_board":
                        jobs = fetch_fastforward_board(board_cfg)
                    elif strategy == "linkedin_guest":
                        jobs = fetch_linkedin_board(board_cfg)
                except Exception as exc:
                    print(f"  [{board_name}] Fetch error: {exc}")
                    board_fetch_status = f"error: {exc}"

                # Save raw fetch log
                _save_fetch_log(f"{strategy}_{board_id}", jobs, board_fetch_status)

                new_count = save_board_vacancies(board_cfg, jobs)
                update_source_tracking(
                    board_name,
                    board_cfg.get("tier", "C"),
                    strategy,
                    new_count,
                    board_fetch_status,
                )
                total_new += new_count
                if new_count > 0:
                    print(f"  [{board_name}] {new_count} NEW vacancies added")

                mark_board_fetched(board_id)
                # Commit per board so an interrupted run keeps finished boards.
                get_conn().commit()

        get_conn().commit()

        # --- Auto-enrich new candidate companies from boards ---
        # Under the run_daily driver this is skipped (--no-auto-enrich): the
        # driver's company_scoring stage owns the canonical chain (find site →
        # collect evidence → WANT-score). Standalone fetches keep the inline
        # enrichment for convenience.
        if not args.no_auto_enrich:
            _auto_enrich_candidates()

        # Firecrawl change tracking summary
        change_statuses = get_firecrawl_change_statuses()
        if change_statuses:
            unchanged = sum(1 for s in change_statuses.values() if s == "same")
            if unchanged:
                print(
                    f"\n  Firecrawl: {unchanged}/{len(change_statuses)} pages unchanged (saved credits)"
                )

        run_status.finish(new=total_new)
        fetch_stats["total_new"] = total_new
        _write_fetch_stats(fetch_stats)
        print(f"\n{'=' * 60}")
        print(f"  FETCH COMPLETE: {total_new} new vacancies found")
        print(f"{'=' * 60}")
        print_reconciliation_report()

        if manual_companies or manual_boards:
            print("\n⚠️  Manual check needed for:")
            for name, url in manual_companies:
                print(f"  - {name}: {url}")
            for name, url in manual_boards:
                print(f"  - {name} (board): {url}")
            print("  Visit the sites and check for new vacancies.")

        # --- Auto-score pipeline (subprocess isolation) ---
        if total_new > 0 and args.auto_score:
            print(f"\n{'─' * 60}")
            print(f"  AUTO-SCORE: filtering {total_new} new vacancies")
            print(f"{'─' * 60}")
            subprocess.run([sys.executable, "scripts/filter_vacancies.py"], check=False)
            print("  Scoring requires Opus subagents — run: /score")
        elif args.auto_score and total_new == 0:
            print("\n  Auto-score skipped: no new vacancies found")

        # Auto-pass expired unseen vacancies. This MUTATES source data (flips
        # expired 'unseen' rows to 'passed'/'expiring'), so it lives INSIDE the
        # fetch guard: a --report-only run must never change the data, only
        # re-render the dashboard from it. Keeping it here also means nothing is
        # left staged for generate_dashboard's snapshot commit to pick up by
        # accident in report-only mode.
        pass_expired_vacancies()
        get_conn().commit()

    # Generate dashboard. This is report OUTPUT (public/data.js in simple mode,
    # the dashboard_snapshot row in full mode) derived from the data — never a
    # source-data write — so --report-only regenerates it too; that IS the flag.
    #
    # --no-dashboard suppresses it: the run_daily driver fetches WITHOUT
    # publishing, then runs the gated publish itself (--report-only) only after a
    # clean run — so a truncated fetch can't push a corrupt snapshot live.
    if args.no_dashboard:
        print("\nSkipping dashboard regeneration (--no-dashboard).")
    else:
        print("\nGenerating dashboard...")
        generate_dashboard()
        print(f"  Dashboard: {PUBLIC_DIR}")

    from database_supabase import validate_db

    validate_db()

    print("\nDone!")


if __name__ == "__main__":
    main()
