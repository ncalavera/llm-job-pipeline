"""Fast Forward board (Getro-hosted tech-nonprofit board — free, no auth)."""

import hashlib

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_multiline, _html_to_snippet
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher

_GETRO_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@board_fetcher("fastforward_board")
def fetch_fastforward_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from Fast Forward's tech-nonprofit board (jobs.ffwd.org).

    Platform is Getro (collection 997). Two free, unauthenticated endpoints:
      * POST api.getro.com/api/v2/collections/997/search/jobs  (paginated list)
      * GET  api.getro.com/api/v1/jobs/{slug}?collection_id=997 (full HTML desc)

    ``fetch_descriptions`` defaults False (listing-only, like Impactpool) so the
    run does not make ~1.5k per-job requests; the enrich pass fills descriptions.
    """
    network_id = int(board_cfg.get("getro_collection_id", 997))
    search_url = f"https://api.getro.com/api/v2/collections/{network_id}/search/jobs"
    detail_tpl = f"https://api.getro.com/api/v1/jobs/{{slug}}?collection_id={network_id}"
    max_pages = int(board_cfg.get("max_pages", 20))
    fetch_desc = bool(board_cfg.get("fetch_descriptions", False))

    board_name = board_cfg["name"]
    board_blacklist = board_cfg.get("board_blacklist", [])
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _GETRO_UA,
    }

    raw_listings: list[dict] = []
    seen_ids: set = set()
    last_error = None
    for page in range(max_pages):
        payload = {"hits_per_page": 100, "page": page, "filters": "", "query": ""}
        try:
            resp = http.post(search_url, json=payload, headers=headers, timeout=20)
            data = resp.json()
        except Exception as exc:
            print(f"  [{board_name}] page {page} ERROR: {exc}")
            last_error = exc
            break
        batch = (data.get("results") or {}).get("jobs") or []
        if not batch:
            break
        for j in batch:
            jid = j.get("id")
            if jid is None or jid in seen_ids:
                continue
            seen_ids.add(jid)
            raw_listings.append(j)
        if len(seen_ids) >= int((data.get("results") or {}).get("count", 0)):
            break

    if not raw_listings and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

    filtered = _blacklist_filter(
        raw_listings,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["title"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs: list[dict] = []
    for j in filtered:
        title = (j.get("title") or "").strip()
        org = ((j.get("organization") or {}).get("name") or "").strip()
        if not title or not org or _is_generic_pipeline_title(title):
            continue

        # Getro lists the literal "Remote" as a pseudo-location; drop it and
        # keep the first real place name so remote roles keep their city.
        locs = j.get("locations") or []
        real_locs = [l for l in locs if l and l.lower() != "remote"]
        location = real_locs[0] if real_locs else ""
        if j.get("work_mode") == "remote":
            location = f"Remote, {location}" if location else "Remote"

        comp_min = j.get("compensation_amount_min_cents")
        comp_max = j.get("compensation_amount_max_cents")
        currency = j.get("compensation_currency") or "USD"
        period = j.get("compensation_period") or ""
        compensation = ""
        if j.get("compensation_public") and comp_min and comp_max:
            label = {"year": "/yr", "month": "/mo", "hour": "/hr"}.get(period, f"/{period}")
            compensation = f"{currency} {comp_min / 100:,.0f}-{comp_max / 100:,.0f}{label}"

        slug = (j.get("slug") or "").strip()
        snippet = full_description = ""
        if fetch_desc and j.get("has_description") and slug:
            try:
                dresp = http.get(
                    detail_tpl.format(slug=slug),
                    headers={"Accept": "application/json", "User-Agent": _GETRO_UA},
                    timeout=15,
                    check=False,
                )
                if dresp.status_code == 200:
                    desc_html = (dresp.json() or {}).get("description") or ""
                    full_description = _html_to_multiline(desc_html)
                    snippet = _html_to_snippet(desc_html)
            except Exception:
                pass

        org_slug = (j.get("organization") or {}).get("slug") or ""
        org_url = f"https://jobs.ffwd.org/companies/{org_slug}" if org_slug else board_cfg["url"]
        jid = j.get("id")
        external_id = str(jid) if jid else hashlib.md5(f"{org}:{title}".encode()).hexdigest()[:12]

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": "",
                "url": (j.get("url") or "").strip(),
                "external_id": external_id,
                "snippet": snippet,
                "full_description": full_description,
                "compensation": compensation,
                "org_override": org,
                "org_url": org_url,
            }
        )

    print(
        f"  [{board_name}] Fast Forward/Getro: {len(jobs)} relevant from {len(raw_listings)} total"
    )
    return jobs
