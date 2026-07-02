"""UNOPS careers widget (official endpoint, no auth)."""

import hashlib
import html as html_module
import re
import time

from fetchers import http
from fetchers.html_utils import _html_to_text
from fetchers.http import FetchError
from fetchers.registry import company_fetcher, register_company


@company_fetcher
def fetch_unops_widget(
    org_name: str,
    url: str,
    *,
    title_blacklist: list[str] | None = None,
    seniority_filter: list[str] | None = None,
    location_keywords: list[str] | None = None,
    fetch_descriptions: bool = True,
) -> list[dict]:
    """Fetch UNOPS open positions from the official careers widget endpoint."""
    print(f"  [{org_name}] UNOPS widget: {url}")
    resp = http.get(url, timeout=20)
    html = resp.text

    article_pattern = re.compile(
        r'<article class="article article--card article--open[^"]*">(.+?)</article>',
        re.IGNORECASE | re.DOTALL,
    )
    link_pattern = re.compile(
        r'<a class="link" href="([^"]*?/JobDetail/[^"]+)"[^>]*>\s*([^<]+?)\s*</a>',
        re.IGNORECASE | re.DOTALL,
    )

    jobs = []
    seen_ids = set()
    skipped = {"expired": 0, "seniority": 0, "location": 0, "blacklist": 0}
    for block in article_pattern.findall(html):
        link_match = link_pattern.search(block)
        if not link_match:
            continue

        job_url = html_module.unescape(link_match.group(1).strip())
        if job_url.startswith("/"):
            job_url = "https://careers.unops.org" + job_url
        title = html_module.unescape(link_match.group(2).strip())

        duty_station = _extract_unops_widget_field(block, "Duty station(s)")
        seniority = _extract_unops_widget_field(block, "Seniority Level")
        deadline = _extract_unops_widget_field(block, "Post End date")

        ext_match = re.search(r"/(\d+)(?:$|[?#])", job_url)
        external_id = (
            ext_match.group(1) if ext_match else hashlib.md5(job_url.encode()).hexdigest()[:12]
        )
        if external_id in seen_ids:
            continue
        seen_ids.add(external_id)

        # Filter 0: title blacklist
        if title_blacklist:
            t_lower = title.lower()
            if any(kw in t_lower for kw in title_blacklist):
                skipped["blacklist"] += 1
                continue

        # Filter 1: skip expired jobs
        if deadline and _unops_deadline_expired(deadline):
            skipped["expired"] += 1
            continue

        # Filter 2: seniority filter (e.g. ["Mid-Level", "Senior"])
        if seniority_filter and seniority:
            if not any(f.lower() in seniority.lower() for f in seniority_filter):
                skipped["seniority"] += 1
                continue

        # Filter 3: location filter (e.g. ["Home-based", "Denmark", ...])
        if location_keywords and duty_station:
            if not any(kw.lower() in duty_station.lower() for kw in location_keywords):
                skipped["location"] += 1
                continue

        snippet_parts = []
        if duty_station:
            snippet_parts.append(f"Duty station: {duty_station}")
        if seniority:
            snippet_parts.append(f"Seniority: {seniority}")
        if deadline:
            snippet_parts.append(f"Deadline: {deadline}")
        snippet = " | ".join(snippet_parts)

        jobs.append(
            {
                "title": title,
                "location": duty_station,
                "department": seniority,
                "url": job_url,
                "external_id": external_id,
                "snippet": snippet,
                "deadline": deadline,
            }
        )

    total_skipped = sum(skipped.values())
    print(
        f"  [{org_name}] Found {len(jobs)} vacancies (skipped: {total_skipped} — "
        f"blacklist:{skipped['blacklist']}, expired:{skipped['expired']}, "
        f"seniority:{skipped['seniority']}, location:{skipped['location']})"
    )

    if fetch_descriptions and jobs:
        print(f"  [{org_name}] Fetching descriptions for {len(jobs)} vacancies...")
        for job in jobs:
            job["full_description"] = _fetch_unops_job_detail(job["url"])
            time.sleep(0.3)

    return jobs


def _extract_unops_widget_field(block_html: str, label: str) -> str:
    """Extract a text field from a UNOPS job card block."""
    pattern = re.compile(
        rf"<strong>\s*{re.escape(label)}\s*:</strong>\s*([^<\n]+)",
        re.IGNORECASE,
    )
    match = pattern.search(block_html)
    return html_module.unescape(match.group(1).strip()) if match else ""


def _parse_unops_deadline(date_str: str):
    """Parse UNOPS deadline string into a date object. Returns None on failure."""
    from datetime import datetime

    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _unops_deadline_expired(date_str: str) -> bool:
    """Return True if the deadline has already passed."""
    from datetime import date

    parsed = _parse_unops_deadline(date_str)
    if parsed is None:
        return False  # unknown format → keep the vacancy
    return parsed < date.today()


def _fetch_unops_job_detail(url: str) -> str:
    """Fetch a UNOPS job detail page and return clean description text."""
    try:
        resp = http.get(url, timeout=20)
    except FetchError:
        return ""
    # Gone jobs 302-redirect to /careersmarketplace/Error — without this
    # guard the error page text would be saved as a full_description.
    if "/error" in resp.url.lower():
        return ""
    html = resp.text

    # UNOPS uses a Twig template with class="section__content" for job details
    # This pattern captures the main job content section
    m = re.search(
        r'<div[^>]+class="section__content"[^>]*>(.*?)</section>', html, re.IGNORECASE | re.DOTALL
    )
    if m and len(m.group(1)) > 200:
        return _html_to_text(m.group(1))

    # Fallback: strip full <body> text
    body = re.search(r"<body[^>]*>(.*?)</body>", html, re.IGNORECASE | re.DOTALL)
    return _html_to_text(body.group(1)) if body else ""


@register_company("unops_widget")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_unops_widget(
        org_name,
        config["url"],
        title_blacklist=config.get("title_blacklist"),
        seniority_filter=config.get("seniority_filter"),
        location_keywords=config.get("location_keywords"),
        fetch_descriptions=config.get("fetch_descriptions", True),
    )
