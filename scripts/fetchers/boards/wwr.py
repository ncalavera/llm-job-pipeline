"""We Work Remotely board (public RSS per category)."""

import hashlib
import re

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_multiline, _html_to_snippet
from fetchers.parsing import _is_generic_pipeline_title
from fetchers.registry import board_fetcher


@board_fetcher("wwr_rss")
def fetch_wwr_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from We Work Remotely category RSS feeds (free, no key).

    RSS per category: https://weworkremotely.com/categories/remote-{cat}-jobs.rss
    Categories come from WWR_CATEGORIES (comma list) or the board config
    default. Item titles are "Company: Role"; <region> narrows the remote zone.
    Parsed with stdlib xml.etree — no feedparser dependency.
    """
    import os
    import xml.etree.ElementTree as ET

    board_name = board_cfg["name"]
    cats_env = os.environ.get("WWR_CATEGORIES", "").strip()
    categories = (
        [c.strip() for c in cats_env.split(",") if c.strip()]
        or board_cfg.get("default_categories")
        or ["product", "management-and-finance"]
    )

    headers = {"User-Agent": "Mozilla/5.0 (job-pipeline RSS reader)"}
    raw_items = []
    last_error = None
    for cat in categories:
        url = f"https://weworkremotely.com/categories/remote-{cat}-jobs.rss"
        try:
            resp = http.get(url, headers=headers, timeout=20)
            root = ET.fromstring(resp.content)
            raw_items.extend(root.findall(".//item"))
        except Exception as e:
            print(f"  [{board_name}] WWR RSS ERROR ({cat}): {e}")
            last_error = e
            continue

    if not raw_items and last_error is not None:
        raise last_error  # every feed failed — let the boundary record why

    def _field(item, tag):
        el = item.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    total = len(raw_items)
    board_blacklist = [kw.lower() for kw in board_cfg.get("board_blacklist", [])]

    jobs = []
    seen_links: set = set()
    for item in raw_items:
        raw_title = _field(item, "title")
        link = _field(item, "link") or _field(item, "guid")
        if not raw_title or link in seen_links:
            continue
        seen_links.add(link)

        # "Company: Role" — split on the first colon.
        if ":" in raw_title:
            org, title = (s.strip() for s in raw_title.split(":", 1))
        else:
            org, title = f"[via {board_name}]", raw_title.strip()
        if not title or _is_generic_pipeline_title(title):
            continue

        t_lower = title.lower()
        if any(kw in t_lower for kw in GLOBAL_BLACKLIST_SUBSTR):
            continue
        if any(
            re.search(r"\b" + re.escape(bl.lower()) + r"\b", t_lower) for bl in GLOBAL_BLACKLIST
        ):
            continue
        if any(kw in t_lower for kw in board_blacklist):
            continue

        # WWR is remote-only; <region> narrows the zone
        # ("Anywhere in the World", "Europe Only" ...).
        region = _field(item, "region")
        loc = f"Remote, {region}" if region else "Remote"

        desc_html = _field(item, "description")
        full_description = _html_to_multiline(desc_html)
        category = _field(item, "category")

        jobs.append(
            {
                "title": title,
                "location": loc,
                "department": category,
                "url": link,
                "external_id": hashlib.md5(link.encode()).hexdigest()[:12],
                "snippet": _html_to_snippet(desc_html),
                "full_description": full_description,
                "compensation": "",
                "deadline": _field(item, "expires_at"),
                "org_override": org,
                "org_url": board_cfg["url"],
            }
        )

    print(
        f"  [{board_name}] WWR: {len(jobs)} relevant from {total} total "
        f"(categories: {', '.join(categories)})"
    )
    return jobs
