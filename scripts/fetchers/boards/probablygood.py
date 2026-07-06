"""Probably Good job board (Algolia search-only key baked into the page — free, no auth)."""

import hashlib
import json

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_snippet
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher

_APP_ID = "OJJMNNBR0H"
# Search-scoped secured key (embeds a `visible_to` filter) returned by
# jobs.probablygood.org's own GraphQL backend. If it 403s, refetch it:
#   curl -s -X POST https://backend.jobs.probablygood.org/api/graphql \
#     -H "Content-Type: application/json" \
#     -d '{"operationName":"AlgoliaSearchKey","variables":{},"query":"query AlgoliaSearchKey { algolia_search_key { api_key app_id index_name_jobs } }"}'
_SEARCH_KEY = (
    "NDYzODg5MzZmMGQ0YWMxYTQ4NTA4NmM1ODVkY2Q4MGFiZTQ3MzU5OTlhYTYyMjY3MzNkNjVhNjczZWU1NmE4"
    "OWZpbHRlcnM9dmlzaWJsZV90byUzQWdyb3VwJTJGSU5URVJOQUwlMjBPUiUyMHZpc2libGVfdG8lM0Fncm91"
    "cCUyRlBVQkxJQw=="
)
_INDEX = "jobs_prod"
_SITE = "https://jobs.probablygood.org"


@board_fetcher("probablygood_algolia")
def fetch_probablygood_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from Probably Good's job board via its embedded Algolia key.

    Algolia's index caps pagination at 1000 reachable hits (paginationLimitedTo)
    even when nbHits is larger — logged explicitly rather than silently dropped.
    """
    board_name = board_cfg["name"]
    board_blacklist = board_cfg.get("board_blacklist", [])

    url = f"https://{_APP_ID}-dsn.algolia.net/1/indexes/{_INDEX}/query"
    headers = {
        "X-Algolia-Application-Id": _APP_ID,
        "X-Algolia-API-Key": _SEARCH_KEY,
        "Content-Type": "application/json",
    }

    all_hits: list[dict] = []
    page = 0
    per_page = 200
    last_error = None
    nb_hits = None
    while True:
        payload = json.dumps({"query": "", "hitsPerPage": per_page, "page": page})
        try:
            resp = http.post(url, data=payload, headers=headers, timeout=15)
            data = resp.json()
        except Exception as exc:
            print(f"  [{board_name}] Algolia ERROR page {page}: {exc}")
            last_error = exc
            break
        nb_hits = data.get("nbHits", nb_hits)
        hits = data.get("hits", [])
        if not hits:
            break
        all_hits.extend(hits)
        if page >= data.get("nbPages", 1) - 1:
            break
        page += 1

    if not all_hits and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

    if nb_hits and len(all_hits) < nb_hits:
        print(
            f"  [{board_name}] Algolia index caps pagination at {len(all_hits)} of "
            f"{nb_hits} total hits — remaining rows skipped this run"
        )

    for h in all_hits:
        h["_title_proxy"] = h.get("title", "")
    filtered = _blacklist_filter(
        all_hits,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["_title_proxy"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs: list[dict] = []
    for hit in filtered:
        title = (hit.get("title") or "").strip()
        if not title or _is_generic_pipeline_title(title):
            continue

        org_data = hit.get("org") or {}
        org = (org_data.get("name") or "").strip()
        org_url = (org_data.get("website") or "").strip() or board_cfg["url"]

        slug = hit.get("slug") or ""
        job_url = (hit.get("url_external") or "").strip() or (
            f"{_SITE}/jobs/{slug}" if slug else ""
        )

        location_names = []
        for loc in hit.get("locations") or []:
            name = (loc.get("name") or "").strip()
            if name and name not in location_names:
                location_names.append(name)
        location = ", ".join(location_names)

        department = ", ".join(t.get("name", "") for t in (hit.get("tags_area") or []))

        description = (hit.get("description") or "").strip()
        snippet = _html_to_snippet(description)
        full_description = description

        meta = []
        skills = ", ".join(t.get("name", "") for t in (hit.get("tags_skill") or []))
        experience = ", ".join(t.get("name", "") for t in (hit.get("tags_experience") or []))
        workload = ", ".join(t.get("name", "") for t in (hit.get("tags_workload") or []))
        if skills:
            meta.append(f"Skills: {skills}")
        if experience:
            meta.append(f"Experience: {experience}")
        if workload:
            meta.append(f"Workload: {workload}")
        if meta:
            full_description = full_description + "\n\n" + " | ".join(meta)

        compensation = hit.get("salary_text") or "" if hit.get("has_salary") else ""
        deadline = hit.get("closes_at") or ""

        external_id = hit.get("objectID") or hashlib.md5(f"{org}:{title}".encode()).hexdigest()[
            :12
        ]

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": department,
                "url": job_url,
                "external_id": external_id,
                "snippet": snippet,
                "full_description": full_description,
                "compensation": compensation,
                "deadline": deadline,
                "org_override": org,
                "org_url": org_url,
            }
        )

    print(f"  [{board_name}] Probably Good: {len(jobs)} relevant from {len(all_hits)} total")
    return jobs
