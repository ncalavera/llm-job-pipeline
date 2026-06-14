#!/usr/bin/env python3
"""ATS discovery: detect hidden ATS endpoints on career pages.

Two modes:
  --map       (default) Use Firecrawl map() to find ATS URLs in sitemap/links
  --html-scan          Scrape career page HTML to detect embedded ATS (iframes, scripts, links)

The HTML scan mode is more thorough — it catches JS-embedded widgets that map() misses.

Usage:
    python3 scripts/discover_ats.py --html-scan --dry-run    # Show targets (active companies)
    python3 scripts/discover_ats.py --html-scan               # Full HTML scan
    python3 scripts/discover_ats.py --html-scan --company "Nesta"  # Single company
    python3 scripts/discover_ats.py --all-tiers               # Map mode, all tiers
    python3 scripts/discover_ats.py --unsourced --all-tiers --dry-run  # Active without strategy
    python3 scripts/discover_ats.py --unsourced --all-tiers --html-scan  # Scan unsourced companies
    python3 scripts/discover_ats.py --html-scan --apply --dry-run  # Preview DB changes
    python3 scripts/discover_ats.py --html-scan --apply       # Scan + apply to DB
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser. Defined before the heavy project imports so
    ``--help`` / ``-h`` prints usage without connecting to the database or
    loading the user profile (those happen only when a real command runs)."""
    parser = argparse.ArgumentParser(description="Discover ATS types on career pages")
    parser.add_argument("--html-scan", action="store_true",
                        help="Scrape HTML to detect embedded ATS (more thorough than map)")
    parser.add_argument("--all-tiers", action="store_true",
                        help="Include all tiers (default: S+A only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show targets without API calls")
    parser.add_argument("--company", type=str,
                        help="Single company to check")
    parser.add_argument("--unsourced", action="store_true",
                        help="Scan active companies without fetch strategy (need ATS detection)")
    parser.add_argument("--apply", action="store_true",
                        help="Apply discovered ATS strategies to DB (default: report only)")
    return parser


# Print help and exit BEFORE importing anything that touches the DB or profile.
from cli_help import wants_help
if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

from config import COMPANIES, get_firecrawl_client, resolve_canonical_name

# ---------------------------------------------------------------------------
# Output directory for persistent results
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DISCOVERY_DIR = PROJECT_ROOT / ".firecrawl" / "ats-discovery"
DISCOVERY_HTML_DIR = DISCOVERY_DIR / "html"
DISCOVERY_RESULTS_PATH = DISCOVERY_DIR / "results.json"
APPLY_LOG_PATH = DISCOVERY_DIR / "apply-log.json"
MAPS_DIR = PROJECT_ROOT / ".firecrawl" / "maps"

# Careers-related URL path keywords for filtering map results
CAREERS_KEYWORDS = {
    "careers", "jobs", "openings", "positions", "vacancies",
    "work-with-us", "apply", "hiring", "opportunities",
}

# ATS strategies that are considered "working" and should never be downgraded
WORKING_ATS_STRATEGIES = {
    "greenhouse", "lever", "ashby", "workable", "recruitee",
    "teamtailor_rss", "workday_api", "bamboohr", "smartrecruiters",
    "occupop", "personio", "applied", "pinpoint", "breezy", "jazzhr",
    "reliefweb_api", "rss",
}

# ---------------------------------------------------------------------------
# ATS URL patterns → (regex, strategy_name)
# Used in both map() links and HTML source scanning
# ---------------------------------------------------------------------------

ATS_PATTERNS = [
    # Greenhouse: boards.greenhouse.io/{slug} or boards-api.greenhouse.io/v1/boards/{slug}
    (r"greenhouse\.io/(?:v1/boards/)?([a-z0-9_-]+)", "greenhouse"),
    # Lever: jobs.lever.co/{slug}
    (r"jobs\.lever\.co/([a-z0-9_-]+)", "lever"),
    # Ashby: jobs.ashbyhq.com/{slug}
    (r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", "ashby"),
    # Workable: apply.workable.com/{slug} or {slug}.workable.com
    (r"apply\.workable\.com/([a-z0-9_-]+)", "workable"),
    (r"([a-z0-9_-]+)\.workable\.com", "workable"),
    # Recruitee: {slug}.recruitee.com
    (r"([a-z0-9_-]+)\.recruitee\.com", "recruitee"),
    # Teamtailor: {slug}.teamtailor.com or career.{slug}.com (varies)
    (r"([a-z0-9_-]+)\.teamtailor\.com", "teamtailor_rss"),
    # Workday: {tenant}.wd{N}.myworkdayjobs.com
    (r"([a-z0-9_-]+)\.wd\d+\.myworkdayjobs\.com", "workday_api"),
    # BambooHR: {slug}.bamboohr.com/careers
    (r"([a-z0-9_-]+)\.bamboohr\.com", "bamboohr"),
    # SmartRecruiters: jobs.smartrecruiters.com/{slug}
    (r"jobs\.smartrecruiters\.com/([a-zA-Z0-9_-]+)", "smartrecruiters"),
    # Occupop: {slug}.occupop-careers.com
    (r"([a-z0-9_-]+)\.occupop-careers\.com", "occupop"),
    # Personio: {slug}.jobs.personio.de or .personio.com
    (r"([a-z0-9_-]+)\.jobs\.personio\.(?:de|com)", "personio"),
    # Applied: app.beapplied.com/{slug}
    (r"app\.beapplied\.com/([a-z0-9_-]+)", "applied"),
    # Pinpoint: {slug}.pinpointhq.com
    (r"([a-z0-9_-]+)\.pinpointhq\.com", "pinpoint"),
    # Breezy: {slug}.breezy.hr
    (r"([a-z0-9_-]+)\.breezy\.hr", "breezy"),
    # JazzHR: {slug}.applytojob.com
    (r"([a-z0-9_-]+)\.applytojob\.com", "jazzhr"),
]

# Additional patterns only meaningful in HTML source (not in plain URLs)
HTML_ONLY_PATTERNS = [
    # Greenhouse embed div
    (r'id=["\']grnhse_app["\']', "greenhouse", None),
    # Greenhouse embed script
    (r'src=["\'][^"\']*boards\.greenhouse\.io/embed/job_board[?/].*?([a-z0-9_-]+)', "greenhouse", 1),
    # Lever embed
    (r'id=["\']lever-jobs-container["\']', "lever", None),
    # Workable embed widget
    (r'whr\(document\)', "workable", None),
    (r'src=["\'][^"\']*workable\.com/.*?([a-z0-9_-]+)', "workable", 1),
    # BambooHR embed
    (r'src=["\'][^"\']*bamboohr\.com/(?:js/)?jobs', "bamboohr", None),
    # Personio embed
    (r'src=["\'][^"\']*personio\.de/recruiting/embed', "personio", None),
]


def detect_ats_in_urls(urls: list[str]) -> list[dict]:
    """Check a list of URLs against known ATS patterns."""
    results = []
    seen = set()
    for url in urls:
        url_lower = url.lower()
        for pattern, ats_name in ATS_PATTERNS:
            m = re.search(pattern, url_lower)
            if m:
                slug = m.group(1)
                key = (ats_name, slug)
                if key not in seen:
                    seen.add(key)
                    results.append({
                        "ats": ats_name,
                        "slug": slug,
                        "url": url,
                    })
    return results


def detect_ats_in_html(html: str) -> list[dict]:
    """Scan raw HTML for embedded ATS patterns (iframes, scripts, links, divs)."""
    results = []
    seen = set()

    # First check all URLs found in href/src attributes
    url_pattern = r'(?:href|src|action)=["\']([^"\']+)["\']'
    urls_in_html = re.findall(url_pattern, html, re.IGNORECASE)
    for hit in detect_ats_in_urls(urls_in_html):
        key = (hit["ats"], hit["slug"])
        if key not in seen:
            seen.add(key)
            results.append({**hit, "source": "html_url"})

    # Then check HTML-only patterns (embedded widgets)
    for pattern, ats_name, slug_group in HTML_ONLY_PATTERNS:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            slug = m.group(slug_group) if slug_group and m.lastindex and m.lastindex >= slug_group else ""
            key = (ats_name, slug or "_embed_")
            if key not in seen:
                seen.add(key)
                results.append({
                    "ats": ats_name,
                    "slug": slug,
                    "url": "",
                    "source": "html_embed",
                })

    return results


def _load_enrichment_tiers() -> dict[str, str]:
    """Load calculated tiers from enriched.json (same formula as fetch_vacancies)."""
    enriched_path = PROJECT_ROOT / "companies" / "enriched.json"
    if not enriched_path.exists():
        return {}
    with open(enriched_path, encoding="utf-8") as f:
        raw = json.load(f)
    enriched = raw.get("companies", raw) if isinstance(raw, dict) else {}

    tiers: dict[str, str] = {}
    for name, data in enriched.items():
        if not isinstance(data, dict):
            continue
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


def _load_unsourced() -> list[tuple]:
    """Load active companies from Supabase without fetch_strategy (need ATS detection)."""
    from db_conn import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT canonical_name, careers_url, website
        FROM company
        WHERE status = 'active'
          AND (fetch_strategy IS NULL OR fetch_strategy = '')
    """)
    results = []
    for name, careers, website in cur.fetchall():
        url = (careers or "").strip() or (website or "").strip()
        if name and url:
            results.append((name, url))
    cur.close()
    return results


def _build_targets(args) -> list[tuple]:
    """Build list of (name, tier, strategy, url) for companies to scan."""
    enriched_tiers = _load_enrichment_tiers()
    priority_tiers = {"S", "A", "B", "C"} if args.all_tiers else {"S", "A"}

    targets = []

    if getattr(args, "unsourced", False):
        # Unsourced mode: scan active companies without fetch strategy
        csv_unsourced = _load_unsourced()
        for name, url in csv_unsourced:
            canonical = resolve_canonical_name(name)
            tier = enriched_tiers.get(canonical)
            if not args.all_tiers and tier not in priority_tiers:
                continue
            if args.company and canonical.lower() != args.company.lower():
                continue
            targets.append((canonical, tier or "?", "unsourced", url))
    else:
        # Active mode: scan configured companies with firecrawl/manual strategy
        for name, cfg in COMPANIES.items():
            strategy = cfg.get("strategy", "")
            if strategy not in ("firecrawl_scrape", "manual_check"):
                continue

            # Use enriched tier (dynamic), fallback to CSV tier
            tier = enriched_tiers.get(name) or cfg.get("tier")
            if not args.all_tiers and tier not in priority_tiers:
                continue
            if args.company and name.lower() != args.company.lower():
                continue

            url = cfg.get("url", "") or cfg.get("careers_url", "")
            if not url:
                print(f"  ⚠ {name} — no URL, skipping")
                continue

            targets.append((name, tier or "?", strategy, url))

    # Sort: S first, then A, then by name
    tier_order = {"S": 0, "A": 1, "B": 2, "C": 3, "?": 9}
    targets.sort(key=lambda x: (tier_order.get(x[1], 9), x[0]))
    return targets


def _load_existing_results() -> dict:
    """Load existing results.json if present."""
    if DISCOVERY_RESULTS_PATH.exists():
        with open(DISCOVERY_RESULTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_results(results: dict) -> None:
    """Save results to .firecrawl/ats-discovery/results.json."""
    DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)
    with open(DISCOVERY_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved: {DISCOVERY_RESULTS_PATH}")


def _save_html(company_name: str, html: str) -> None:
    """Cache career page HTML for reference."""
    DISCOVERY_HTML_DIR.mkdir(parents=True, exist_ok=True)
    slug = company_name.lower().replace(" ", "-").replace(".", "")
    path = DISCOVERY_HTML_DIR / f"{slug}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def _save_map_cache(company_name: str, website: str, all_urls: list[str]) -> None:
    """Cache map() results to .firecrawl/maps/{slug}.json."""
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    slug = company_name.lower().replace(" ", "_")
    path = MAPS_DIR / f"{slug}.json"

    careers_urls = [
        u for u in all_urls
        if any(kw in u.lower() for kw in CAREERS_KEYWORDS)
    ]

    cache = {
        "company": company_name,
        "website": website,
        "urls": all_urls,
        "careers_urls": careers_urls,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _apply_results(scan_results: list[dict], dry_run: bool = False) -> None:
    """Apply discovered ATS strategies to DB. Only upgrades, never downgrades."""
    from db_conn import get_conn

    upgradeable = [r for r in scan_results if r.get("verdict") == "upgrade"]
    if not upgradeable:
        print("\nNo upgradeable results to apply.")
        return

    conn = get_conn()
    cur = conn.cursor()
    changes = []

    print(f"\n{'='*60}")
    print(f"  {'DRY RUN — ' if dry_run else ''}APPLYING {len(upgradeable)} UPGRADES")
    print(f"{'='*60}\n")

    for result in upgradeable:
        company = result["company"]
        new_strategy = result["ats"]
        new_slug = result.get("slug", "")
        discovered_url = result.get("url", "")

        # Look up current state
        cur.execute(
            "SELECT fetch_strategy, ats_slug, careers_url, website FROM company WHERE canonical_name = %s",
            (company,),
        )
        row = cur.fetchone()
        if not row:
            print(f"  SKIP {company} — not found in DB")
            continue

        current_strategy, current_slug, current_careers, current_website = row

        # --- Safety guardrail A: never downgrade working ATS strategies ---
        if current_strategy and current_strategy in WORKING_ATS_STRATEGIES:
            print(f"  SKIP {company} — already has working strategy '{current_strategy}'")
            continue

        # --- Safety guardrail B: manual_check → firecrawl_scrape rules ---
        if current_strategy == "manual_check":
            # manual_check → firecrawl_scrape only if no ATS detected + valid careers URL
            # But if we found an ATS, we upgrade to that ATS (not firecrawl_scrape)
            # This guardrail applies only when trying to go manual_check → firecrawl_scrape
            if new_strategy == "firecrawl_scrape":
                if not discovered_url:
                    print(f"  SKIP {company} — manual_check → firecrawl_scrape requires valid URL")
                    continue

        # --- Safety guardrail C: careers_url same-domain guard ---
        new_careers_url = None
        if discovered_url:
            discovered_domain = urlparse(discovered_url).netloc.lower().replace("www.", "")
            if current_website:
                website_domain = urlparse(current_website).netloc.lower().replace("www.", "")
                if discovered_domain == website_domain or discovered_domain.endswith("." + website_domain):
                    new_careers_url = discovered_url
                else:
                    # Different domain — still set careers_url for ATS URLs (e.g., greenhouse.io)
                    # These are legitimate external ATS URLs
                    new_careers_url = discovered_url
            else:
                new_careers_url = discovered_url

        before = {"fetch_strategy": current_strategy, "ats_slug": current_slug}
        after = {"fetch_strategy": new_strategy, "ats_slug": new_slug}
        if new_careers_url:
            after["careers_url"] = new_careers_url

        change = {
            "company": company,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "before": before,
            "after": after,
        }

        prefix = "  [DRY] " if dry_run else "  "
        print(f"{prefix}{company}:")
        print(f"{prefix}  strategy: {current_strategy or 'NULL'} → {new_strategy}")
        print(f"{prefix}  ats_slug: {current_slug or 'NULL'} → {new_slug}")
        if new_careers_url:
            print(f"{prefix}  careers_url → {new_careers_url}")

        if not dry_run:
            if new_careers_url:
                cur.execute(
                    "UPDATE company SET fetch_strategy = %s, ats_slug = %s, careers_url = %s WHERE canonical_name = %s",
                    (new_strategy, new_slug, new_careers_url, company),
                )
            else:
                cur.execute(
                    "UPDATE company SET fetch_strategy = %s, ats_slug = %s WHERE canonical_name = %s",
                    (new_strategy, new_slug, company),
                )
            changes.append(change)

    if not dry_run and changes:
        conn.commit()
        print(f"\n  Committed {len(changes)} DB updates.")

        # Append to apply log
        DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)
        existing_log = []
        if APPLY_LOG_PATH.exists():
            try:
                with open(APPLY_LOG_PATH, encoding="utf-8") as f:
                    existing_log = json.load(f)
            except (json.JSONDecodeError, ValueError):
                existing_log = []
        existing_log.extend(changes)
        with open(APPLY_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing_log, f, indent=2, ensure_ascii=False)
        print(f"  Change log: {APPLY_LOG_PATH}")
    elif dry_run:
        print(f"\n  Dry run complete — no changes applied.")

    cur.close()


def run_html_scan(targets: list[tuple], dry_run: bool = False, apply: bool = False, apply_dry_run: bool = False) -> None:
    """Scrape career page HTML and detect embedded ATS widgets."""
    tier_label = ", ".join(sorted(set(t[1] for t in targets)))
    print(f"\n{'='*60}")
    print(f"  ATS HTML SCAN — {len(targets)} companies (tiers: {tier_label})")
    print(f"{'='*60}\n")

    if dry_run:
        for name, tier, strategy, url in targets:
            print(f"  {tier} | {strategy:18} | {name:30} | {url}")
        print(f"\nEstimated cost: ~{len(targets)} Firecrawl credits (1 per scrape)")
        return

    client = get_firecrawl_client()
    if not client:
        print("ERROR: Firecrawl client not available. Set FIRECRAWL_API_KEY.")
        sys.exit(1)

    existing = _load_existing_results()
    scan_results = []

    for i, (name, tier, strategy, url) in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {name} (tier {tier}, {strategy})")
        print(f"  URL: {url}")

        # Step 1: Check URL directly for ATS pattern
        direct_hits = detect_ats_in_urls([url])
        if direct_hits:
            hit = direct_hits[0]
            print(f"  ✓ Direct URL match: {hit['ats']} (slug: {hit['slug']})")
            result = {
                "company": name,
                "tier": tier,
                "ats": hit["ats"],
                "slug": hit["slug"],
                "url": hit["url"],
                "scan_mode": "direct",
                "scan_date": datetime.now().isoformat(timespec="seconds"),
                "verdict": "upgrade",
            }
            scan_results.append(result)
            existing[name] = result
            continue

        # Step 2: Scrape HTML via Firecrawl
        try:
            scrape_result = client.scrape(url, formats=["html", "markdown"])
            html = ""
            markdown = ""
            if hasattr(scrape_result, "html"):
                html = scrape_result.html or ""
            elif isinstance(scrape_result, dict):
                html = scrape_result.get("html", "")
            if hasattr(scrape_result, "markdown"):
                markdown = scrape_result.markdown or ""
            elif isinstance(scrape_result, dict):
                markdown = scrape_result.get("markdown", "")

            if html:
                _save_html(name, html)
                print(f"  Scraped {len(html):,} chars HTML")

                # Detect ATS in HTML
                hits = detect_ats_in_html(html)
                if hits:
                    hit = hits[0]  # Take the best match
                    print(f"  ✓ Found: {hit['ats']} (slug: {hit['slug']}) via {hit['source']}")
                    result = {
                        "company": name,
                        "tier": tier,
                        "ats": hit["ats"],
                        "slug": hit["slug"],
                        "url": hit.get("url", url),
                        "scan_mode": "html_scan",
                        "scan_date": datetime.now().isoformat(timespec="seconds"),
                        "verdict": "upgrade",
                        "all_hits": hits if len(hits) > 1 else None,
                    }
                    scan_results.append(result)
                    existing[name] = result
                else:
                    # Also scan markdown for any links we might have missed
                    md_urls = re.findall(r'https?://[^\s)\]>"\']+', markdown)
                    md_hits = detect_ats_in_urls(md_urls)
                    if md_hits:
                        hit = md_hits[0]
                        print(f"  ✓ Found in markdown: {hit['ats']} (slug: {hit['slug']})")
                        result = {
                            "company": name,
                            "tier": tier,
                            "ats": hit["ats"],
                            "slug": hit["slug"],
                            "url": hit["url"],
                            "scan_mode": "markdown",
                            "scan_date": datetime.now().isoformat(timespec="seconds"),
                            "verdict": "upgrade",
                        }
                        scan_results.append(result)
                        existing[name] = result
                    else:
                        print(f"  ✗ No ATS detected — keep {strategy}")
                        result = {
                            "company": name,
                            "tier": tier,
                            "ats": "none",
                            "slug": "",
                            "url": url,
                            "scan_mode": "html_scan",
                            "scan_date": datetime.now().isoformat(timespec="seconds"),
                            "verdict": "keep",
                        }
                        scan_results.append(result)
                        existing[name] = result
            else:
                print(f"  ⚠ No HTML returned")
                result = {
                    "company": name,
                    "tier": tier,
                    "ats": "none",
                    "slug": "",
                    "url": url,
                    "scan_mode": "html_scan",
                    "scan_date": datetime.now().isoformat(timespec="seconds"),
                    "verdict": "check_manually",
                }
                scan_results.append(result)
                existing[name] = result

        except Exception as e:
            print(f"  ⚠ Error: {str(e)[:120]}")
            result = {
                "company": name,
                "tier": tier,
                "ats": "error",
                "slug": "",
                "url": url,
                "scan_mode": "html_scan",
                "scan_date": datetime.now().isoformat(timespec="seconds"),
                "verdict": "error",
                "error": str(e)[:200],
            }
            scan_results.append(result)
            existing[name] = result

        time.sleep(1)  # Rate limiting

    # Save all results
    _save_results(existing)

    # Print summary
    _print_summary(scan_results)

    # Apply if requested
    if apply:
        _apply_results(scan_results, dry_run=apply_dry_run)


def run_map_scan(targets: list[tuple], dry_run: bool = False, apply: bool = False, apply_dry_run: bool = False) -> None:
    """Original map()-based scan (legacy mode)."""
    tier_label = ", ".join(sorted(set(t[1] for t in targets)))
    print(f"\n{'='*60}")
    print(f"  ATS DISCOVERY (map) — {len(targets)} companies (tiers: {tier_label})")
    print(f"{'='*60}\n")

    if dry_run:
        for name, tier, strategy, url in targets:
            print(f"  {tier} | {name:30} | {url}")
        print(f"\nEstimated cost: ~{len(targets)} Firecrawl credits")
        return

    client = get_firecrawl_client()
    if not client:
        print("ERROR: Firecrawl client not available.")
        sys.exit(1)

    scan_results = []
    existing = _load_existing_results()

    for i, (name, tier, strategy, url) in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {name} (tier {tier})")
        print(f"  URL: {url}")

        direct_hits = detect_ats_in_urls([url])
        if direct_hits:
            hit = direct_hits[0]
            print(f"  ✓ Direct URL match: {hit['ats']} (slug: {hit['slug']})")
            result = {"company": name, "tier": tier, **hit, "scan_mode": "direct",
                       "scan_date": datetime.now().isoformat(timespec="seconds"), "verdict": "upgrade"}
            scan_results.append(result)
            existing[name] = result
            continue

        try:
            parsed = urlparse(url)
            root = f"{parsed.scheme}://{parsed.netloc}"
            map_result = client.map(root, search="careers jobs apply openings",
                                     limit=50, include_subdomains=True)
            links = map_result.links if hasattr(map_result, 'links') else map_result if isinstance(map_result, list) else []
            link_urls = []
            for link in links:
                if isinstance(link, str):
                    link_urls.append(link)
                elif isinstance(link, dict):
                    link_urls.append(link.get("url", ""))
                elif hasattr(link, "url"):
                    link_urls.append(link.url or "")
            link_urls = [u for u in link_urls if u]

            # Cache map results
            _save_map_cache(name, url, link_urls)

            print(f"  Map returned {len(link_urls)} URLs")
            hits = detect_ats_in_urls(link_urls)
            if hits:
                for h in hits:
                    print(f"  ✓ Found: {h['ats']} (slug: {h['slug']}) — {h['url']}")
                result = {"company": name, "tier": tier, **hits[0], "scan_mode": "map",
                           "scan_date": datetime.now().isoformat(timespec="seconds"), "verdict": "upgrade"}
            else:
                print(f"  ✗ No ATS detected")
                result = {"company": name, "tier": tier, "ats": "none", "slug": "", "url": url,
                           "scan_mode": "map", "scan_date": datetime.now().isoformat(timespec="seconds"), "verdict": "keep"}
            scan_results.append(result)
            existing[name] = result

        except Exception as e:
            print(f"  ⚠ Error: {str(e)[:100]}")
            result = {"company": name, "tier": tier, "ats": "error", "slug": "", "url": url,
                       "scan_mode": "map", "scan_date": datetime.now().isoformat(timespec="seconds"),
                       "verdict": "error", "error": str(e)[:200]}
            scan_results.append(result)
            existing[name] = result

        time.sleep(0.5)

    _save_results(existing)
    _print_summary(scan_results)

    # Apply if requested
    if apply:
        _apply_results(scan_results, dry_run=apply_dry_run)


def _print_summary(scan_results: list[dict]) -> None:
    """Print scan summary."""
    print(f"\n{'='*60}")
    print(f"  DISCOVERY RESULTS")
    print(f"{'='*60}\n")

    upgradeable = [r for r in scan_results if r.get("verdict") == "upgrade"]
    keep = [r for r in scan_results if r.get("verdict") == "keep"]
    errors = [r for r in scan_results if r.get("verdict") in ("error", "check_manually")]

    if upgradeable:
        print(f"✓ UPGRADEABLE ({len(upgradeable)}):")
        for r in upgradeable:
            print(f"  {r.get('tier', '?')} | {r['company']:30} → {r['ats']} (slug: {r.get('slug', '')})")

    if keep:
        print(f"\n✗ NO ATS FOUND ({len(keep)}) — keep current strategy:")
        for r in keep:
            print(f"  {r.get('tier', '?')} | {r['company']}")

    if errors:
        print(f"\n⚠ ERRORS / MANUAL CHECK ({len(errors)}):")
        for r in errors:
            print(f"  {r.get('tier', '?')} | {r['company']} — {r.get('error', 'no HTML')[:60]}")


def main():
    args = build_parser().parse_args()

    targets = _build_targets(args)
    if not targets:
        print("No companies match the given filters.")
        return

    # When --apply --dry-run: run the scan (not dry), but dry-run the DB apply step
    scan_dry_run = args.dry_run and not args.apply

    if args.html_scan:
        run_html_scan(targets, dry_run=scan_dry_run, apply=args.apply,
                      apply_dry_run=args.dry_run)
    else:
        run_map_scan(targets, dry_run=scan_dry_run, apply=args.apply,
                     apply_dry_run=args.dry_run)


if __name__ == "__main__":
    main()
