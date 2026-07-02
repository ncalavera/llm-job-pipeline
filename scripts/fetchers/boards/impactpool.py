"""Impactpool impact-sector board (server-rendered HTML, free)."""

import re

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.registry import board_fetcher


def _impactpool_location_ok(loc: str) -> bool:
    # Neutral: no geography is privileged. Every location is accepted; drop a
    # location via the user profile's exclude_countries instead.
    return True


def _impactpool_seniority_ok(level: str) -> bool:
    # Neutral: no seniority is privileged. Drop a seniority via the user
    # profile's exclude_title_keywords instead.
    return True


@board_fetcher("impactpool_html")
def fetch_impactpool_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from impactpool.org/search by paginating server-rendered HTML.
    Free (no API key, no Firecrawl). Listing-only: title, org, location, seniority.
    Full description is not fetched — relies on later enrichment via /score pipeline.
    """
    from bs4 import BeautifulSoup

    board_name = board_cfg["name"]
    base_url = board_cfg["url"].rstrip("/")
    max_pages = int(board_cfg.get("max_pages", 5))
    board_blacklist = [kw.lower() for kw in board_cfg.get("board_blacklist", [])]

    # No org-level dedup here: the save layer dedups by canonical company + title
    # (and skips inactive companies). Filtering by the company registry would drop
    # vacancies from orgs we know but don't actually fetch directly (e.g. several
    # UN agencies), losing exactly what this board is best at.
    print(f"  [{board_name}] Impactpool: fetching up to {max_pages} pages...")

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    total_seen = 0
    rej = {"location": 0, "seniority": 0, "blacklist": 0}
    last_error = None

    for page in range(1, max_pages + 1):
        url = f"{base_url}?page={page}"
        try:
            resp = http.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        except Exception as exc:
            print(f"  [{board_name}] page {page} ERROR: {exc}")
            last_error = exc
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all(
            "a",
            attrs={"data-turbo-frame": "_top", "href": re.compile(r"^/jobs/\d+$")},
        )
        if not cards:
            break

        page_new_ids = 0
        for a in cards:
            href = a.get("href", "")
            m = re.match(r"^/jobs/(\d+)$", href)
            if not m:
                continue
            ext_id = m.group(1)
            if ext_id in seen_ids:
                continue
            seen_ids.add(ext_id)
            total_seen += 1
            page_new_ids += 1

            title_div = a.find("div", attrs={"type": "cardTitle"})
            org_div = a.find("div", attrs={"type": "bodyEmphasis"})
            title = title_div.get_text(strip=True) if title_div else ""
            org = org_div.get_text(strip=True) if org_div else ""
            all_typo = a.find_all("div", class_="ip-typography")
            texts = [d.get_text(strip=True) for d in all_typo]
            location = texts[2] if len(texts) > 2 else ""
            seniority = texts[3] if len(texts) > 3 else ""

            if not title or not org:
                continue

            t_lower = title.lower()
            if any(kw in t_lower for kw in GLOBAL_BLACKLIST_SUBSTR):
                rej["blacklist"] += 1
                continue
            if any(
                re.search(r"\b" + re.escape(bl.lower()) + r"\b", t_lower) for bl in GLOBAL_BLACKLIST
            ):
                rej["blacklist"] += 1
                continue
            if any(kw in t_lower for kw in board_blacklist):
                rej["blacklist"] += 1
                continue

            if not _impactpool_location_ok(location):
                rej["location"] += 1
                continue
            if not _impactpool_seniority_ok(seniority):
                rej["seniority"] += 1
                continue

            job_url = f"https://www.impactpool.org/jobs/{ext_id}"
            snippet = f"{org} — {location}. {seniority}".strip(" .")

            jobs.append(
                {
                    "title": title,
                    "location": location,
                    "department": "",
                    "url": job_url,
                    "external_id": ext_id,
                    "snippet": snippet,
                    "deadline": "",
                    "org_override": org,
                    "org_url": board_cfg["url"],
                }
            )

        if page_new_ids == 0:
            break

    if not jobs and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

    print(
        f"  [{board_name}] Impactpool: {len(jobs)} relevant from {total_seen} total "
        f"(rejected: location={rej['location']}, seniority={rej['seniority']}, "
        f"blacklist={rej['blacklist']})"
    )
    return jobs
