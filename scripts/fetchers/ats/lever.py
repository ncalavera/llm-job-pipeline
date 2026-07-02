"""Lever public postings API (free, no auth)."""

import hashlib

from fetchers import http
from fetchers.registry import company_fetcher, register_company


@company_fetcher
def fetch_lever(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Lever public API (free, no auth).
    Endpoint: GET https://api.lever.co/v0/postings/{slug}?mode=json
    """
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    print(f"  [{org_name}] Lever API: {url}")
    resp = http.get(url, timeout=15)
    data = resp.json()  # Lever returns a flat JSON array
    jobs = []
    for j in data:
        categories = j.get("categories", {})
        raw_desc = j.get("descriptionPlain", "") or ""
        snippet = raw_desc[:400].rsplit(" ", 1)[0] + "…" if len(raw_desc) > 400 else raw_desc
        jobs.append(
            {
                "title": j.get("text", ""),
                "location": categories.get("location", ""),
                "department": categories.get("team", ""),
                "url": j.get("hostedUrl", ""),
                "external_id": j.get("id", "")
                or hashlib.md5(f"{org_name}:{j.get('text', '')}".encode()).hexdigest()[:12],
                "snippet": snippet,
                "full_description": raw_desc,
            }
        )
    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


@register_company("lever")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_lever(org_name, config["slug"])
