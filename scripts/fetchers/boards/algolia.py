"""Generic Algolia-backed job board (e.g. the 80,000 Hours job board)."""

import hashlib
import json
import os
import re
import uuid

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher
from source_observations import record_source_observations, record_source_run


@board_fetcher("algolia_api")
def fetch_algolia_board(board_cfg: dict) -> list[dict]:
    """Query an Algolia search index directly via REST API (free, no Firecrawl).
    Applies GLOBAL_BLACKLIST + board-specific blacklist. NO caps, NO keyword/location filters.
    """
    app_id = board_cfg["algolia_app_id"]
    api_key = board_cfg["algolia_api_key"]
    index = board_cfg["algolia_index"]
    board_name = board_cfg["name"]
    run_id = os.environ.get("JOBS_RUN_ID") or str(uuid.uuid4())
    source_key = (
        board_cfg.get("source_key")
        or board_cfg.get("id")
        or re.sub(r"[^a-z0-9]+", "_", board_name.lower()).strip("_")
    )
    source_url = board_cfg.get("url")
    ledger_ok = record_source_run(run_id, source_key, source_url)
    if run_id and not ledger_ok:
        from fetchers.registry import record_fetch_error

        record_fetch_error(board_name, "error: source ledger unavailable")

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
    reported_total = None

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
        if reported_total is None and data.get("nbHits") is not None:
            reported_total = int(data["nbHits"])
        if not hits:
            break
        all_hits.extend(hits)

        if page >= data.get("nbPages", 1) - 1:
            break
        page += 1

    if last_error is None and reported_total is not None and len(all_hits) < reported_total:
        last_error = RuntimeError(
            f"incomplete pagination: observed {len(all_hits)} of advertised {reported_total} hits"
        )

    if not all_hits and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

    # Durable raw capture happens before parser filters run. A second upsert
    # below annotates each row with the parser outcome.
    raw_observations = [
        {
            "external_id": hit.get("objectID")
            or hashlib.md5(
                f"{hit.get('company_name', '')}:{hit.get('title', '')}".encode()
            ).hexdigest()[:12],
            "title": hit.get("title") or "",
            "organization": (hit.get("company_name") or "").strip(),
            "url": hit.get("url_external") or "",
            "outcome": "observed",
        }
        for hit in all_hits
    ]
    observations_ok = record_source_observations(run_id, source_key, source_url, raw_observations)

    # Apply GLOBAL_BLACKLIST + board-specific blacklist (NO caps, NO location filter, NO keyword filter)
    board_blacklist = board_cfg.get("board_blacklist", [])
    combined_blacklist = GLOBAL_BLACKLIST + board_blacklist
    filtered = _blacklist_filter(
        all_hits,
        combined_blacklist,
        title_fields=["title"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    # Keep the source's complete observed roster before any filtering. This is
    # also the reconciliation key for newsletters, where the canonical vacancy
    # row may have been filtered out before it reached the dashboard.
    filtered_ids = {id(hit) for hit in filtered}
    observations = []
    for hit in all_hits:
        title = hit.get("title") or ""
        if id(hit) not in filtered_ids:
            reason = "blacklist"
            outcome = "excluded"
        elif _is_generic_pipeline_title(title):
            reason = "generic_pipeline_title"
            outcome = "excluded"
        else:
            reason = None
            outcome = "accepted"
        observations.append(
            {
                "external_id": hit.get("objectID")
                or hashlib.md5(f"{hit.get('company_name', '')}:{title}".encode()).hexdigest()[:12],
                "title": title,
                "organization": (hit.get("company_name") or "").strip(),
                "url": hit.get("url_external") or "",
                "outcome": outcome,
                "reason": reason,
            }
        )
    observations_ok = observations_ok and record_source_observations(
        run_id,
        source_key,
        source_url,
        observations,
    )
    final_ledger_ok = record_source_run(
        run_id,
        source_key,
        source_url,
        raw_count=len(all_hits),
        accepted_count=sum(o["outcome"] == "accepted" for o in observations),
        excluded_count=sum(o["outcome"] == "excluded" for o in observations),
        complete=last_error is None and ledger_ok and observations_ok,
        error=str(last_error)
        if last_error
        else (None if ledger_ok and observations_ok else "source ledger write failed"),
    )
    if run_id and (not observations_ok or not final_ledger_ok):
        from fetchers.registry import record_fetch_error

        record_fetch_error(board_name, "error: source ledger write failed")

    # A partial page walk is useful evidence, but it is not a complete source
    # run. Keep the rows already collected while exposing the failure to the
    # fetch boundary so gone detection/publish gates cannot treat it as healthy.
    if last_error is not None:
        from fetchers.registry import record_fetch_error

        record_fetch_error(board_name, f"error: incomplete pagination: {last_error}")

    jobs = []
    generic_filtered_out = 0
    for hit in all_hits:  # raw intake; observations above retain filter reasons
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
                "org_override": org,
                "org_url": board_cfg["url"],
                "preserve_listing": True,
                "_source_run": run_id,
                "_source_key": source_key,
            }
        )

    print(
        f"  [{board_name}] Algolia: {len(jobs)} retained from {len(all_hits)} total"
        f" (generic postings flagged: {generic_filtered_out})"
    )
    return jobs
