"""Workday public JSON API (undocumented, free) — two-phase list + detail."""

import hashlib
import time

from fetchers import http
from fetchers.html_utils import _html_to_snippet, _html_to_text
from fetchers.registry import company_fetcher, register_company

_WORKDAY_DETAIL_RATE_LIMIT = 0.25  # seconds between per-job detail requests


@company_fetcher
def fetch_workday_api(
    org_name: str, tenant: str, board: str, base_url: str, config: dict | None = None
) -> list[dict]:
    """Fetch jobs from Workday's undocumented but public JSON API.

    Two-phase fetch:
      Phase 1 — POST /jobs: paginated listing (metadata only, no descriptions)
      Phase 2 — GET /wday/cxs/{tenant}/{board}{ext_path}: per-job detail with full description

    Args:
        tenant: subdomain owner, e.g. "gatesfoundation"
        board:  board name in URL path, e.g. "Gates"
        base_url: full base URL e.g. "https://gatesfoundation.wd1.myworkdayjobs.com"
    """
    list_url = f"{base_url}/wday/cxs/{tenant}/{board}/jobs"
    url_prefix = (config or {}).get("url_prefix", "")
    search_text = (config or {}).get("search_text", "")
    print(f"  [{org_name}] Workday API: {list_url}")

    jobs = []
    offset = 0
    limit = 20  # Workday API rejects limits > 20
    total_known = None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

    # ── Phase 1: Listing ─────────────────────────────────────────────────
    while True:
        payload = {
            "appliedFacets": {},
            "limit": limit,
            "offset": offset,
            "searchText": search_text,
        }
        resp = http.post(list_url, json=payload, headers=headers, timeout=20)
        data = resp.json()

        postings = data.get("jobPostings", [])
        if total_known is None:
            total_known = data.get("total", 0)

        for j in postings:
            ext_path = j.get("externalPath", "")
            if ext_path:
                # Workday API sometimes omits the board name from externalPath,
                # returning "/job/..." instead of "/en-US/{board}/job/...".
                # URLs without the board 404, so we prepend it when missing.
                if url_prefix:
                    job_url = f"{base_url}/{url_prefix}{ext_path}"
                elif ext_path.startswith("/job/"):
                    job_url = f"{base_url}/{board}{ext_path}"
                else:
                    job_url = f"{base_url}{ext_path}"
                job_url = job_url.replace("//", "/").replace(":/", "://")
            else:
                job_url = ""
            jobs.append(
                {
                    "title": j.get("title", ""),
                    "location": j.get("locationsText", ""),
                    "department": "",
                    "url": job_url,
                    "external_id": hashlib.md5(
                        job_url.encode() if job_url else f"{org_name}:{j.get('title', '')}".encode()
                    ).hexdigest()[:12],
                    "snippet": "",
                    "_ext_path": ext_path,  # temp: used in phase 2, removed before return
                }
            )

        offset += len(postings)
        if not postings or offset >= (total_known or 0):
            break

    print(f"  [{org_name}] Found {len(jobs)} vacancies, fetching descriptions...")

    # ── Phase 2: Per-job descriptions ────────────────────────────────────
    fetched_desc = 0
    for i, job in enumerate(jobs):
        ext_path = job.pop("_ext_path", "")
        if not ext_path:
            continue
        detail_url = f"{base_url}/wday/cxs/{tenant}/{board}{ext_path}"
        try:
            resp = http.get(
                detail_url,
                headers={"Accept": "application/json", "User-Agent": headers["User-Agent"]},
                timeout=10,
            )
            detail = resp.json()
            html_desc = detail.get("jobPostingInfo", {}).get("jobDescription", "") or ""
            if html_desc:
                job["full_description"] = _html_to_text(html_desc)
                job["snippet"] = _html_to_snippet(html_desc)
                fetched_desc += 1
        except Exception:
            pass  # description stays empty; LLM will score by title only

        if (i + 1) % 20 == 0:
            print(f"  [{org_name}] Descriptions: {i + 1}/{len(jobs)} ({fetched_desc} ok)")
        time.sleep(_WORKDAY_DETAIL_RATE_LIMIT)

    print(f"  [{org_name}] Descriptions: {fetched_desc}/{len(jobs)} fetched")
    return jobs


@register_company("workday_api")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_workday_api(
        org_name, config["tenant"], config["board"], config["base_url"], config
    )
