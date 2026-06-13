#!/usr/bin/env python3
"""
Company registry audit — DB-only health check.

Generates REPORT-company-audit.md with MECE groups:
  1. Active monitored  — has strategy + has vacancies
  2. Active zero       — has strategy but 0 vacancies
  3. Unsourced active  — active status but no strategy
  4. Data quality      — missing fields, stale fetches
  5. Alias health      — dangling aliases, phantom orgs

Usage:
    python3 scripts/audit_companies.py
    python3 scripts/audit_companies.py --fix --dry-run
    python3 scripts/audit_companies.py --fix
"""

import argparse
import random
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from company_registry import (
    COMPANIES,
    _ALL_KNOWN_NAMES,
    _STRATEGY_REQUIRES_SLUG,
    resolve_canonical_name,
)
from database_supabase import is_fetch_error
from db_conn import get_conn
from quality import clean_description

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------

def audit_all_companies() -> dict:
    """Run all audit checks against Supabase data.

    Returns dict with keys:
      monitored_active, monitored_zero, unsourced_active,
      data_quality, alias_health, stats
    """
    conn = get_conn()
    cur = conn.cursor()

    # --- Load all companies with vacancy counts ---
    cur.execute("""
        SELECT c.canonical_name, c.fetch_strategy, c.status, c.tier,
               c.careers_url, c.ats_slug, c.website, c.last_fetched,
               c.fetch_status, c.aliases, c.alignment_score,
               count(v.id) AS vacancy_count
        FROM company c
        LEFT JOIN vacancy v ON v.company_id = c.id
        GROUP BY c.id
        ORDER BY c.canonical_name
    """)
    rows = cur.fetchall()
    cur.close()

    monitored_active = []
    monitored_zero = []
    unsourced_active = []
    data_quality = []

    for (name, strategy, status, tier, careers_url, ats_slug,
         website, last_fetched, fetch_status, aliases,
         alignment_score, vacancy_count) in rows:

        if status == 'inactive':
            continue

        has_strategy = bool(strategy and strategy.strip())

        if has_strategy and vacancy_count > 0:
            # Group 1: Active monitored
            monitored_active.append({
                "name": name,
                "strategy": strategy,
                "vacancy_count": vacancy_count,
                "last_fetched": last_fetched.isoformat() if last_fetched else "",
                "fetch_status": fetch_status or "",
                "tier": tier or "-",
                "alignment_score": alignment_score,
                "issues": _detect_issues(name, strategy, ats_slug, careers_url,
                                         last_fetched, fetch_status, vacancy_count),
            })
        elif has_strategy and vacancy_count == 0:
            # Group 2: Active with strategy but 0 vacancies
            monitored_zero.append({
                "name": name,
                "strategy": strategy,
                "last_fetched": last_fetched.isoformat() if last_fetched else "",
                "fetch_status": fetch_status or "",
                "issues": _detect_issues(name, strategy, ats_slug, careers_url,
                                         last_fetched, fetch_status, vacancy_count),
            })
        elif status == 'active' and not has_strategy:
            # Group 3: Active but unsourced
            unsourced_active.append({
                "name": name,
                "website": website or "",
                "careers_url": careers_url or "",
                "alignment_score": alignment_score,
            })

        # Group 4: Data quality issues
        issues = _detect_data_issues(name, strategy, ats_slug, careers_url,
                                      website, aliases)
        if issues:
            for issue in issues:
                data_quality.append({"name": name, **issue})

    # Group 5: Alias health
    alias_health = _check_alias_health()

    # Stats
    stats = {
        "total_companies": len(rows),
        "monitored_active": len(monitored_active),
        "monitored_zero": len(monitored_zero),
        "unsourced_active": len(unsourced_active),
        "data_quality_issues": len(data_quality),
        "alias_issues": len(alias_health),
    }

    return {
        "monitored_active": monitored_active,
        "monitored_zero": monitored_zero,
        "unsourced_active": unsourced_active,
        "data_quality": data_quality,
        "alias_health": alias_health,
        "stats": stats,
    }


def _detect_issues(name, strategy, ats_slug, careers_url,
                    last_fetched, fetch_status, vacancy_count):
    """Detect per-company operational issues."""
    issues = []
    if vacancy_count == 0:
        issues.append("zero_vacancies")
    if not last_fetched:
        issues.append("never_fetched")
    elif last_fetched:
        days_ago = (datetime.now(last_fetched.tzinfo) - last_fetched).days
        if days_ago >= 60:
            issues.append("stale_60d")
    if is_fetch_error(fetch_status):
        issues.append("fetch_error")
    if strategy in _STRATEGY_REQUIRES_SLUG and not ats_slug:
        issues.append("missing_slug")
    return issues


def _detect_data_issues(name, strategy, ats_slug, careers_url, website, aliases):
    """Detect data completeness issues."""
    issues = []
    if strategy and strategy in _STRATEGY_REQUIRES_SLUG and not ats_slug:
        issues.append({"type": "missing_slug", "detail": f"{strategy} needs ats_slug"})
    if not website and not careers_url:
        issues.append({"type": "no_url", "detail": "no website or careers_url"})
    if not aliases or len(aliases) <= 1:
        issues.append({"type": "few_aliases", "detail": f"only {len(aliases or [])} alias(es)"})
    return issues


def _check_alias_health():
    """Check for alias-related issues in the database."""
    conn = get_conn()
    cur = conn.cursor()

    issues = []

    # Check for duplicate aliases across companies
    cur.execute("""
        SELECT unnest(aliases) AS alias, array_agg(canonical_name) AS companies
        FROM company
        WHERE aliases IS NOT NULL
        GROUP BY alias
        HAVING count(*) > 1
    """)
    for alias, companies in cur.fetchall():
        issues.append({
            "type": "duplicate_alias",
            "detail": f"alias '{alias}' claimed by: {', '.join(companies)}",
        })

    cur.close()
    return issues


# ---------------------------------------------------------------------------
# Vacancy description quality audit
# ---------------------------------------------------------------------------

def audit_description_quality(sample_size: int = 30) -> dict:
    """Audit quality of vacancy.full_description via clean_description().

    Returns:
      counters  — {verdict: count} for the whole DB
      sample    — list of dicts for the random sample
      worst_10  — 10 lowest-quality vacancies (non-ok verdicts, sorted by verdict)
    """
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT v.id, c.canonical_name, v.title, v.full_description
        FROM vacancy v
        JOIN company c ON c.id = v.company_id
        WHERE v.full_description IS NOT NULL
        ORDER BY v.id
    """)
    all_rows = cur.fetchall()
    cur.close()

    counters: dict[str, int] = {
        "ok": 0,
        "cookie_wall": 0,
        "error_page": 0,
        "nav_junk": 0,
        "too_short": 0,
        "empty": 0,
    }

    non_ok: list[dict] = []

    for vac_id, org, title, description in all_rows:
        _, verdict = clean_description(description or "")
        counters[verdict] = counters.get(verdict, 0) + 1
        if verdict != "ok":
            non_ok.append({
                "id": str(vac_id),
                "org": org or "?",
                "title": title or "?",
                "verdict": verdict,
                "preview": (description or "")[:80],
            })

    # Random sample (capped at available rows)
    sample_rows = random.sample(all_rows, min(sample_size, len(all_rows)))
    sample_results = []
    for vac_id, org, title, description in sample_rows:
        _, verdict = clean_description(description or "")
        sample_results.append({
            "org": org or "?",
            "title": title or "?",
            "verdict": verdict,
            "preview": (description or "")[:80],
        })

    # Worst 10: non-ok verdicts, ordered by verdict type then org
    worst_10 = sorted(non_ok, key=lambda x: (x["verdict"], x["org"]))[:10]

    total = len(all_rows)
    counters["_total"] = total

    print(f"  Description quality: {total} vacancies scanned", flush=True)
    for verdict, count in sorted(counters.items()):
        if not verdict.startswith("_"):
            pct = round(100 * count / total, 1) if total else 0
            print(f"    {verdict}: {count} ({pct}%)", flush=True)

    return {
        "counters": counters,
        "sample": sample_results,
        "worst_10": worst_10,
    }


# ---------------------------------------------------------------------------
# Cleanup: archive unseen vacancies from inactive companies
# ---------------------------------------------------------------------------

def cleanup_unseen_inactive(dry_run: bool = False) -> int:
    """Archive unseen vacancies whose company is inactive.

    Returns number of vacancies archived.
    Only touches status='unseen' — does NOT touch liked/to_apply/applied/etc.
    """
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT v.id, c.canonical_name, v.title
        FROM vacancy v
        JOIN company c ON c.id = v.company_id
        WHERE v.status = 'unseen' AND c.status = 'inactive'
        ORDER BY c.canonical_name, v.title
    """)
    rows = cur.fetchall()
    count = len(rows)

    print(f"\n{'='*50}", flush=True)
    print(f"  Cleanup: unseen vacancies from inactive companies — {count} total", flush=True)
    print(f"{'='*50}", flush=True)

    for vac_id, org, title in rows:
        print(f"    [{org}] {title}", flush=True)

    if count == 0:
        print("  Nothing to clean up.", flush=True)
        cur.close()
        return 0

    if dry_run:
        print(f"\n  [DRY RUN] Would archive {count} vacancies.", flush=True)
        cur.close()
        return count

    cur.execute("""
        UPDATE vacancy
        SET status = 'archived'
        WHERE id IN (
            SELECT v.id
            FROM vacancy v
            JOIN company c ON c.id = v.company_id
            WHERE v.status = 'unseen' AND c.status = 'inactive'
        )
    """)
    conn.commit()

    # Verify
    cur.execute("""
        SELECT count(*)
        FROM vacancy v
        JOIN company c ON c.id = v.company_id
        WHERE v.status = 'unseen' AND c.status = 'inactive'
    """)
    remaining = cur.fetchone()[0]
    cur.close()

    print(f"\n  Archived {count} vacancies. Remaining unseen+inactive: {remaining}", flush=True)
    return count


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(audit: dict, desc_quality: dict | None = None) -> str:
    """Generate markdown report from audit results."""
    stats = audit["stats"]
    lines = [
        "# Company Registry Audit Report",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Source: Supabase (sole source of truth)",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Total companies | {stats['total_companies']} |",
        f"| Monitored active | {stats['monitored_active']} |",
        f"| Monitored zero | {stats['monitored_zero']} |",
        f"| Unsourced active | {stats['unsourced_active']} |",
        f"| Data quality issues | {stats['data_quality_issues']} |",
        f"| Alias issues | {stats['alias_issues']} |",
        "",
    ]

    # Group 1: Monitored active
    active = audit["monitored_active"]
    lines.append(f"## 1. Monitored Active ({len(active)} companies)")
    lines.append("")
    if active:
        lines.append("| Company | Strategy | Vacancies | Last Fetched | Tier | Alignment | Issues |")
        lines.append("|---------|----------|-----------|--------------|------|-----------|--------|")
        for c in active:
            last = c["last_fetched"][:10] if c["last_fetched"] else "never"
            align = str(c["alignment_score"]) if c["alignment_score"] is not None else "-"
            issues = ", ".join(c["issues"]) if c["issues"] else "-"
            lines.append(f"| {c['name']} | {c['strategy']} | {c['vacancy_count']} | {last} | {c['tier']} | {align} | {issues} |")
    lines.append("")

    # Group 2: Monitored zero
    zero = audit["monitored_zero"]
    lines.append(f"## 2. Monitored Zero Vacancies ({len(zero)} companies)")
    lines.append("")
    if zero:
        lines.append("| Company | Strategy | Last Fetched | Status | Issues |")
        lines.append("|---------|----------|--------------|--------|--------|")
        for c in zero:
            last = c["last_fetched"][:10] if c["last_fetched"] else "never"
            status = c["fetch_status"] or "-"
            issues = ", ".join(c["issues"]) if c["issues"] else "-"
            lines.append(f"| {c['name']} | {c['strategy']} | {last} | {status} | {issues} |")
    else:
        lines.append("No monitored companies with zero vacancies.")
    lines.append("")

    # Group 3: Unsourced active
    unsourced = audit["unsourced_active"]
    lines.append(f"## 3. Unsourced Active ({len(unsourced)} companies)")
    lines.append("")
    if unsourced:
        lines.append("| Company | Website | Careers URL | Alignment |")
        lines.append("|---------|---------|-------------|-----------|")
        for c in unsourced:
            align = str(c["alignment_score"]) if c["alignment_score"] is not None else "-"
            website = c["website"][:40] if c["website"] else "-"
            lines.append(f"| {c['name']} | {website} | {c['careers_url'][:40] if c['careers_url'] else '-'} | {align} |")
    lines.append("")

    # Group 4: Data quality
    dq = audit["data_quality"]
    lines.append(f"## 4. Data Quality Issues ({len(dq)} issues)")
    lines.append("")
    if dq:
        for issue in dq:
            lines.append(f"- **{issue['name']}**: {issue['type']} — {issue['detail']}")
    else:
        lines.append("No data quality issues found.")
    lines.append("")

    # Group 5: Alias health
    alias = audit["alias_health"]
    lines.append(f"## 5. Alias Health ({len(alias)} issues)")
    lines.append("")
    if alias:
        for issue in alias:
            lines.append(f"- **{issue['type']}**: {issue['detail']}")
    else:
        lines.append("No alias issues found.")
    lines.append("")

    # Group 6: Description quality
    if desc_quality:
        counters = desc_quality["counters"]
        total = counters.get("_total", 0)
        lines.append(f"## 6. Description Quality ({total} vacancies scanned)")
        lines.append("")
        lines.append("### Counters (full DB)")
        lines.append("")
        lines.append("| Verdict | Count | % |")
        lines.append("|---------|-------|---|")
        for verdict in ("ok", "cookie_wall", "error_page", "nav_junk", "too_short", "empty"):
            count = counters.get(verdict, 0)
            pct = round(100 * count / total, 1) if total else 0
            lines.append(f"| {verdict} | {count} | {pct}% |")
        lines.append("")

        lines.append("### Random sample (30 vacancies)")
        lines.append("")
        lines.append("| Org | Title | Verdict | Preview |")
        lines.append("|-----|-------|---------|---------|")
        for row in desc_quality["sample"]:
            preview = row["preview"].replace("|", "\\|").replace("\n", " ")[:60]
            lines.append(f"| {row['org']} | {row['title'][:40]} | {row['verdict']} | {preview} |")
        lines.append("")

        worst = desc_quality["worst_10"]
        lines.append(f"### 10 worst descriptions (non-ok)")
        lines.append("")
        if worst:
            lines.append("| Org | Title | Verdict | Preview |")
            lines.append("|-----|-------|---------|---------|")
            for row in worst:
                preview = row["preview"].replace("|", "\\|").replace("\n", " ")[:60]
                lines.append(f"| {row['org']} | {row['title'][:40]} | {row['verdict']} | {preview} |")
        else:
            lines.append("All descriptions passed quality check.")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fix mode — auto-remediate detected issues
# ---------------------------------------------------------------------------

def _fix_issues(audit_result: dict, dry_run: bool = False) -> None:
    """Auto-remediate issues found by audit.

    Actions:
      1. Run discover_ats --unsourced --all-tiers --html-scan --apply on unsourced companies
      2. Trigger fetch for never-fetched companies with valid strategy
    """
    unsourced = audit_result["unsourced_active"]
    monitored_zero = audit_result["monitored_zero"]
    monitored_active = audit_result["monitored_active"]

    # Collect never-fetched companies from groups 1 & 2
    never_fetched = []
    for c in monitored_zero + monitored_active:
        if "never_fetched" in c.get("issues", []):
            never_fetched.append(c)

    actions_taken = []

    # --- Action 1: ATS discovery for unsourced companies ---
    if unsourced:
        print(f"\n{'='*50}")
        print(f"  FIX: ATS Discovery for {len(unsourced)} unsourced companies")
        print(f"{'='*50}")
        if dry_run:
            print("  [DRY RUN] Would run: discover_ats.py --unsourced --all-tiers --html-scan --apply")
            for c in unsourced:
                print(f"    - {c['name']} ({c['website'] or c['careers_url'] or 'no URL'})")
            actions_taken.append(f"discover_ats --apply on {len(unsourced)} unsourced companies")
        else:
            cmd = [
                sys.executable, str(PROJECT_ROOT / "scripts" / "discover_ats.py"),
                "--unsourced", "--all-tiers", "--html-scan", "--apply",
            ]
            print(f"  Running: {' '.join(cmd[-5:])}")
            try:
                subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False, timeout=600)
                actions_taken.append(f"discover_ats --apply on {len(unsourced)} unsourced")
            except subprocess.TimeoutExpired:
                print("  ⚠ ATS discovery timed out (10 min limit)")
            except Exception as e:
                print(f"  ⚠ ATS discovery failed: {e}")
    else:
        print("\n  No unsourced companies — skipping ATS discovery")

    # --- Action 2: Fetch for never-fetched companies ---
    if never_fetched:
        # Group by strategy for efficient batch fetching
        by_strategy: dict[str, list[str]] = {}
        for c in never_fetched:
            strat = c.get("strategy", "unknown")
            by_strategy.setdefault(strat, []).append(c["name"])

        print(f"\n{'='*50}")
        print(f"  FIX: Fetch {len(never_fetched)} never-fetched companies")
        print(f"{'='*50}")

        for strat, names in sorted(by_strategy.items()):
            print(f"\n  Strategy: {strat} ({len(names)} companies)")
            for n in names:
                print(f"    - {n}")

            if strat == "manual_check":
                print("    → Skipping manual_check (requires human intervention)")
                continue

            if dry_run:
                print(f"    [DRY RUN] Would run: fetch_vacancies.py --companies \"{','.join(names)}\" --no-boards")
                actions_taken.append(f"fetch {len(names)} {strat} companies")
            else:
                companies_arg = ",".join(names)
                cmd = [
                    sys.executable, str(PROJECT_ROOT / "scripts" / "fetch_vacancies.py"),
                    "--companies", companies_arg, "--no-boards",
                ]
                print(f"    Running fetch for {len(names)} companies...")
                try:
                    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False, timeout=600)
                    actions_taken.append(f"fetched {len(names)} {strat} companies")
                except subprocess.TimeoutExpired:
                    print(f"    ⚠ Fetch timed out for {strat}")
                except Exception as e:
                    print(f"    ⚠ Fetch failed: {e}")
    else:
        print("\n  No never-fetched companies — skipping fetch")

    # --- Summary ---
    print(f"\n{'='*50}")
    print(f"  FIX SUMMARY {'(DRY RUN)' if dry_run else ''}")
    print(f"{'='*50}")
    if actions_taken:
        for action in actions_taken:
            print(f"  ✓ {action}")
    else:
        print("  No actions needed — pipeline is healthy")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Company registry audit")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-remediate detected issues")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what --fix would do without executing")
    parser.add_argument("--no-desc-quality", action="store_true",
                        help="Skip description quality audit (faster)")
    parser.add_argument("--cleanup-unseen", action="store_true",
                        help="Archive unseen vacancies from inactive companies")
    args = parser.parse_args()

    print("Running company registry audit (Supabase-only)...", flush=True)
    audit_result = audit_all_companies()

    desc_quality = None
    if not args.no_desc_quality:
        print("\nRunning description quality audit...", flush=True)
        desc_quality = audit_description_quality(sample_size=30)

    report = generate_report(audit_result, desc_quality=desc_quality)
    report_path = PROJECT_ROOT / "REPORT-company-audit.md"
    report_path.write_text(report, encoding="utf-8")

    stats = audit_result["stats"]
    print(f"\nAudit complete:", flush=True)
    print(f"  Monitored active:    {stats['monitored_active']}", flush=True)
    print(f"  Monitored zero:      {stats['monitored_zero']}", flush=True)
    print(f"  Unsourced active:    {stats['unsourced_active']}", flush=True)
    print(f"  Data quality issues: {stats['data_quality_issues']}", flush=True)
    print(f"  Alias issues:        {stats['alias_issues']}", flush=True)
    print(f"\nReport saved: {report_path}", flush=True)

    if args.fix:
        _fix_issues(audit_result, dry_run=args.dry_run)

    if args.cleanup_unseen:
        cleanup_unseen_inactive(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
