"""Teamtailor RSS feed (free, no auth)."""

import hashlib
import urllib.parse
import xml.etree.ElementTree as ET

from fetchers import http
from fetchers.html_utils import _html_to_snippet, _html_to_text
from fetchers.http import FetchError
from fetchers.registry import company_fetcher, register_company


def _teamtailor_hosts(slug: str, careers_url: str = "") -> list[str]:
    """Candidate RSS hosts, custom domain first: some Teamtailor customers
    (e.g. Chatham House) publish their feed on a custom career-site domain
    (``careers.chathamhouse.org``) rather than ``<slug>.teamtailor.com``.
    Falls back to the default host so unconfigured companies are unaffected.
    """
    default = f"{slug}.teamtailor.com"
    hosts = []
    if careers_url:
        netloc = urllib.parse.urlparse(careers_url).netloc
        if netloc and netloc != default:
            hosts.append(netloc)
    hosts.append(default)
    return hosts


def _parse_teamtailor_feed(text: str) -> ET.Element | None:
    """Return the parsed root iff ``text`` is a genuine RSS/Atom feed.

    A custom career-site domain can answer ``/jobs.rss`` with HTTP 200 and an
    SPA/marketing HTML page instead of the feed. That HTML must NOT be mistaken
    for an empty feed (which would stop the host fallback dead), so anything
    that fails to parse as XML or lacks an ``<rss>``/``<feed>`` root is rejected
    → the caller tries the next host. A valid but empty feed IS accepted: a real
    Teamtailor org with zero openings must not be misread as a failure.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    tag = root.tag.split("}")[-1].lower()
    return root if tag in ("rss", "feed") else None


@company_fetcher
def fetch_teamtailor_rss(org_name: str, slug: str, careers_url: str = "") -> list[dict]:
    """Fetch jobs from Teamtailor RSS feed (free, no auth).
    Feed URL: https://{host}/jobs.rss, where {host} is either a configured
    custom domain (``careers_url``) or the default ``{slug}.teamtailor.com``.
    Returns full HTML descriptions, locations, departments.
    """
    hosts = _teamtailor_hosts(slug, careers_url)
    root = None
    for i, host in enumerate(hosts):
        url = f"https://{host}/jobs.rss"
        print(f"  [{org_name}] Teamtailor RSS: {url}")
        last_host = i == len(hosts) - 1
        try:
            resp = http.get(url, timeout=15)
        except FetchError as exc:
            if last_host:
                raise
            print(f"  [{org_name}] {host} failed ({exc.reason}); trying next host")
            continue
        # A 200 alone is not enough: the custom host must actually serve a feed,
        # not a career-site landing page.
        root = _parse_teamtailor_feed(resp.text)
        if root is not None:
            break
        if last_host:
            raise FetchError(
                "not_a_feed", f"{url} returned a non-feed response (career-site page?)"
            )
        print(f"  [{org_name}] {host} returned non-feed content; trying next host")

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
    return fetch_teamtailor_rss(org_name, config["slug"], careers_url=config.get("careers_url", ""))
