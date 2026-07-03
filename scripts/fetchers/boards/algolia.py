"""Generic Algolia-backed job board (e.g. the 80,000 Hours job board)."""

import hashlib
import json
import re

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher


@board_fetcher("algolia_api")
def fetch_algolia_board(board_cfg: dict) -> list[dict]:
    """Query an Algolia search index directly via REST API (free, no Firecrawl).
    Applies GLOBAL_BLACKLIST + board-specific blacklist. NO caps, NO keyword/location filters.
    """
    app_id = board_cfg["algolia_app_id"]
    api_key = board_cfg["algolia_api_key"]
    index = board_cfg["algolia_index"]
    board_name = board_cfg["name"]

    url = f"https://{app_id}-dsn.algolia.net/1/indexes/{index}/query"
    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
        "Content-Type": "application/json",
    }

    # Fetch all hits from the index (paginate through everything)
    all_hits = []
    page = 0
    per_page = 200
    last_error = None

    while True:
        payload = json.dumps(
            {
                "query": "",
                "hitsPerPage": per_page,
                "page": page,
            }
        )
        try:
            resp = http.post(url, data=payload, headers=headers, timeout=15)
            data = resp.json()
        except Exception as e:
            print(f"  [{board_name}] Algolia ERROR page {page}: {e}")
            last_error = e
            break

        hits = data.get("hits", [])
        if not hits:
            break
        all_hits.extend(hits)

        if page >= data.get("nbPages", 1) - 1:
            break
        page += 1

    if not all_hits and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

    # Apply GLOBAL_BLACKLIST + board-specific blacklist (NO caps, NO location filter, NO keyword filter)
    board_blacklist = board_cfg.get("board_blacklist", [])
    combined_blacklist = GLOBAL_BLACKLIST + board_blacklist
    filtered = _blacklist_filter(
        all_hits,
        combined_blacklist,
        title_fields=["title"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs = []
    generic_filtered_out = 0
    for hit in filtered:  # NO cap — LLM scoring decides relevance
        org = (hit.get("company_name") or "").strip() or f"[via {board_name}]"
        title = hit.get("title") or ""
        if _is_generic_pipeline_title(title):
            generic_filtered_out += 1
            continue
        # Location: join city tags, fallback to country tags
        cities = hit.get("tags_city") or []
        location = ", ".join(cities) if cities else ", ".join(hit.get("tags_country") or [])
        job_url = hit.get("url_external") or ""
        # Strip HTML from description_short
        snippet = hit.get("description_short") or ""
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if len(snippet) > 400:
            snippet = snippet[:400].rsplit(" ", 1)[0] + "…"

        # Build full_description from all available Algolia fields
        comp_desc = hit.get("company_description") or ""
        comp_desc = re.sub(r"<[^>]+>", " ", comp_desc)
        comp_desc = re.sub(r"\s+", " ", comp_desc).strip()
        skills = ", ".join(hit.get("tags_skill") or [])
        loc_type = ", ".join(hit.get("tags_location_type") or [])
        exp_req = ", ".join(hit.get("tags_exp_required") or [])
        areas = ", ".join(hit.get("tags_area") or [])
        salary = hit.get("salary") or ""

        desc_parts = [snippet]
        if comp_desc:
            desc_parts.append(f"About {org}: {comp_desc}")
        meta = []
        if areas:
            meta.append(f"Area: {areas}")
        if skills:
            meta.append(f"Skills: {skills}")
        if loc_type:
            meta.append(f"Location type: {loc_type}")
        if exp_req:
            meta.append(f"Experience: {exp_req}")
        if salary:
            meta.append(f"Salary: {salary}")
        if meta:
            desc_parts.append(" | ".join(meta))

        full_description = "\n\n".join(desc_parts)

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": ", ".join(hit.get("tags_area") or []),
                "url": job_url,
                "external_id": hit.get("objectID")
                or hashlib.md5(f"{org}:{title}".encode()).hexdigest()[:12],
                "snippet": snippet,
                "full_description": full_description,
                "compensation": hit.get("salary") or "",
                "org_override": org,
                "org_url": board_cfg["url"],
            }
        )

    print(
        f"  [{board_name}] Algolia: {len(jobs)} relevant from {len(all_hits)} total"
        f" (generic postings filtered: {generic_filtered_out})"
    )
    return jobs
