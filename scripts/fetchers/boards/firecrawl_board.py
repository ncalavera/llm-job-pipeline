"""Generic Firecrawl-scraped job board (markdown-only, 1 credit per page)."""

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers.firecrawl import fetch_firecrawl_scrape
from fetchers.parsing import _blacklist_filter, _extract_org_from_listing
from fetchers.registry import board_fetcher


@board_fetcher("firecrawl_board")
def fetch_firecrawl_board(board_cfg: dict) -> list[dict]:
    """Scrape a job board page via Firecrawl, parse listings, apply GLOBAL_BLACKLIST."""
    board_name = board_cfg["name"]
    url = board_cfg["url"]

    raw_jobs = fetch_firecrawl_scrape(board_name, url, use_json=False)
    # Apply GLOBAL_BLACKLIST only (NO keyword filter, NO cap)
    filtered = _blacklist_filter(
        raw_jobs, GLOBAL_BLACKLIST, title_fields=["title"], substr_blacklist=GLOBAL_BLACKLIST_SUBSTR
    )

    jobs = []
    for job in filtered:  # NO cap — LLM scoring decides relevance
        org = _extract_org_from_listing(job.get("snippet", ""), job.get("title", ""), board_name)
        jobs.append(
            {
                **job,
                "org_override": org,
                "org_url": job.get("url") or url,
            }
        )

    print(
        f"  [{board_name}] Board: {len(jobs)} relevant from {len(raw_jobs)} raw (blacklist applied)"
    )
    return jobs
