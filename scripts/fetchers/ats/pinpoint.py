"""Pinpoint public postings API (free, no auth)."""

import hashlib

from fetchers import http
from fetchers.html_utils import _html_to_snippet, _html_to_text
from fetchers.registry import company_fetcher, register_company


def _pinpoint_location(job: dict) -> str:
    """Build a human-readable location, prefixing the workplace type.

    Pinpoint carries a structured ``location`` dict (city/name/province) plus a
    ``workplace_type_text`` ("Remote"/"Hybrid"/"On-site"). Name and province are
    often identical (e.g. "London", "London"), so dedupe before joining.
    """
    loc = job.get("location") or {}
    seen: list[str] = []
    for part in (loc.get("name") or loc.get("city") or "", loc.get("province") or ""):
        part = (part or "").strip()
        if part and part not in seen:
            seen.append(part)
    location = ", ".join(seen)

    workplace = (job.get("workplace_type_text") or "").strip()
    if workplace and workplace.lower() not in ("on-site", "onsite", "on site"):
        location = f"{workplace} — {location}" if location else workplace
    return location


@company_fetcher
def fetch_pinpoint(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from the Pinpoint public postings feed (free, no auth).
    Endpoint: GET https://{slug}.pinpointhq.com/postings.json
    Returns HTML descriptions, structured location, department, compensation.
    No pagination — the feed is a single ``data`` array of all live postings.
    """
    url = f"https://{slug}.pinpointhq.com/postings.json"
    print(f"  [{org_name}] Pinpoint API: {url}")
    resp = http.get(url, timeout=15)
    data = resp.json() or {}
    jobs = []
    for j in data.get("data", []):
        raw_desc = j.get("description", "") or ""
        full_desc = _html_to_text(raw_desc)
        snippet = _html_to_snippet(raw_desc) if raw_desc else ""

        department = ((j.get("job") or {}).get("department") or {}).get("name", "") or ""

        job_url = j.get("url", "") or ""
        external_id = (
            str(j.get("id", ""))
            or (hashlib.md5(f"{org_name}:{j.get('title', '')}".encode()).hexdigest()[:12])
        )

        # `compensation` is a pre-formatted string ("£70,000 - £105,000 / year"),
        # shown only when the posting opts into displaying it.
        compensation = (j.get("compensation", "") or "") if j.get("compensation_visible") else ""

        jobs.append(
            {
                "title": j.get("title", ""),
                "location": _pinpoint_location(j),
                "department": department,
                "url": job_url,
                "external_id": external_id,
                "snippet": snippet,
                "full_description": full_desc,
                "compensation": compensation,
            }
        )
    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


@register_company("pinpoint")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_pinpoint(org_name, config["slug"])
