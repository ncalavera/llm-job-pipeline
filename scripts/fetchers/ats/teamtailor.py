"""Teamtailor RSS feed (free, no auth)."""

import hashlib

from fetchers import http
from fetchers.html_utils import _html_to_snippet, _html_to_text
from fetchers.registry import company_fetcher, register_company


@company_fetcher
def fetch_teamtailor_rss(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Teamtailor RSS feed (free, no auth).
    Feed URL: https://{slug}.teamtailor.com/jobs.rss
    Returns full HTML descriptions, locations, departments.
    """
    import xml.etree.ElementTree as ET

    url = f"https://{slug}.teamtailor.com/jobs.rss"
    print(f"  [{org_name}] Teamtailor RSS: {url}")
    resp = http.get(url, timeout=15)

    root = ET.fromstring(resp.text)
    # RSS 2.0: channel > item
    channel = root.find("channel")
    if channel is None:
        print(f"  [{org_name}] No channel found in RSS")
        return []

    jobs = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        raw_desc = item.findtext("description") or ""
        full_desc = _html_to_text(raw_desc)
        snippet = _html_to_snippet(raw_desc) if raw_desc else ""

        # Teamtailor uses <category> for department and custom namespaced tags
        department = (item.findtext("category") or "").strip()

        # Location: Teamtailor uses tt:locations > tt:location > tt:city/tt:country
        location = ""
        tt_ns = "https://teamtailor.com/locations"
        loc_container = item.find(f"{{{tt_ns}}}locations")
        if loc_container is not None:
            loc_el = loc_container.find(f"{{{tt_ns}}}location")
            if loc_el is not None:
                city = (loc_el.findtext(f"{{{tt_ns}}}city") or "").strip()
                country = (loc_el.findtext(f"{{{tt_ns}}}country") or "").strip()
                location = ", ".join(p for p in [city, country] if p)
        # Fallback: check for generic namespaced location element
        if not location:
            for child in item:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "location":
                    location = (child.text or "").strip()

        # Remote status from <remoteStatus> tag
        remote_status = (item.findtext("remoteStatus") or "").strip().lower()
        if remote_status == "fully":
            location = f"Remote — {location}" if location else "Remote"
        elif remote_status == "hybrid":
            location = f"Hybrid — {location}" if location else "Hybrid"

        # Department from tt:department
        tt_dept = item.findtext(f"{{{tt_ns}}}department")
        if tt_dept:
            department = tt_dept.strip()

        # Stable external_id from link URL
        external_id = (
            hashlib.md5(link.encode()).hexdigest()[:12]
            if link
            else hashlib.md5(f"{org_name}:{title}".encode()).hexdigest()[:12]
        )

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": department,
                "url": link,
                "external_id": external_id,
                "snippet": snippet,
                "full_description": full_desc,
            }
        )

    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


@register_company("teamtailor_rss")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_teamtailor_rss(org_name, config["slug"])
