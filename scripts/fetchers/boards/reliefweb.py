"""ReliefWeb humanitarian job board (public RSS feed, no auth)."""

import hashlib
import re

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_snippet
from fetchers.registry import board_fetcher


def _extract_rss_field(html: str, pattern: str) -> str:
    """Extract a field from ReliefWeb RSS description HTML."""
    m = re.search(pattern, html)
    return m.group(1).strip() if m else ""


@board_fetcher("reliefweb_api")
def fetch_reliefweb_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from ReliefWeb RSS feed (free, no registration).
    The JSON API requires pre-approved appname since Nov 2025,
    so we use the public RSS feed instead.
    """
    import xml.etree.ElementTree as ET

    board_name = board_cfg["name"]

    # RSS feed with limit=50 (server caps at 20 per request, so we fetch twice)
    base = "https://reliefweb.int/jobs/rss.xml"
    print(f"  [{board_name}] ReliefWeb RSS: fetching...")

    all_items = []
    last_error = None
    # Fetch without search (broadest), then filter client-side
    for offset in [0, 20]:
        url = f"{base}?limit=20&offset={offset}"
        try:
            resp = http.get(url, timeout=20)
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")
            all_items.extend(items)
            if len(items) < 20:
                break
        except Exception as e:
            print(f"  [{board_name}] RSS ERROR (offset={offset}): {e}")
            last_error = e
            break

    if not all_items and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

    total = len(all_items)
    board_blacklist = [kw.lower() for kw in board_cfg.get("board_blacklist", [])]

    jobs = []
    for item in all_items:
        title = item.find("title").text if item.find("title") is not None else ""
        job_url = item.find("link").text if item.find("link") is not None else ""
        desc_html = item.find("description").text if item.find("description") is not None else ""

        # Apply GLOBAL_BLACKLIST (word boundaries) + GLOBAL_BLACKLIST_SUBSTR (substring)
        t_lower = title.lower()
        if any(kw in t_lower for kw in GLOBAL_BLACKLIST_SUBSTR):
            continue
        if any(
            re.search(r"\b" + re.escape(bl.lower()) + r"\b", t_lower) for bl in GLOBAL_BLACKLIST
        ):
            continue
        # Board-specific blacklist (substring match)
        if any(kw in t_lower for kw in board_blacklist):
            continue

        # Extract metadata from description HTML
        org = _extract_rss_field(desc_html, r"Organization:\s*([^<]+)")
        location = _extract_rss_field(desc_html, r"Country:\s*([^<]+)")
        deadline = _extract_rss_field(desc_html, r"Closing date:\s*([^<]+)")

        if not org:
            org = f"[via {board_name}]"

        # Extract snippet (the actual description text after metadata divs)
        snippet = _html_to_snippet(desc_html)

        # External ID from URL (e.g. /job/4199305/...)
        ext_id_match = re.search(r"/job/(\d+)/", job_url)
        ext_id = (
            ext_id_match.group(1)
            if ext_id_match
            else hashlib.md5(job_url.encode()).hexdigest()[:12]
        )

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": "",
                "url": job_url,
                "external_id": ext_id,
                "snippet": snippet,
                "deadline": deadline,
                "org_override": org,
                "org_url": board_cfg["url"],
            }
        )

    print(
        f"  [{board_name}] ReliefWeb: {len(jobs)} relevant from {total} total (blacklist applied)"
    )
    return jobs
