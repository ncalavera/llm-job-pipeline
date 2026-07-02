"""Consultants for Impact board (static JSON feed behind their Netlify widget)."""

import hashlib

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher

# consultantsforimpact.org/job-board embeds an <iframe src="https://cfi-job-board
# .netlify.app">, which itself fetches a single static JSON blob (no auth, no
# pagination). If the widget domain changes, refetch it from the page:
#   curl -s https://www.consultantsforimpact.org/job-board | grep -o 'cfi-job-board[^"]*'
_DEFAULT_DATA_URL = "https://cfi-job-board.netlify.app/board-data.json"


@board_fetcher("cfi_board_json")
def fetch_cfi_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from Consultants for Impact's board-data.json.

    CFI's own widget aggregates + rescores 80,000 Hours and Probably Good
    listings for a strategy-consultant audience (~1.5k entries, pre-sorted
    best-first by CFI's own relevance score). Listing-only — no description
    text in the feed, so ``full_description`` is left for the later blind-
    enrichment pass, which scrapes the direct employer ``url`` each entry
    already carries.

    board_cfg knobs: ``data_url`` (the JSON endpoint, overridable if it
    moves), ``max_jobs`` (default 150 — a cost cap per STRATEGY guardrail 3;
    the feed is pre-ranked so a cap keeps the top slice, not a random one).
    Does NOT filter on CFI's own relevance score — that judgment stays with
    this pipeline's scoring stage, not baked into the fetcher (same
    convention as the other boards' neutral ``*_ok`` gates).
    """
    board_name = board_cfg["name"]
    data_url = board_cfg.get("data_url", _DEFAULT_DATA_URL)
    max_jobs = int(board_cfg.get("max_jobs", 150))
    board_blacklist = board_cfg.get("board_blacklist", [])

    print(f"  [{board_name}] CFI board: fetching {data_url}...")
    resp = http.get(data_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    data = resp.json()
    raw = data.get("jobs") or []

    total_seen = len(raw)
    live = [j for j in raw if not j.get("expired")]

    for j in live:
        j["_title_proxy"] = j.get("title", "")
    filtered = _blacklist_filter(
        live,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["_title_proxy"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs: list[dict] = []
    for j in filtered:
        if len(jobs) >= max_jobs:
            break
        title = (j.get("title") or "").strip()
        org = (j.get("org") or "").strip()
        url = (j.get("url") or "").strip()
        if not title or not org or not url or _is_generic_pipeline_title(title):
            continue

        location = (j.get("location") or "").strip()
        if location == "—":  # CFI's placeholder for "unknown"
            location = ""

        seniority = (j.get("seniority") or "").strip()
        cause_areas = j.get("cause_areas") or []
        department = ", ".join(cause_areas[:3])

        snippet_parts = [p for p in (org, location, seniority) if p]
        snippet = " — ".join(snippet_parts)
        if j.get("part_time"):
            snippet += " (Part-Time)"
        if j.get("project_based"):
            snippet += " (Fixed-Term)"

        external_id = hashlib.md5(url.encode()).hexdigest()[:12]

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": department,
                "url": url,
                "external_id": external_id,
                "snippet": snippet,
                "deadline": (j.get("deadline") or "").strip(),
                "org_override": org,
                "org_url": board_cfg["url"],
            }
        )

    print(
        f"  [{board_name}] CFI board: {len(jobs)} relevant from {total_seen} total "
        f"({len(raw) - len(live)} expired, cap={max_jobs})"
    )
    return jobs
