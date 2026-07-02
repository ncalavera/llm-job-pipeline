"""Idealist board (Algolia search-only key embedded in the page — free, no auth)."""

import hashlib
import json

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_multiline, _html_to_snippet
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher

_IDEALIST_APP_ID = "NSV3AUESS7"
# Search-only public key baked into idealist.org's page HTML. If it 403s, refetch
# it from the page: curl https://www.idealist.org/en/jobs | grep searchApiKey
_IDEALIST_SEARCH_KEY = "c2730ea10ab82787f2f3cc961e8c1e06"
_IDEALIST_INDEX = "idealist7-production-published-desc"  # newest-first
_IDEALIST_SITE = "https://www.idealist.org"


@board_fetcher("idealist_algolia")
def fetch_idealist_board(board_cfg: dict) -> list[dict]:
    """Fetch nonprofit/impact jobs from Idealist via its embedded Algolia key.

    Idealist's React SPA bakes the Algolia appId + search-only key into the page
    HTML — no login, no paid API. POSTs to the Algolia query endpoint.

    board_cfg knobs: ``remote_zone`` (default "WORLD" = globally-open remote),
    ``include_onsite`` (default False), ``max_pages`` (default 20).
    """
    board_name = board_cfg["name"]
    board_blacklist = board_cfg.get("board_blacklist", [])
    max_pages = int(board_cfg.get("max_pages", 20))
    remote_zone = board_cfg.get("remote_zone", "WORLD")
    include_onsite = bool(board_cfg.get("include_onsite", False))

    algolia_url = f"https://{_IDEALIST_APP_ID}-dsn.algolia.net/1/indexes/{_IDEALIST_INDEX}/query"
    headers = {
        "X-Algolia-Application-Id": _IDEALIST_APP_ID,
        "X-Algolia-API-Key": _IDEALIST_SEARCH_KEY,
        "Content-Type": "application/json",
    }
    facet = [["type:JOB"]]
    if not include_onsite:
        facet.append(["locationType:REMOTE"])
        if remote_zone:
            facet.append([f"remoteZone:{remote_zone}"])

    print(
        f"  [{board_name}] Idealist/Algolia: up to {max_pages} pages "
        f"(remote_zone={remote_zone!r}, include_onsite={include_onsite})..."
    )

    all_hits: list[dict] = []
    last_error = None
    for page in range(max_pages):
        params_str = "&".join(
            [
                "query=",
                "hitsPerPage=200",
                f"page={page}",
                f"facetFilters={json.dumps(facet)}",
            ]
        )
        try:
            resp = http.post(
                algolia_url,
                data=json.dumps({"params": params_str}),
                headers=headers,
                timeout=20,
            )
            data = resp.json()
        except Exception as exc:
            print(f"  [{board_name}] Algolia ERROR page {page}: {exc}")
            last_error = exc
            break
        hits = data.get("hits") or []
        if not hits:
            break
        all_hits.extend(hits)
        if page >= int(data.get("nbPages", 1)) - 1:
            break

    if not all_hits and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

    total_seen = len(all_hits)
    for h in all_hits:
        h["_title_proxy"] = h.get("name", "")
    filtered = _blacklist_filter(
        all_hits,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["_title_proxy"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs: list[dict] = []
    for hit in filtered:
        title = (hit.get("name") or "").strip()
        if not title or _is_generic_pipeline_title(title):
            continue
        org = (hit.get("orgName") or "").strip()

        slug = (hit.get("url") or {}).get("en") or ""
        job_url = (_IDEALIST_SITE + slug) if slug else ""

        loc_type = hit.get("locationType") or ""
        zone = hit.get("remoteZone") or ""
        country = hit.get("country") or ""
        city = hit.get("city") or ""
        if loc_type == "REMOTE":
            if zone == "WORLD":
                location = "Remote (worldwide)"
            elif zone == "COUNTRY" and country:
                location = f"Remote ({country})"
            else:
                location = "Remote"
        else:
            location = ", ".join(p for p in (city, country) if p) or loc_type

        functions = hit.get("functions") or []
        keywords = hit.get("keywords") or []
        department = ", ".join((keywords or functions)[:3])

        raw_desc = hit.get("description") or ""
        snippet = _html_to_snippet(raw_desc)
        full_description = _html_to_multiline(raw_desc)
        meta = []
        if hit.get("areasOfFocus"):
            meta.append(f"Areas of focus: {', '.join(hit['areasOfFocus'][:5])}")
        if functions:
            meta.append(f"Functions: {', '.join(functions)}")
        level = hit.get("professionalLevel") or ""
        if level and level != "NONE":
            meta.append(f"Level: {level}")
        if meta:
            full_description = full_description + "\n\n" + " | ".join(meta)

        def _as_num(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        sal_min, sal_max = _as_num(hit.get("salaryMinimum")), _as_num(hit.get("salaryMaximum"))
        currency = hit.get("salaryCurrency") or "USD"
        period = (hit.get("salaryPeriod") or "YEAR").lower()
        compensation = ""
        if sal_min and sal_max:
            compensation = (
                f"{currency} {sal_min:,.0f}/{period}"
                if sal_min == sal_max
                else f"{currency} {sal_min:,.0f}-{sal_max:,.0f}/{period}"
            )

        org_slug = (hit.get("orgUrl") or {}).get("en") or ""
        org_url = (_IDEALIST_SITE + org_slug) if org_slug else board_cfg["url"]

        external_id = hit.get("objectID") or hashlib.md5(f"{org}:{title}".encode()).hexdigest()[:12]

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
                "org_override": org,
                "org_url": org_url,
            }
        )

    print(f"  [{board_name}] Idealist: {len(jobs)} relevant from {total_seen} total")
    return jobs
