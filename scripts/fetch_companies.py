#!/usr/bin/env python3
"""Fetch company data via Firecrawl — scrape about pages, social signals.

Replaces scrape logic from old score_companies.py and social tier from enrich_companies.py.

Usage:
    fetch_companies.py --limit 20                 # Firecrawl scrape about pages
    fetch_companies.py --social --limit 10        # social signals (Glassdoor/LinkedIn/news)
    fetch_companies.py --company "Name1,Name2"    # specific companies
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))


def build_parser():
    """Construct the CLI parser. Defined before the heavy project imports so
    ``--help`` / ``-h`` prints usage without connecting to the database or
    loading the user profile (those happen only when a real command runs)."""
    import argparse

    parser = argparse.ArgumentParser(description="Fetch company data via Firecrawl")
    parser.add_argument(
        "--social", action="store_true", help="Fetch social signals instead of about pages"
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--company", type=str, help="Specific companies (comma-separated)")
    parser.add_argument("--force", action="store_true", help="Re-scrape cached companies")
    return parser


# Print help and exit BEFORE importing anything that touches the DB or profile.
from cli_help import wants_help

if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

from score_companies import _get_main_repo_root, SCRAPE_CACHE_PATH

# ---------------------------------------------------------------------------
# Cache paths
# ---------------------------------------------------------------------------

_MAIN_ROOT = _get_main_repo_root()
COMPANY_MD_CACHE = _MAIN_ROOT / ".firecrawl" / "companies"

# Keywords for page discovery
_ABOUT_PAGE_KEYWORDS = {
    "about",
    "mission",
    "team",
    "values",
    "impact",
    "who-we-are",
    "our-story",
    "what-we-do",
}


# ---------------------------------------------------------------------------
# Firecrawl helpers
# ---------------------------------------------------------------------------


def _get_firecrawl_client():
    """Get Firecrawl client, return None if unavailable."""
    try:
        from firecrawl import FirecrawlApp
        import os

        key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not key:
            print("WARNING: FIRECRAWL_API_KEY not set", file=sys.stderr)
            return None
        return FirecrawlApp(api_key=key)
    except ImportError:
        print("WARNING: firecrawl package not installed", file=sys.stderr)
        return None


def _with_backoff(fn, *args, **kwargs):
    """Call fn with exponential backoff on 529/overload errors."""
    delays = [5, 15, 45]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            print(f"    Retrying in {delay}s (attempt {attempt + 1})...", flush=True)
            time.sleep(delay)
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err_str = str(e)
            is_overload = "529" in err_str or "overloaded" in err_str.lower()
            if is_overload and attempt < len(delays):
                continue
            raise
    return None


# ---------------------------------------------------------------------------
# Page discovery + scraping
# ---------------------------------------------------------------------------


def _discover_relevant_pages(client, url: str) -> list[str]:
    """Use Firecrawl map() to find about/mission/team pages."""
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    try:
        result = _with_backoff(
            client.map,
            root,
            search="about mission team values impact",
            limit=20,
            include_subdomains=False,
        )
        links = (
            result.links if hasattr(result, "links") else result if isinstance(result, list) else []
        )
    except Exception as e:
        print(f"    Map failed for {root}: {str(e)[:80]}")
        return [url]

    scored = []
    seen = set()
    for link in links:
        if isinstance(link, dict):
            link_url = link.get("url", "")
            title = link.get("title", "")
        elif hasattr(link, "url"):
            link_url = link.url or ""
            title = getattr(link, "title", "") or ""
        elif isinstance(link, str):
            link_url = link
            title = ""
        else:
            continue

        if not link_url or link_url in seen:
            continue
        seen.add(link_url)
        path = urlparse(link_url).path.lower().rstrip("/")

        if any(
            skip in path
            for skip in (
                "/login",
                "/signup",
                "/register",
                "/privacy",
                "/terms",
                "/cookie",
                "/legal",
                "/404",
            )
        ):
            continue

        score = 0
        if path in ("", "/"):
            score = 100
        for kw in _ABOUT_PAGE_KEYWORDS:
            if kw in path:
                score = max(score, 90)
        for kw in ("careers", "jobs", "news", "blog", "press", "media"):
            if kw in path:
                score = max(score, 60)
        title_lower = title.lower()
        for kw in ("about", "mission", "team", "impact", "careers", "news"):
            if kw in title_lower:
                score = max(score, 50)
        if score > 0:
            scored.append((score, link_url))

    scored.sort(key=lambda x: -x[0])
    urls = [u for _, u in scored]
    if url not in urls:
        urls.insert(0, url)

    selected = urls[:3]
    print(
        f"    Map found {len(links)} pages, selected {len(selected)}: "
        + ", ".join(urlparse(u).path or "/" for u in selected)
    )
    return selected


def _scrape_markdown(client, url: str) -> str:
    """Scrape single page as markdown."""
    try:
        result = _with_backoff(
            client.scrape,
            url,
            formats=["markdown"],
            only_main_content=True,
            timeout=60000,
        )
        md = (
            result.markdown
            if hasattr(result, "markdown")
            else (result.get("markdown") if isinstance(result, dict) else "")
        )
        return md or ""
    except Exception as e:
        print(f"    Scrape failed for {url}: {str(e)[:80]}")
        return ""


def _scrape_company_pages(client, name: str, url: str) -> tuple[str | None, int]:
    """Map + scrape top 2-3 pages. Returns (combined_markdown, scraped_count)."""
    if not url:
        print(f"    No website URL for {name}")
        return None, 0

    print(f"    Discovering pages on {url}...", flush=True)
    pages_to_scrape = _discover_relevant_pages(client, url)

    combined = ""
    scraped_count = 0
    for page_url in pages_to_scrape[:3]:
        md = _scrape_markdown(client, page_url)
        if md:
            combined += f"=== PAGE: {page_url} ===\n{md}\n\n"
            scraped_count += 1

    if not combined or len(combined) < 100:
        print(f"    No useful content scraped from {name}")
        return None, 0

    return combined, scraped_count


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def _load_scrape_cache() -> dict[str, dict]:
    """Load cached scrape results. Returns {name: {url, markdown}}."""
    if not SCRAPE_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(SCRAPE_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_scrape_cache(cache: dict):
    """Save scrape cache to JSON."""
    SCRAPE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCRAPE_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  Cache saved: {SCRAPE_CACHE_PATH.name} ({len(cache)} companies)")


def _cache_company_markdown(name: str, markdown: str):
    """Save company markdown to .firecrawl/companies/{slug}.md."""
    COMPANY_MD_CACHE.mkdir(parents=True, exist_ok=True)
    slug = name.lower().replace(" ", "_").replace(".", "")
    try:
        (COMPANY_MD_CACHE / f"{slug}.md").write_text(markdown, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_companies(*, company_filter=None, limit=None, for_social=False):
    """Load companies needing scrape or social enrichment.

    Default: candidates without cached markdown, with website.
    --social: candidates/active without social data.
    """
    from db_conn import get_conn

    conn = get_conn()
    cur = conn.cursor()

    if company_filter:
        names = [n.strip() for n in company_filter.split(",")]
        cur.execute(
            """
            SELECT id, canonical_name, website
            FROM company
            WHERE canonical_name = ANY(%s)
              AND website IS NOT NULL AND website != ''
            ORDER BY canonical_name
        """,
            (names,),
        )
    elif for_social:
        cur.execute("""
            SELECT id, canonical_name, website
            FROM company
            WHERE status IN ('candidate', 'active')
              AND website IS NOT NULL AND website != ''
            ORDER BY canonical_name
        """)
    else:
        cur.execute("""
            SELECT id, canonical_name, website
            FROM company
            WHERE status = 'candidate'
              AND website IS NOT NULL AND website != ''
            ORDER BY canonical_name
        """)

    columns = [desc[0] for desc in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    cur.close()
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------------------
# Social signals (Firecrawl search)
# ---------------------------------------------------------------------------


def _enrich_social_signals(client, name: str) -> dict | None:
    """Fetch Glassdoor rating, LinkedIn size, and recent news via Firecrawl search."""
    social = {
        "glassdoor_rating": None,
        "linkedin_employees": "",
        "recent_news": [],
        "social_enriched_at": datetime.now().isoformat(),
    }

    def _get_web_results(search_data) -> list:
        if hasattr(search_data, "web") and search_data.web:
            return search_data.web
        return []

    # Glassdoor
    try:
        print(f"    Searching Glassdoor for {name}...", flush=True)
        results = _with_backoff(client.search, query=f"{name} Glassdoor rating", limit=3)
        for item in _get_web_results(results):
            snippet = getattr(item, "description", "") or ""
            rating_match = re.search(r"(\d\.\d)\s*/\s*5", snippet)
            if rating_match:
                social["glassdoor_rating"] = float(rating_match.group(1))
                break
    except Exception as e:
        print(f"    Glassdoor search failed: {str(e)[:80]}")

    # LinkedIn
    try:
        print(f"    Searching LinkedIn for {name}...", flush=True)
        results = _with_backoff(client.search, query=f"{name} LinkedIn company employees", limit=3)
        for item in _get_web_results(results):
            snippet = getattr(item, "description", "") or ""
            emp_match = re.search(r"([\d,]+-[\d,]+)\s*employees", snippet, re.IGNORECASE)
            if emp_match:
                social["linkedin_employees"] = emp_match.group(1).replace(",", "")
                break
            emp_match2 = re.search(r"([\d,]+)\s*employees", snippet, re.IGNORECASE)
            if emp_match2:
                social["linkedin_employees"] = emp_match2.group(1).replace(",", "")
                break
    except Exception as e:
        print(f"    LinkedIn search failed: {str(e)[:80]}")

    # News
    try:
        print(f"    Searching news for {name}...", flush=True)
        results = _with_backoff(client.search, query=f'"{name}" news 2025 2026', limit=3)
        for item in _get_web_results(results)[:3]:
            title = getattr(item, "title", "") or ""
            item_url = getattr(item, "url", "") or ""
            if title:
                social["recent_news"].append({"title": title[:200], "url": item_url})
    except Exception as e:
        print(f"    News search failed: {str(e)[:80]}")

    return social


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_scrape(args):
    """Scrape about pages for companies via Firecrawl."""
    client = _get_firecrawl_client()
    if not client:
        sys.exit(1)

    companies = _load_companies(company_filter=args.company, limit=args.limit)
    cache = _load_scrape_cache()

    if not companies:
        print("No companies to scrape.")
        return

    # Filter out already-cached companies (unless --force)
    if not args.force:
        to_scrape = [c for c in companies if c["canonical_name"] not in cache]
        cached_hits = len(companies) - len(to_scrape)
        if cached_hits:
            print(f"  {cached_hits} companies already in cache (use --force to re-scrape)")
        companies = to_scrape

    if not companies:
        print("All companies already cached.")
        return

    print(f"Scraping {len(companies)} companies via Firecrawl")

    scraped = 0
    errors = 0
    for i, c in enumerate(companies, 1):
        name = c["canonical_name"]
        url = (c.get("website") or "").strip()

        print(f"  [{i}/{len(companies)}] {name:40s}", end="", flush=True)

        markdown, page_count = _scrape_company_pages(client, name, url)
        if markdown:
            cache[name] = {"url": url, "markdown": markdown}
            _cache_company_markdown(name, markdown)
            _save_scrape_cache(cache)
            scraped += 1
            print(f"  OK: {page_count} pages, {len(markdown)} chars")
        else:
            errors += 1
            print("  FAILED")

    print(f"\nDone! Scraped {scraped} companies. Errors: {errors}")


def cmd_social(args):
    """Fetch social signals for companies."""
    client = _get_firecrawl_client()
    if not client:
        sys.exit(1)

    companies = _load_companies(
        company_filter=args.company,
        limit=args.limit,
        for_social=True,
    )

    if not companies:
        print("No companies need social enrichment.")
        return

    from db_conn import get_conn
    from psycopg2.extras import Json

    conn = get_conn()
    print(f"Fetching social signals for {len(companies)} companies")

    enriched = 0
    for i, c in enumerate(companies, 1):
        name = c["canonical_name"]
        print(f"  [{i}/{len(companies)}] {name:40s}", end="", flush=True)

        social = _enrich_social_signals(client, name)
        if not social:
            print("  no data")
            continue

        rating = social.get("glassdoor_rating", "?")
        news = len(social.get("recent_news", []))
        print(f"  Glassdoor={rating}, news={news}")

        cur = conn.cursor()
        cur.execute(
            "UPDATE company SET social = %s WHERE id = %s",
            (Json(social), c["id"]),
        )
        cur.close()
        enriched += 1

        if enriched % 5 == 0:
            conn.commit()

    conn.commit()
    print(f"\nDone! Enriched {enriched}/{len(companies)} companies with social signals.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    args = build_parser().parse_args()

    if args.social:
        cmd_social(args)
    else:
        cmd_scrape(args)


if __name__ == "__main__":
    main()
