"""Generic Algolia-backed job board (e.g. the 80,000 Hours job board)."""

import hashlib
import json
import re
from datetime import datetime, timezone

from fetchers import http
from fetchers.parsing import _is_generic_pipeline_title
from fetchers.registry import board_fetcher


@board_fetcher("algolia_api")
def fetch_algolia_board(board_cfg: dict) -> list[dict]:
    """Query an Algolia search index directly via REST API (free, no Firecrawl).

    NO caps, NO keyword/location filters: every listing is offered to the save
    layer, which runs the shared title/quality gate (each job carries
    ``preserve_listing``). The observation ledger is written by the shared board
    loop in fetch_vacancies, keyed by board id.
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

    # Algolia stops paging at 1000 hits per query. A board that outgrows that
    # names `algolia_partitions`: filters that split the index into disjoint
    # parts under the cap. Each part is walked on its own and hits merge by objectID.
    partitions = board_cfg.get("algolia_partitions")
    all_hits = []
    seen_ids = set()
    per_page = 200
    last_error = None
    reported_total = None
    if partitions:
        # The whole index's own count, so a hit outside every partition reads as missing.
        count = json.dumps({"query": "", "hitsPerPage": 0})
        reported_total = http.post(url, data=count, headers=headers, timeout=15).json().get("nbHits")

    for partition in partitions or [""]:
        page = 0
        while True:
            body = {"query": "", "hitsPerPage": per_page, "page": page}
            if partition:
                body["filters"] = partition
            try:
                resp = http.post(url, data=json.dumps(body), headers=headers, timeout=15)
                data = resp.json()
            except Exception as e:
                print(f"  [{board_name}] Algolia ERROR {partition or 'all'} page {page}: {e}")
                last_error = e
                break

            if not partitions and page == 0 and data.get("nbHits") is not None:
                reported_total = int(data["nbHits"])
            hits = data.get("hits", [])
            if not hits:
                break
            for hit in hits:
                key = hit.get("objectID")
                if key and key in seen_ids:
                    continue
                if key:
                    seen_ids.add(key)
                all_hits.append(hit)

            if page >= data.get("nbPages", 1) - 1:
                break
            page += 1

    if last_error is None and reported_total is not None and len(all_hits) < reported_total:
        last_error = RuntimeError(f"observed {len(all_hits)} of advertised {reported_total} hits")

    if not all_hits and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

    # A partial page walk is useful evidence, but it is not a complete source
    # run. Keep the rows already collected while exposing the failure to the
    # fetch boundary so gone detection/publish gates cannot treat it as healthy.
    if last_error is not None:
        from fetchers.registry import record_fetch_error

        record_fetch_error(board_name, f"error: incomplete pagination: {last_error}")

    jobs = []
    generic_filtered_out = 0
    for hit in all_hits:  # raw intake; the save layer runs the quality gate
        org = (hit.get("company_name") or "").strip() or f"[via {board_name}]"
        title = hit.get("title") or ""
        if _is_generic_pipeline_title(title):
            generic_filtered_out += 1
        # Location: join city tags, fallback to country tags
        cities = hit.get("tags_city") or []
        location = ", ".join(cities) if cities else ", ".join(hit.get("tags_country") or [])
        job_url = hit.get("url_external") or ""
        # `description_short` is the ONLY role text this index carries — the
        # `description` field is always empty (verified across the live index),
        # and the full posting lives off-site at url_external. So the full
        # cleaned text feeds full_description uncapped; `snippet` is only a
        # capped preview for the digest/card.
        desc_text = hit.get("description_short") or ""
        desc_text = re.sub(r"<[^>]+>", " ", desc_text)
        desc_text = re.sub(r"\s+", " ", desc_text).strip()
        snippet = desc_text
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
        # This index stores closes_at as UNIX seconds; the save layer parses a
        # date string, so convert here rather than hand it an int.
        closes_at = hit.get("closes_at")
        deadline = (
            datetime.fromtimestamp(closes_at, tz=timezone.utc).date().isoformat()
            if isinstance(closes_at, (int, float)) and not isinstance(closes_at, bool)
            else ""
        )

        desc_parts = [desc_text]
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
                "deadline": deadline,
                "org_override": org,
                "org_url": board_cfg["url"],
                "preserve_listing": True,
            }
        )

    print(
        f"  [{board_name}] Algolia: {len(jobs)} retained from {len(all_hits)} total"
        f" (generic postings flagged: {generic_filtered_out})"
    )
    return jobs
