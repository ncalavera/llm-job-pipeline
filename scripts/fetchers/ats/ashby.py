"""Ashby public job-board API (free, no auth)."""

import hashlib

from fetchers import http
from fetchers.html_utils import _html_to_snippet, _html_to_text
from fetchers.registry import company_fetcher, register_company


@company_fetcher
def fetch_ashby(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Ashby public API (free, no auth).
    Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    print(f"  [{org_name}] Ashby API: {url}")
    resp = http.get(url, timeout=15)
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        raw_desc = j.get("descriptionPlain", "") or ""
        html_desc = j.get("descriptionHtml", "") or ""
        full_desc = raw_desc or _html_to_text(html_desc)
        snippet = (
            _html_to_snippet(html_desc)
            if html_desc
            else (raw_desc[:400].rsplit(" ", 1)[0] + "…" if len(raw_desc) > 400 else raw_desc)
        )
        job_url = j.get("jobUrl", "")
        jobs.append(
            {
                "title": j.get("title", ""),
                "location": j.get("location", ""),
                "department": j.get("department", "") or j.get("team", ""),
                "url": job_url,
                "external_id": hashlib.md5(job_url.encode()).hexdigest()[:12]
                if job_url
                else hashlib.md5(f"{org_name}:{j.get('title', '')}".encode()).hexdigest()[:12],
                "snippet": snippet,
                "full_description": full_desc,
                "compensation": j.get("compensationTierSummary", ""),
            }
        )
    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


@register_company("ashby")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_ashby(org_name, config["slug"])
