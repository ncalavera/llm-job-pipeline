"""Workable public widget API (free, no auth)."""

import hashlib

from fetchers import http
from fetchers.firecrawl import _enrich_blind_jobs
from fetchers.html_utils import _html_to_multiline
from fetchers.http import FetchError
from fetchers.registry import company_fetcher, register_company


def workable_posting_text(slug: str, shortcode: str) -> str:
    """One posting's body from the public job API (free, no auth): the
    description, requirements and benefits HTML as text. Raises FetchError."""
    url = f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}"
    data = http.get(url, timeout=15).json()
    return _html_to_multiline(
        "\n".join(data.get(k) or "" for k in ("description", "requirements", "benefits"))
    )


@company_fetcher
def fetch_workable(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Workable widget API (free, no auth).
    Endpoint: GET https://apply.workable.com/api/v1/widget/accounts/{slug}
    """
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    print(f"  [{org_name}] Workable API: {url}")
    resp = http.get(url, timeout=15)
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location", {})
        location_parts = [loc.get("city", ""), loc.get("country", "")]
        location = ", ".join(p for p in location_parts if p)
        if j.get("remote"):
            location = f"Remote — {location}" if location else "Remote"
        shortcode = j.get("shortcode", "")
        try:
            body = workable_posting_text(slug, shortcode) if shortcode else ""
        except (FetchError, ValueError) as e:  # ValueError: not JSON
            print(f"  [{org_name}] Workable job API failed for {shortcode}: {e!r}")
            body = ""
        job_url = j.get("url", "") or f"https://apply.workable.com/{slug}/j/{shortcode}/"
        jobs.append(
            {
                "title": j.get("title", ""),
                "location": location,
                "department": j.get("department", ""),
                "url": job_url,
                "external_id": shortcode or hashlib.md5(job_url.encode()).hexdigest()[:12],
                "snippet": j.get("shortDescription", ""),
                "full_description": body,
            }
        )
    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    # The list API only returns shortDescription; the body comes from the job
    # API above. Firecrawl only for a job whose API call failed.
    return _enrich_blind_jobs(jobs, org_name)


@register_company("workable")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_workable(org_name, config["slug"])
