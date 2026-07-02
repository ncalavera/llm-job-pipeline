"""Recruitee public offers API (free, no auth)."""

import hashlib

from fetchers import http
from fetchers.html_utils import _html_to_snippet, _html_to_text
from fetchers.registry import company_fetcher, register_company


@company_fetcher
def fetch_recruitee(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Recruitee public API (free, no auth).
    Endpoint: GET https://{slug}.recruitee.com/api/offers/
    Returns full HTML descriptions, city/country, remote/hybrid, salary, department.
    """
    url = f"https://{slug}.recruitee.com/api/offers/"
    print(f"  [{org_name}] Recruitee API: {url}")
    resp = http.get(url, timeout=15)
    data = resp.json()
    jobs = []
    for j in data.get("offers", []):
        raw_desc = j.get("description", "") or ""
        full_desc = _html_to_text(raw_desc)
        snippet = _html_to_snippet(raw_desc) if raw_desc else ""

        # Build location string
        location = j.get("location", "") or ""
        if not location:
            parts = [j.get("city", ""), j.get("country", "")]
            location = ", ".join(p for p in parts if p)
        if j.get("remote"):
            location = f"Remote — {location}" if location else "Remote"
        elif j.get("hybrid"):
            location = f"Hybrid — {location}" if location else "Hybrid"

        # Salary
        salary = j.get("salary", {}) or {}
        compensation = ""
        if salary.get("min") or salary.get("max"):
            parts = []
            if salary.get("min"):
                parts.append(str(salary["min"]))
            if salary.get("max"):
                parts.append(str(salary["max"]))
            compensation = " – ".join(parts)
            if salary.get("currency"):
                compensation += f" {salary['currency']}"

        job_url = j.get("careers_url", "")
        jobs.append(
            {
                "title": j.get("title", ""),
                "location": location,
                "department": j.get("department", ""),
                "url": job_url,
                "external_id": str(j.get("id", ""))
                or hashlib.md5(f"{org_name}:{j.get('title', '')}".encode()).hexdigest()[:12],
                "snippet": snippet,
                "full_description": full_desc,
                "compensation": compensation,
            }
        )
    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


@register_company("recruitee")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_recruitee(org_name, config["slug"])
