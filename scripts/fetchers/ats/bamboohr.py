"""BambooHR public careers API (free, no auth) — two-phase list + detail."""

import time

from fetchers import http
from fetchers.html_utils import _html_to_snippet, _html_to_text
from fetchers.http import FetchError
from fetchers.registry import company_fetcher, register_company

_BAMBOOHR_DETAIL_RATE_LIMIT = 0.3  # seconds between detail requests


@company_fetcher
def fetch_bamboohr(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from BambooHR public careers API (free, no auth).
    Two-phase: list at /careers/list, then detail at /careers/{id}/detail.
    """
    list_url = f"https://{slug}.bamboohr.com/careers/list"
    print(f"  [{org_name}] BambooHR API: {list_url}")
    resp = http.get(
        list_url,
        headers={"Accept": "application/json"},
        timeout=15,
        allow_redirects=False,
        check=False,
    )
    if resp.status_code in (301, 302, 303, 307, 308):
        print(
            f"  [{org_name}] ERROR: BambooHR redirected to {resp.headers.get('Location', '?')} — "
            f"account likely disabled, update fetch_strategy in Supabase"
        )
        return []
    if resp.status_code >= 400:
        raise FetchError(f"http_{resp.status_code}", f"BambooHR list returned {resp.status_code}")
    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype.lower():
        print(
            f"  [{org_name}] ERROR: BambooHR returned {ctype or 'unknown content-type'} "
            f"(expected JSON), status={resp.status_code}"
        )
        return []
    data = resp.json()
    postings = data.get("result", [])
    print(f"  [{org_name}] Found {len(postings)} vacancies, fetching descriptions...")

    jobs = []
    for i, p in enumerate(postings):
        job_id = p.get("id", "")
        title = p.get("jobOpeningName", "")
        loc = p.get("location", {})
        location_parts = [loc.get("city", ""), loc.get("state", "")]
        location = ", ".join(pt for pt in location_parts if pt)
        if p.get("isRemote"):
            location = f"Remote — {location}" if location else "Remote"

        department = p.get("departmentLabel", "")
        job_url = f"https://{slug}.bamboohr.com/careers/{job_id}"

        # Phase 2: fetch full description
        full_desc = ""
        snippet = ""
        compensation = ""
        if job_id:
            try:
                detail_url = f"https://{slug}.bamboohr.com/careers/{job_id}/detail"
                dr = http.get(detail_url, headers={"Accept": "application/json"}, timeout=15)
                detail = dr.json().get("result", {}).get("jobOpening", {})
                raw_desc = detail.get("description", "") or ""
                full_desc = _html_to_text(raw_desc)
                snippet = _html_to_snippet(raw_desc) if raw_desc else ""
                compensation = detail.get("compensation", "") or ""
            except Exception as e:
                print(f"  [{org_name}] Detail fetch failed for {title[:40]}: {e}")
            if i < len(postings) - 1:
                time.sleep(_BAMBOOHR_DETAIL_RATE_LIMIT)

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": department,
                "url": job_url,
                "external_id": str(job_id),
                "snippet": snippet,
                "full_description": full_desc,
                "compensation": compensation,
            }
        )

    print(f"  [{org_name}] {len(jobs)} vacancies with descriptions")
    return jobs


@register_company("bamboohr")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_bamboohr(org_name, config["slug"])
