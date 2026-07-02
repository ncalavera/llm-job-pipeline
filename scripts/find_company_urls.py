#!/usr/bin/env python3
"""
Find official website URLs for board companies using Firecrawl search.
Adds found companies to Supabase for subsequent enrichment.

Usage:
    python3 scripts/find_company_urls.py [--dry-run] [--limit N]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def build_parser():
    """Construct the CLI parser. Defined before the heavy project imports so
    ``--help`` / ``-h`` prints usage without connecting to the database or
    loading the user profile (those happen only when a real command runs)."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    return parser


# Print help and exit BEFORE importing anything that touches the DB or profile.
from cli_help import wants_help

if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

from config import get_firecrawl_client

# Job board / aggregator domains to skip in search results
SKIP_DOMAINS = {
    "linkedin.com",
    "glassdoor.com",
    "indeed.com",
    "ziprecruiter.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "80000hours.org",
    "devex.com",
    "idealist.org",
    "impactsource.com",
    "jobs.ffwd.org",
    "wikipedia.org",
    "bloomberg.com",
    "crunchbase.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "youtube.com",
    "angel.co",
    "wellfound.com",
    "builtinnyc.com",
    "builtinsf.com",
    "charity.com",
    "charitynavigator.org",
    "guidestar.org",
}


def _load_known_companies() -> set[str]:
    """Return set of all canonical company names from Supabase."""
    from company_registry import _ALL_KNOWN_NAMES

    return _ALL_KNOWN_NAMES


def _load_companies_to_find() -> list[str]:
    """Load companies needing URLs.

    Two sources:
    1. Companies with high-scoring vacancies (llm_score >= 40) but no website
    2. Ghost candidates: status='candidate', no website, no alignment_score
    """
    from db_conn import get_conn
    from database_supabase import load_all_enrichment

    conn = get_conn()
    cur = conn.cursor()

    # Source 1: companies with high-scoring vacancies but no website
    cur.execute("""
        SELECT c.canonical_name
        FROM vacancy v
        JOIN company c ON v.company_id = c.id
        WHERE v.llm_score IS NOT NULL AND v.llm_score >= 40
          AND (c.website IS NULL OR c.website = '')
        GROUP BY c.canonical_name
    """)
    from_vacancies = {row[0] for row in cur.fetchall()}

    # Source 2: ghost candidates (no website, no alignment, pending review)
    cur.execute("""
        SELECT canonical_name
        FROM company
        WHERE status = 'candidate'
          AND (website IS NULL OR website = '')
          AND alignment_score IS NULL
    """)
    from_ghosts = {row[0] for row in cur.fetchall()}
    cur.close()

    all_names = from_vacancies | from_ghosts

    enrichment = load_all_enrichment()
    junk_patterns = [
        "[via ",
        " USD ",
        "New York",
        "San Francisco",
        "SAGE",
        "Individual Philanthropy",
        "Various Fellowship",
        "HUD",
        "Grants Associate",
        "Manager,",
        "Associate,",
        "Program Officer",
    ]

    result = []
    for name in all_names:
        if any(p in name for p in junk_patterns):
            continue
        has_mission = (
            name in enrichment
            and enrichment[name].get("mission_fit", {}).get("alignment_score") is not None
        )
        if not has_mission:
            result.append(name)

    return sorted(result)


def _get_search_results(search_data) -> list:
    """Extract result items from Firecrawl SearchData response."""
    if hasattr(search_data, "data") and search_data.data:
        return search_data.data
    if hasattr(search_data, "web") and search_data.web:
        return search_data.web
    if isinstance(search_data, dict):
        return search_data.get("data", []) or search_data.get("web", [])
    return []


def _search_website(client, company_name: str) -> str | None:
    """Search for company's official website via Firecrawl."""
    query = f"{company_name} official website"
    try:
        results = client.search(query=query, limit=5)

        for item in _get_search_results(results):
            url = getattr(item, "url", None) or (
                item.get("url") if isinstance(item, dict) else None
            )
            if not url:
                continue
            # Skip known job boards and aggregators
            domain = url.split("/")[2] if "://" in url else ""
            domain = domain.replace("www.", "")
            if any(skip in domain for skip in SKIP_DOMAINS):
                continue
            # Normalize to root domain
            parts = url.split("/")
            root = "/".join(parts[:3])  # https://domain.com
            return root

        return None
    except Exception as e:
        print(f"    ⚠ Search error: {e}", flush=True)
        return None


def _save_found_urls(entries: list[dict]):
    """Update company website URLs in Supabase."""
    if not entries:
        return
    from db_conn import get_conn

    conn = get_conn()
    cur = conn.cursor()
    for entry in entries:
        name = entry["Company Name"]
        website = entry["Website"]
        cur.execute(
            "UPDATE company SET website = %s WHERE canonical_name = %s",
            (website, name),
        )
        if cur.rowcount == 0:
            # Company doesn't exist yet — create as candidate
            cur.execute(
                """INSERT INTO company (canonical_name, website, status, aliases)
                   VALUES (%s, %s, 'candidate', ARRAY[%s]::text[])
                   ON CONFLICT (canonical_name) DO UPDATE SET website = EXCLUDED.website""",
                (name, website, name),
            )
    conn.commit()
    cur.close()


def main():
    args = build_parser().parse_args()

    companies = _load_companies_to_find()
    if args.limit:
        companies = companies[: args.limit]

    print(f"🔍 Companies to find URLs for: {len(companies)}")
    if args.dry_run:
        print("🏃 Dry run — no API calls")
        for c in companies:
            print(f"  • {c}")
        return

    client = get_firecrawl_client()
    found = []
    not_found = []

    for i, company in enumerate(companies, 1):
        print(f"\n[{i}/{len(companies)}] {company}", flush=True)
        url = _search_website(client, company)
        if url:
            print(f"    ✅ {url}", flush=True)
            found.append(
                {
                    "Company Name": company,
                    "Category": "",
                    "Product": "",
                    "Website": url,
                    "Careers URL": "",
                    "Offices": "",
                    "Notes": "auto-added for enrichment",
                    "Tier": "C",
                    "Experience Match": "",
                    "Personal Interest": "",
                    "User Comments": "",
                    "Status": "candidate",
                    "Fetch Strategy": "",
                    "ATS Slug": "",
                    "ATS Config": "",
                }
            )
        else:
            print("    ⚠ Not found", flush=True)
            not_found.append(company)

        # Brief pause to avoid rate limiting
        time.sleep(0.5)

    print(f"\n{'=' * 50}")
    print(f"✅ Found URLs: {len(found)}")
    print(f"⚠  Not found:  {len(not_found)}")

    if not_found:
        print("\nNot found:")
        for c in not_found:
            print(f"  • {c}")

    if found:
        _save_found_urls(found)
        print(f"\n📝 Updated {len(found)} company URLs in Supabase")
        print("Next: run enrichment with --tiers-only 'about,mission'")


if __name__ == "__main__":
    main()
