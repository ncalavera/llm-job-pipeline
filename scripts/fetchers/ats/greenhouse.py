"""Greenhouse public boards API (free, no credits)."""

from fetchers import http
from fetchers.html_utils import (
    _extract_compensation,
    _extract_deadline,
    _html_to_snippet,
    _html_to_text,
)
from fetchers.registry import company_fetcher, register_company


@company_fetcher
def fetch_greenhouse(org_name: str, slug: str, *, eu: bool = False) -> list[dict]:
    """Fetch jobs from Greenhouse public API (free, no credits).
    Uses ?content=true to get the job description snippet.
    Set eu=True for companies hosted on the EU Greenhouse instance.
    """
    host = "boards-api.eu.greenhouse.io" if eu else "boards-api.greenhouse.io"
    url = f"https://{host}/v1/boards/{slug}/jobs?content=true"
    print(f"  [{org_name}] Greenhouse API: {url}")
    resp = http.get(url, timeout=15)
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        raw_content = j.get("content", "") or ""
        jobs.append(
            {
                "title": j.get("title", ""),
                "location": j.get("location", {}).get("name", ""),
                "department": (
                    j.get("departments", [{}])[0].get("name", "") if j.get("departments") else ""
                ),
                "url": j.get("absolute_url", ""),
                "external_id": str(j.get("id", "")),
                "snippet": _html_to_snippet(raw_content),
                "full_description": _html_to_text(raw_content),
                "compensation": _extract_compensation(raw_content),
                "deadline": _extract_deadline(raw_content),
            }
        )
    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


@register_company("greenhouse")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_greenhouse(org_name, config["slug"], eu=config.get("eu", False))
