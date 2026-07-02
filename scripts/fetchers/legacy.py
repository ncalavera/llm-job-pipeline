"""Vacancy fetchers: Greenhouse API, Workday API, Firecrawl scraper, markdown parser."""

import hashlib
import html as html_module
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request

import fetchers as _pkg
from fetchers.http import _LOCAL_UA
from fetchers.html_utils import (
    _absolutize_links,
    _extract_compensation,
    _extract_deadline,
    _html_to_markdown,
    _html_to_multiline,
    _html_to_snippet,
    _html_to_text,
)
from fetchers.parsing import (
    _blacklist_filter,
    _extract_org_from_listing,
    _is_generic_pipeline_title,
    _parse_json_jobs,
    parse_markdown_jobs,
)
from config import FIRECRAWL_CACHE, GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR


# ---------------------------------------------------------------------------
# Greenhouse API
# ---------------------------------------------------------------------------


def fetch_greenhouse(org_name: str, slug: str, *, eu: bool = False) -> list[dict]:
    """Fetch jobs from Greenhouse public API (free, no credits).
    Uses ?content=true to get the job description snippet.
    Set eu=True for companies hosted on the EU Greenhouse instance.
    """
    host = "boards-api.eu.greenhouse.io" if eu else "boards-api.greenhouse.io"
    url = f"https://{host}/v1/boards/{slug}/jobs?content=true"
    print(f"  [{org_name}] Greenhouse API: {url}")
    try:
        resp = _pkg.requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for j in data.get("jobs", []):
            raw_content = j.get("content", "") or ""
            snippet = _html_to_snippet(raw_content)
            full_desc = _html_to_text(raw_content)
            jobs.append(
                {
                    "title": j.get("title", ""),
                    "location": j.get("location", {}).get("name", ""),
                    "department": (
                        j.get("departments", [{}])[0].get("name", "")
                        if j.get("departments")
                        else ""
                    ),
                    "url": j.get("absolute_url", ""),
                    "external_id": str(j.get("id", "")),
                    "snippet": snippet,
                    "full_description": full_desc,
                    "compensation": _extract_compensation(raw_content),
                    "deadline": _extract_deadline(raw_content),
                }
            )
        print(f"  [{org_name}] Found {len(jobs)} vacancies")
        return jobs
    except Exception as e:
        print(f"  [{org_name}] ERROR: {e}")
        return []


# ---------------------------------------------------------------------------
# Workday public JSON API
# ---------------------------------------------------------------------------

_WORKDAY_DETAIL_RATE_LIMIT = 0.25  # seconds between per-job detail requests


def fetch_workday_api(
    org_name: str, tenant: str, board: str, base_url: str, config: dict | None = None
) -> list[dict]:
    """Fetch jobs from Workday's undocumented but public JSON API.

    Two-phase fetch:
      Phase 1 — POST /jobs: paginated listing (metadata only, no descriptions)
      Phase 2 — GET /wday/cxs/{tenant}/{board}{ext_path}: per-job detail with full description

    Args:
        tenant: subdomain owner, e.g. "gatesfoundation"
        board:  board name in URL path, e.g. "Gates"
        base_url: full base URL e.g. "https://gatesfoundation.wd1.myworkdayjobs.com"
    """
    list_url = f"{base_url}/wday/cxs/{tenant}/{board}/jobs"
    url_prefix = (config or {}).get("url_prefix", "")
    search_text = (config or {}).get("search_text", "")
    print(f"  [{org_name}] Workday API: {list_url}")

    jobs = []
    offset = 0
    limit = 20  # Workday API rejects limits > 20
    total_known = None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

    try:
        # ── Phase 1: Listing ─────────────────────────────────────────────────
        while True:
            payload = json.dumps(
                {
                    "appliedFacets": {},
                    "limit": limit,
                    "offset": offset,
                    "searchText": search_text,
                }
            ).encode()
            req = urllib.request.Request(list_url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)

            postings = data.get("jobPostings", [])
            if total_known is None:
                total_known = data.get("total", 0)

            for j in postings:
                ext_path = j.get("externalPath", "")
                if ext_path:
                    # Workday API sometimes omits the board name from externalPath,
                    # returning "/job/..." instead of "/en-US/{board}/job/...".
                    # URLs without the board 404, so we prepend it when missing.
                    if url_prefix:
                        job_url = f"{base_url}/{url_prefix}{ext_path}"
                    elif ext_path.startswith("/job/"):
                        job_url = f"{base_url}/{board}{ext_path}"
                    else:
                        job_url = f"{base_url}{ext_path}"
                    job_url = job_url.replace("//", "/").replace(":/", "://")
                else:
                    job_url = ""
                jobs.append(
                    {
                        "title": j.get("title", ""),
                        "location": j.get("locationsText", ""),
                        "department": "",
                        "url": job_url,
                        "external_id": hashlib.md5(
                            job_url.encode()
                            if job_url
                            else f"{org_name}:{j.get('title', '')}".encode()
                        ).hexdigest()[:12],
                        "snippet": "",
                        "_ext_path": ext_path,  # temp: used in phase 2, removed before return
                    }
                )

            offset += len(postings)
            if not postings or offset >= (total_known or 0):
                break

        print(f"  [{org_name}] Found {len(jobs)} vacancies, fetching descriptions...")

        # ── Phase 2: Per-job descriptions ────────────────────────────────────
        fetched_desc = 0
        for i, job in enumerate(jobs):
            ext_path = job.pop("_ext_path", "")
            if not ext_path:
                continue
            detail_url = f"{base_url}/wday/cxs/{tenant}/{board}{ext_path}"
            try:
                req = urllib.request.Request(
                    detail_url,
                    headers={"Accept": "application/json", "User-Agent": headers["User-Agent"]},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    detail = json.load(resp)
                html_desc = detail.get("jobPostingInfo", {}).get("jobDescription", "") or ""
                if html_desc:
                    job["full_description"] = _html_to_text(html_desc)
                    job["snippet"] = _html_to_snippet(html_desc)
                    fetched_desc += 1
            except Exception:
                pass  # description stays empty; LLM will score by title only

            if (i + 1) % 20 == 0:
                print(f"  [{org_name}] Descriptions: {i + 1}/{len(jobs)} ({fetched_desc} ok)")
            time.sleep(_WORKDAY_DETAIL_RATE_LIMIT)

        print(f"  [{org_name}] Descriptions: {fetched_desc}/{len(jobs)} fetched")
        return jobs

    except Exception as e:
        print(f"  [{org_name}] Workday API ERROR: {e}")
        return []


# ---------------------------------------------------------------------------
# Lever public API
# ---------------------------------------------------------------------------


def fetch_lever(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Lever public API (free, no auth).
    Endpoint: GET https://api.lever.co/v0/postings/{slug}?mode=json
    """
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    print(f"  [{org_name}] Lever API: {url}")
    try:
        resp = _pkg.requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()  # Lever returns a flat JSON array
        jobs = []
        for j in data:
            categories = j.get("categories", {})
            raw_desc = j.get("descriptionPlain", "") or ""
            snippet = (
                raw_desc[:400].rsplit(" ", 1)[0] + "\u2026" if len(raw_desc) > 400 else raw_desc
            )
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
    except Exception as e:
        print(f"  [{org_name}] ERROR: {e}")
        return []


# ---------------------------------------------------------------------------
# Ashby public API
# ---------------------------------------------------------------------------


def fetch_ashby(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Ashby public API (free, no auth).
    Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    print(f"  [{org_name}] Ashby API: {url}")
    try:
        resp = _pkg.requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for j in data.get("jobs", []):
            raw_desc = j.get("descriptionPlain", "") or ""
            html_desc = j.get("descriptionHtml", "") or ""
            full_desc = raw_desc or _html_to_text(html_desc)
            snippet = (
                _html_to_snippet(html_desc)
                if html_desc
                else (
                    raw_desc[:400].rsplit(" ", 1)[0] + "\u2026" if len(raw_desc) > 400 else raw_desc
                )
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
    except Exception as e:
        print(f"  [{org_name}] ERROR: {e}")
        return []


# ---------------------------------------------------------------------------
# Workable public widget API
# ---------------------------------------------------------------------------


def fetch_workable(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Workable widget API (free, no auth).
    Endpoint: GET https://apply.workable.com/api/v1/widget/accounts/{slug}
    """
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    print(f"  [{org_name}] Workable API: {url}")
    try:
        resp = _pkg.requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for j in data.get("jobs", []):
            loc = j.get("location", {})
            location_parts = [loc.get("city", ""), loc.get("country", "")]
            location = ", ".join(p for p in location_parts if p)
            if j.get("remote"):
                location = f"Remote \u2014 {location}" if location else "Remote"
            shortcode = j.get("shortcode", "")
            job_url = j.get("url", "") or f"https://apply.workable.com/{slug}/j/{shortcode}/"
            jobs.append(
                {
                    "title": j.get("title", ""),
                    "location": location,
                    "department": j.get("department", ""),
                    "url": job_url,
                    "external_id": shortcode or hashlib.md5(job_url.encode()).hexdigest()[:12],
                    "snippet": j.get("shortDescription", ""),
                }
            )
        print(f"  [{org_name}] Found {len(jobs)} vacancies")
        # Workable list API only returns shortDescription — enrich via Firecrawl
        jobs = _enrich_blind_jobs(jobs, org_name)
        return jobs
    except Exception as e:
        print(f"  [{org_name}] ERROR: {e}")
        return []


# ---------------------------------------------------------------------------
# Recruitee public API
# ---------------------------------------------------------------------------


def fetch_recruitee(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Recruitee public API (free, no auth).
    Endpoint: GET https://{slug}.recruitee.com/api/offers/
    Returns full HTML descriptions, city/country, remote/hybrid, salary, department.
    """
    url = f"https://{slug}.recruitee.com/api/offers/"
    print(f"  [{org_name}] Recruitee API: {url}")
    try:
        resp = _pkg.requests.get(url, timeout=15)
        resp.raise_for_status()
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
    except Exception as e:
        print(f"  [{org_name}] ERROR: {e}")
        return []


# ---------------------------------------------------------------------------
# Teamtailor RSS feed
# ---------------------------------------------------------------------------


def fetch_teamtailor_rss(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from Teamtailor RSS feed (free, no auth).
    Feed URL: https://{slug}.teamtailor.com/jobs.rss
    Returns full HTML descriptions, locations, departments.
    """
    import xml.etree.ElementTree as ET

    url = f"https://{slug}.teamtailor.com/jobs.rss"
    print(f"  [{org_name}] Teamtailor RSS: {url}")
    try:
        resp = _pkg.requests.get(url, timeout=15)
        resp.raise_for_status()

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
    except Exception as e:
        print(f"  [{org_name}] ERROR: {e}")
        return []


# ---------------------------------------------------------------------------
# BambooHR public careers API
# ---------------------------------------------------------------------------

_BAMBOOHR_DETAIL_RATE_LIMIT = 0.3  # seconds between detail requests


def fetch_bamboohr(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from BambooHR public careers API (free, no auth).
    Two-phase: list at /careers/list, then detail at /careers/{id}/detail.
    """
    list_url = f"https://{slug}.bamboohr.com/careers/list"
    print(f"  [{org_name}] BambooHR API: {list_url}")
    try:
        resp = _pkg.requests.get(
            list_url, headers={"Accept": "application/json"}, timeout=15, allow_redirects=False
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            print(
                f"  [{org_name}] ERROR: BambooHR redirected to {resp.headers.get('Location', '?')} — "
                f"account likely disabled, update fetch_strategy in Supabase"
            )
            return []
        resp.raise_for_status()
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
                    dr = _pkg.requests.get(
                        detail_url, headers={"Accept": "application/json"}, timeout=15
                    )
                    dr.raise_for_status()
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
    except Exception as e:
        print(f"  [{org_name}] ERROR: {e}")
        return []


# ---------------------------------------------------------------------------
# SAP SuccessFactors — Career Site Builder tile-search feed
# ---------------------------------------------------------------------------


def _sf_base_url(config: dict) -> str:
    """Resolve the SuccessFactors site base (host + /<Site>) from config.

    Supports both site shapes (KTD3): the Career Site Builder host
    (``jobsearch.createyourowncareer.com/<Site>``) and any backend variant
    supplied directly as ``config['url']`` (e.g. ILO's ``jobs.ilo.org`` /
    ``career5.successfactors.eu`` portal). Falls back to building the default
    CSB URL from ``ats_slug``/``slug``. A trailing ``/search`` or
    ``/tile-search-results`` (and any querystring) is trimmed so the tile and
    search endpoints can be derived cleanly.
    """
    base = (config.get("url") or "").strip()
    if not base:
        site = config.get("ats_slug") or config.get("slug") or ""
        if site:
            base = f"https://jobsearch.createyourowncareer.com/{site}"
    if not base:
        return ""
    base = base.split("?")[0].rstrip("/")
    base = re.sub(r"/(search|tile-search-results)$", "", base).rstrip("/")
    return base


def _parse_successfactors_tiles(html: str, base_url: str, org_name: str) -> list[dict]:
    """Parse a SuccessFactors CSB tile-search HTML fragment into job dicts.

    The endpoint is named "tile-search" but returns an HTML fragment of
    ``<li class="job-tile job-id-<ID> ...">`` tiles, NOT JSON (see module note
    on U4 drift). Each tile carries a ``data-url`` job path, a ``job-id-<ID>``
    class and a ``jobTitle-link`` anchor; the title/anchor repeats across
    responsive sub-sections, so we take the first per tile.
    """
    m_host = re.match(r"(https?://[^/]+)", base_url)
    host = m_host.group(1) if m_host else base_url
    jobs = []
    for li in re.findall(r'<li class="job-tile\b.*?</li>', html, re.DOTALL):
        m_url = re.search(r'data-url="([^"]+)"', li)
        if not m_url:
            continue
        path = html_module.unescape(m_url.group(1))
        job_url = urllib.parse.urljoin(host + "/", path.lstrip("/"))
        m_id = re.search(r"job-id-(\d+)", li)
        ext_id = m_id.group(1) if m_id else hashlib.md5(job_url.encode()).hexdigest()[:12]
        m_title = re.search(r"jobTitle-link[^>]*>\s*(.*?)\s*</a>", li, re.DOTALL)
        title = ""
        if m_title:
            title = html_module.unescape(re.sub(r"\s+", " ", m_title.group(1))).strip()
        if not title:
            continue
        # Location, when present, sits in a jobLocation/jobGeoLocation span.
        location = ""
        m_loc = re.search(
            r'class="[^"]*job(?:Geo)?Location[^"]*"[^>]*>\s*(.*?)\s*</', li, re.DOTALL
        )
        if m_loc:
            location = html_module.unescape(re.sub(r"\s+", " ", m_loc.group(1))).strip()
        jobs.append(
            {
                "title": title,
                "location": location,
                "department": "",
                "url": job_url,
                "external_id": ext_id,
                "snippet": "",
            }
        )
    return jobs


def fetch_successfactors(org_name: str, config: dict) -> list[dict]:
    """Fetch jobs from a SAP SuccessFactors Career Site Builder tile feed (free).

    Endpoint: ``<base>/tile-search-results/?q=&startrow=<N>`` paginated by
    ``startrow`` (25 tiles/page). ``base`` comes from config via
    :func:`_sf_base_url`, so both the ``createyourowncareer.com/<Site>`` host
    and backend variants (ILO's ``jobs.ilo.org`` / ``career5.successfactors.eu``)
    are supported. The tile endpoint returns an empty shell without a session
    cookie, so we GET the search page first to establish one. Unblocks ILO,
    reframe[Tech], Bertelsmann Stiftung, Robert Bosch (WS3/U4).
    """
    base = _sf_base_url(config)
    if not base:
        print(f"  [{org_name}] SuccessFactors: no url/ats_slug configured")
        return []

    search_url = f"{base}/search/"
    tile_url = f"{base}/tile-search-results/"
    print(f"  [{org_name}] SuccessFactors: {tile_url}")

    # Establish a session cookie — the tile endpoint returns an empty shell
    # (16-byte doctype) when careerSite cookies are absent.
    cookies = None
    try:
        boot = _pkg.requests.get(search_url, headers={"User-Agent": _LOCAL_UA}, timeout=20)
        cookies = boot.cookies
    except Exception as e:
        print(f"  [{org_name}] SuccessFactors bootstrap failed ({e}); trying tiles anyway")

    headers = {
        "User-Agent": _LOCAL_UA,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": search_url,
    }

    jobs: list[dict] = []
    seen: set[str] = set()
    startrow, page_size = 0, 25
    for _ in range(20):  # hard page cap
        page_url = f"{tile_url}?q=&startrow={startrow}"
        try:
            resp = _pkg.requests.get(page_url, headers=headers, cookies=cookies, timeout=20)
            resp.raise_for_status()
            text = resp.text
        except Exception as e:
            print(f"  [{org_name}] SuccessFactors page error: {e}")
            break

        page_new = 0
        for j in _parse_successfactors_tiles(text, base, org_name):
            if j["external_id"] in seen:
                continue
            seen.add(j["external_id"])
            jobs.append(j)
            page_new += 1
        if page_new == 0:  # empty page / startrow past the end / all seen
            break
        startrow += page_size

    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


# ---------------------------------------------------------------------------
# ADP Workforce Now — public job-requisitions JSON feed
# ---------------------------------------------------------------------------


def _adp_cid(config: dict) -> str:
    """Resolve the ADP career-center ``cid`` from config (ats_slug or ats_config)."""
    return (
        config.get("ats_slug")
        or (config.get("ats_config") or {}).get("cid")
        or config.get("cid")
        or ""
    ).strip()


def _adp_location(locs) -> str:
    """Join ADP ``requisitionLocations`` into a display string."""
    if not isinstance(locs, list):
        return ""
    names: list[str] = []
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        name = ((loc.get("nameCode") or {}).get("shortName") or "").strip()
        if not name:
            addr = loc.get("address") or {}
            parts = [
                addr.get("cityName", ""),
                (addr.get("countrySubdivisionLevel1") or {}).get("codeValue", ""),
            ]
            name = ", ".join(p for p in parts if p)
        if name:
            names.append(name.strip())
    return " | ".join(dict.fromkeys(names))


def _adp_job_url(portal: str, cid: str, item_id: str) -> str:
    """Build a per-requisition public URL, reusing the configured portal if given."""
    if portal and "recruitment.html" in portal:
        sep = "&" if "?" in portal else "?"
        return f"{portal}{sep}jobId={item_id}" if item_id else portal
    base = (
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
        f"recruitment.html?cid={cid}&type=MP&lang=en_US"
    )
    return f"{base}&jobId={item_id}" if item_id else base


def _adp_snippet(location: str, pay) -> str:
    """Build a short snippet from location + pay range (feed has no description)."""
    parts = [location] if location else []
    if isinstance(pay, dict):
        lo = (pay.get("minimumRate") or {}).get("amountValue")
        hi = (pay.get("maximumRate") or {}).get("amountValue")
        currency = ((pay.get("minimumRate") or {}).get("currencyCode") or "").strip()
        nums = [f"{int(v):,}" for v in (lo, hi) if isinstance(v, (int, float))]
        if nums:
            parts.append(f"{currency} {'-'.join(nums)}".strip())
    return ". ".join(parts)


def fetch_adp_json(org_name: str, config: dict) -> list[dict]:
    """Fetch jobs from the ADP Workforce Now public requisitions feed (free, no auth).

    Endpoint: ``workforcenow.adp.com/mascsr/default/careercenter/public/events/
    staffing/v1/job-requisitions?cid=<CID>`` where ``cid`` comes from config.
    Parses ``jobRequisitions`` into job dicts. The list feed carries no
    description, so the snippet is built from location + pay range (a per-job
    detail fetch is skipped — these boards are low-fit/US-only). Unblocks
    Rockefeller and Carnegie (WS3/U5).
    """
    cid = _adp_cid(config)
    if not cid:
        print(f"  [{org_name}] ADP: no cid configured (ats_slug/ats_config.cid)")
        return []
    url = (
        "https://workforcenow.adp.com/mascsr/default/careercenter/public/"
        f"events/staffing/v1/job-requisitions?cid={cid}"
    )
    print(f"  [{org_name}] ADP Workforce Now: {url}")
    try:
        resp = _pkg.requests.get(
            url, headers={"Accept": "application/json", "User-Agent": _LOCAL_UA}, timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [{org_name}] ERROR: {e}")
        return []

    portal = config.get("careers_url") or config.get("url") or ""
    jobs = []
    for r in data.get("jobRequisitions") or []:
        if not isinstance(r, dict):
            continue
        title = (r.get("requisitionTitle") or "").strip()
        if not title:
            continue
        item_id = str(r.get("itemID") or "")
        location = _adp_location(r.get("requisitionLocations"))
        jobs.append(
            {
                "title": title,
                "location": location,
                "department": "",
                "url": _adp_job_url(portal, cid, item_id),
                "external_id": item_id
                or hashlib.md5(f"{org_name}:{title}".encode()).hexdigest()[:12],
                "snippet": _adp_snippet(location, r.get("payGradeRange")),
            }
        )
    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


# ---------------------------------------------------------------------------
# Firecrawl scraper (SDK with JSON extraction + CLI fallback)
# ---------------------------------------------------------------------------

FIRECRAWL_JOBS_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "location": {"type": "string"},
                    "url": {"type": "string"},
                    "snippet": {"type": "string"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["jobs"],
}

# Per-run state (change statuses, scrape statuses, credit balance)
# lives on the fetchers package namespace — see fetchers/__init__.py.


def get_firecrawl_change_statuses() -> dict[str, str]:
    """Return the change tracking statuses from the last fetch run."""
    return dict(_pkg._last_firecrawl_change_status)


def get_scrape_statuses() -> dict[str, str]:
    """Return fetch_status overrides set by the scraper (e.g. js_required)."""
    return dict(_pkg._last_scrape_status)


def _firecrawl_credits_available() -> bool:
    """Check Firecrawl credit balance once per run.

    Queries GET /v2/team/credit-usage with $FIRECRAWL_API_KEY. Caches the
    result for the rest of the process. Returns True only when credits > 0.
    On any error (no key, network), assumes credits available and lets the
    normal Firecrawl path surface the real error.
    """
    if _pkg._firecrawl_credits_remaining is not None:
        return _pkg._firecrawl_credits_remaining > 0

    import os

    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        # No key: can't check, but Firecrawl client likely unusable anyway.
        _pkg._firecrawl_credits_remaining = -1  # unknown → treat as "try anyway"
        return True
    try:
        resp = _pkg.requests.get(
            "https://api.firecrawl.dev/v2/team/credit-usage",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        data = resp.json().get("data", {}) if resp.ok else {}
        remaining = int(data.get("remainingCredits", -1))
        _pkg._firecrawl_credits_remaining = remaining
        if remaining == 0:
            print("  Firecrawl credits exhausted — using local scraper")
        return remaining != 0  # >0 → use Firecrawl; -1 (unknown) → try anyway
    except Exception as e:
        print(f"  Firecrawl credit check failed ({e}); will attempt Firecrawl")
        _pkg._firecrawl_credits_remaining = -1
        return True


# Errors from the Firecrawl SDK that signal quota exhaustion / rate limits.
_QUOTA_ERROR_MARKERS = (
    "402",
    "429",
    "payment required",
    "insufficient credit",
    "out of credit",
    "rate limit",
    "quota",
    "too many requests",
)


def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _QUOTA_ERROR_MARKERS)


# Browser-like User-Agent lives in fetchers.http (shared skeleton).


def _fetch_pageup_xhr(org_name: str, url: str, *, url_filter: str = "") -> list[dict]:
    """PageUp ATS (e.g. jobs.unicef.org): facet filters apply only via XHR.

    Plain GET ignores ?optionsFacetsDD_* facets and returns the unfiltered
    board. With X-Requested-With the server returns {"results": "<html>"}
    honoring the facet. PageUp throttles bursts (HTTP 202 + empty body), so
    retry with backoff; production cadence is one request per TTL cycle.
    """
    import time

    print(f"  [{org_name}] PageUp XHR scrape: {url}")
    headers = {
        "User-Agent": _LOCAL_UA,
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _get_throttled(req_url, *, xhr=True, retries=3):
        """GET with backoff: PageUp answers bursts with HTTP 202 + empty body."""
        h = headers if xhr else {"User-Agent": _LOCAL_UA}
        for attempt in range(retries):
            if attempt:
                time.sleep(60 * attempt)
            try:
                resp = _pkg.requests.get(req_url, headers=h, timeout=20)
                if resp.status_code == 200 and resp.text:
                    return resp.text
                print(
                    f"  [{org_name}] PageUp throttled "
                    f"(HTTP {resp.status_code}), retry {attempt + 1}/{retries}..."
                )
            except Exception as e:
                print(f"  [{org_name}] PageUp fetch error: {e}")
        return ""

    url_filter_re = re.compile(url_filter) if url_filter else None
    jobs, seen_urls, all_md = [], set(), []
    sep = "&" if "?" in url else "?"
    for page in range(1, 6):
        page_url = url if page == 1 else f"{url}{sep}page={page}"
        raw = _get_throttled(page_url)
        if not raw.strip().startswith("{"):
            break
        html = _absolutize_links(json.loads(raw).get("results", ""), url)
        all_md.append(_html_to_markdown(html))

        # Parse the PageUp list structure directly: markdown-parsing drops
        # most rows (titles routinely exceed the 100-char title limit).
        page_new = 0
        for m in re.finditer(
            r'class="job-link"\s+href="([^"]+)"\s*>\s*([^<]+)</a>(.{0,2000}?)'
            r'(?=class="job-link"|$)',
            html,
            re.DOTALL,
        ):
            job_url, title, tail = (
                m.group(1),
                html_module.unescape(m.group(2)).strip(),
                m.group(3),
            )
            if job_url in seen_urls:
                continue
            seen_urls.add(job_url)
            if url_filter_re and not url_filter_re.search(job_url):
                continue
            snippet_m = re.search(r"<p[^>]*>\s*([^<]{30,})</p>", tail)
            loc_m = re.search(r"location[^>]*>\s*<[^>]*>\s*([^<]+)<", tail, re.IGNORECASE)
            jobs.append(
                {
                    "title": title,
                    "location": (loc_m.group(1).strip() if loc_m else ""),
                    "department": "",
                    "url": job_url,
                    "external_id": hashlib.md5(job_url.encode()).hexdigest()[:12],
                    "snippet": (
                        html_module.unescape(snippet_m.group(1).strip()) if snippet_m else ""
                    ),
                }
            )
            page_new += 1
        if page_new == 0:  # page param ignored or past the end
            break
        time.sleep(10)
    if not jobs:
        _pkg._last_scrape_status[org_name] = "js_required"
        return []
    _cache_markdown(org_name, "\n".join(all_md), source="pageup")
    print(f"  [{org_name}] PageUp parsed {len(jobs)} vacancies")

    # Detail pages are server-rendered; fetch gently to respect throttling.
    for job in jobs:
        time.sleep(10)
        detail_html = _get_throttled(job["url"], xhr=False, retries=2)
        if len(detail_html) > 2000:
            detail_md = _html_to_markdown(detail_html)
            if len(detail_md) > len(job.get("snippet", "")):
                job["full_description"] = detail_md
    with_desc = sum(1 for j in jobs if j.get("full_description"))
    print(f"  [{org_name}] PageUp descriptions: {with_desc}/{len(jobs)}")
    return jobs


def _fetch_wagtail_jobs_api(org_name: str, url: str) -> list[dict]:
    """Wagtail CMS pages API (e.g. /api/v2/pages/?type=jobs.JobPage).

    The jobs *listing* page is a JS app, but the underlying Wagtail API and
    the per-job detail pages are server-rendered — zero-cost to fetch.
    """
    import time

    print(f"  [{org_name}] Wagtail jobs API: {url}")
    try:
        resp = _pkg.requests.get(url, headers={"User-Agent": _LOCAL_UA}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [{org_name}] Wagtail API error: {e}")
        _pkg._last_scrape_status[org_name] = "js_required"
        return []

    jobs = []
    for it in data.get("items", []):
        title = (it.get("title") or "").strip()
        job_url = (it.get("meta", {}).get("html_url") or "").strip()
        if not title or not job_url:
            continue

        def _s(key):
            v = it.get(key)
            return v.strip() if isinstance(v, str) else ""

        extras = " | ".join(
            filter(
                None,
                [
                    _s("location"),
                    _s("salary"),
                    _s("contract_type"),
                    f"closes {it['closes'][:10]}" if isinstance(it.get("closes"), str) else "",
                ],
            )
        )
        snippet = " ".join(filter(None, [_s("listing_summary"), extras]))
        jobs.append(
            {
                "title": title,
                "location": _s("location"),
                "department": "",
                "url": job_url,
                "external_id": hashlib.md5(job_url.encode()).hexdigest()[:12],
                "snippet": snippet,
            }
        )
    print(f"  [{org_name}] Wagtail API: {len(jobs)} vacancies")

    for job in jobs:
        time.sleep(2)
        try:
            resp = _pkg.requests.get(job["url"], headers={"User-Agent": _LOCAL_UA}, timeout=20)
            if resp.status_code == 200 and len(resp.text) > 2000:
                job["full_description"] = _html_to_markdown(resp.text)
        except Exception as e:
            print(f"  [{org_name}] detail fetch failed for {job['title']}: {e}")
    with_desc = sum(1 for j in jobs if j.get("full_description"))
    print(f"  [{org_name}] Wagtail descriptions: {with_desc}/{len(jobs)}")
    return jobs


def _fetch_local_scrape(org_name: str, url: str, *, url_filter: str = "") -> list[dict]:
    """Zero-cost fallback: fetch the page with requests → markdown → parse.

    Records a 'js_required' status override when the page looks like a
    JS-rendered shell (little text / no links) so the company row is marked
    honestly instead of faking a successful empty fetch.
    """
    if "optionsFacetsDD" in url or "/filter/?" in url:
        return _fetch_pageup_xhr(org_name, url, url_filter=url_filter)
    if "/api/v2/pages/" in url:
        return _fetch_wagtail_jobs_api(org_name, url)
    print(f"  [{org_name}] Local scrape (no Firecrawl credits): {url}")
    try:
        resp = _pkg.requests.get(
            url,
            headers={
                "User-Agent": _LOCAL_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  [{org_name}] Local fetch error: {e}")
        return []

    markdown = _html_to_markdown(resp.text)
    _cache_markdown(org_name, markdown, source="local")

    # JS-shell detection: thin text or no links → can't scrape without a browser.
    text_len = len(re.sub(r"\s+", " ", markdown).strip())
    has_links = "](" in markdown
    if text_len < 500 or not has_links:
        print(
            f"  [{org_name}] Page looks JS-rendered "
            f"(text={text_len} chars, links={has_links}) → js_required"
        )
        _pkg._last_scrape_status[org_name] = "js_required"
        return []

    jobs = parse_markdown_jobs(markdown, org_name, url_filter=url_filter)
    print(f"  [{org_name}] Local scraper parsed {len(jobs)} vacancies")
    if not jobs:
        # HTML had links but parser found no job-like rows: likely JS-gated list.
        _pkg._last_scrape_status[org_name] = "js_required"
    return jobs


# ---------------------------------------------------------------------------
# Amazon Jobs public API
# ---------------------------------------------------------------------------


def fetch_amazon_jobs(org_name: str, config: dict) -> list[dict]:
    """Fetch jobs from Amazon Jobs search API (free, no auth).
    Endpoint: GET https://www.amazon.jobs/en/search.json?base_query=...
    Supports multiple queries (comma-separated) since the API does AND matching.
    """
    queries_raw = config.get("queries", config.get("base_query", "nonprofit,social impact"))
    queries = (
        [q.strip() for q in queries_raw.split(",")] if isinstance(queries_raw, str) else queries_raw
    )
    api_url = "https://www.amazon.jobs/en/search.json"
    limit = 100
    seen_ids: set[str] = set()
    all_jobs: list[dict] = []

    print(f"  [{org_name}] Amazon Jobs API: queries={queries}")
    for query in queries:
        offset = 0
        while True:
            params = {
                "base_query": query,
                "offset": offset,
                "result_limit": limit,
                "invalid_location": "false",
            }
            try:
                resp = _pkg.requests.get(api_url, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  [{org_name}] ERROR ({query}): {e}")
                break

            raw_jobs = data.get("jobs", [])
            if not raw_jobs:
                break

            for j in raw_jobs:
                title = (j.get("title") or "").strip()
                if not title:
                    continue
                job_id = j.get("id_icims") or j.get("id") or ""
                ext_id = (
                    str(job_id)
                    if job_id
                    else hashlib.md5(f"{org_name}:{title}".encode()).hexdigest()[:12]
                )
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                job_url = f"https://www.amazon.jobs{j['job_path']}" if j.get("job_path") else ""
                desc = (j.get("description") or "").strip()
                snippet = (j.get("description_short") or "").strip()
                location = (j.get("location") or "").strip()

                all_jobs.append(
                    {
                        "title": title,
                        "location": location,
                        "department": (j.get("job_category") or "").strip(),
                        "url": job_url,
                        "external_id": ext_id,
                        "snippet": snippet,
                        "full_description": desc,
                    }
                )

            hits = data.get("hits", 0)
            offset += limit
            if offset >= hits:
                break

    print(f"  [{org_name}] Found {len(all_jobs)} vacancies (deduped across {len(queries)} queries)")
    return all_jobs


# ---------------------------------------------------------------------------
# Apple Jobs API (CSRF token + POST)
# ---------------------------------------------------------------------------


def fetch_apple_jobs(org_name: str, config: dict) -> list[dict]:
    """Fetch jobs from Apple Jobs API (free, CSRF token required).
    Step 1: GET /api/csrfToken → extract token + cookies
    Step 2: POST /api/role/search with JSON body
    """
    query = config.get("query", "social impact nonprofit")
    print(f"  [{org_name}] Apple Jobs API: query={query!r}")

    session = _pkg.requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # Step 1: Get CSRF token
    try:
        csrf_resp = session.get("https://jobs.apple.com/api/csrfToken", timeout=15)
        csrf_resp.raise_for_status()
        csrf_token = csrf_resp.headers.get("X-Apple-CSRF-Token", "")
        if not csrf_token:
            # Try extracting from response body/cookies as fallback
            csrf_token = csrf_resp.text.strip()
        if not csrf_token:
            print(f"  [{org_name}] ERROR: No CSRF token returned")
            return []
    except Exception as e:
        print(f"  [{org_name}] ERROR getting CSRF token: {e}")
        return []

    all_jobs: list[dict] = []
    page = 1

    while True:
        payload = {
            "query": query,
            "filters": {"range": {"standardWeeklyHours": {"start": None, "end": None}}},
            "page": page,
            "locale": "en-us",
            "sort": "relevance",
        }
        try:
            resp = session.post(
                "https://jobs.apple.com/api/role/search",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Apple-CSRF-Token": csrf_token,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [{org_name}] ERROR on page {page}: {e}")
            break

        search_results = data.get("searchResults", [])
        if not search_results:
            break

        for j in search_results:
            title = (j.get("postingTitle") or "").strip()
            if not title:
                continue
            pos_id = j.get("positionId", "")
            job_url = f"https://jobs.apple.com/en-us/details/{pos_id}" if pos_id else ""
            summary = (j.get("jobSummary") or "").strip()
            locs = j.get("locations", [])
            location = (
                ", ".join((loc.get("name") or loc.get("city") or "") for loc in locs[:3])
                if isinstance(locs, list)
                else ""
            )

            all_jobs.append(
                {
                    "title": title,
                    "location": location,
                    "department": (
                        j.get("team", {}).get("teamName", "")
                        if isinstance(j.get("team"), dict)
                        else ""
                    ),
                    "url": job_url,
                    "external_id": str(pos_id)
                    if pos_id
                    else hashlib.md5(f"{org_name}:{title}".encode()).hexdigest()[:12],
                    "snippet": summary,
                }
            )

        total = data.get("totalRecords", 0)
        if len(all_jobs) >= total or not search_results:
            break
        page += 1

    print(f"  [{org_name}] Found {len(all_jobs)} vacancies")
    return all_jobs


def fetch_firecrawl_scrape(
    org_name: str, url: str, *, use_json: bool = True, url_filter: str = ""
) -> list[dict]:
    """Scrape a careers page via Firecrawl SDK (preferred) or CLI fallback.

    With use_json=True (default for companies): requests both JSON extraction
    and markdown in a single API call (5 credits). Falls back to markdown
    parsing if JSON yields 0 results, then to CLI if SDK unavailable.

    With use_json=False (boards): requests markdown only (1 credit).
    """
    FIRECRAWL_CACHE.mkdir(parents=True, exist_ok=True)

    # PageUp facets (?optionsFacetsDD_*, /filter/?) apply only via XHR with
    # X-Requested-With — Firecrawl's plain render gets the unfiltered board,
    # so route these straight to the local PageUp scraper.
    if "optionsFacetsDD" in url or "/filter/?" in url:
        return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)

    # Quota guard: if credits are exhausted, skip Firecrawl entirely (saves
    # ~60s of latency per company) and go straight to the local scraper.
    # Record the reason (U9) so an empty result is marked 'credit_exhausted'
    # rather than an ambiguous no_data — the local fallback may override this
    # with 'js_required' or clear it by returning rows.
    if not _firecrawl_credits_available():
        _pkg._last_scrape_status[org_name] = "credit_exhausted"
        return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)

    print(f"  [{org_name}] Firecrawl scrape: {url}")

    client = _pkg.get_firecrawl_client()
    if client is None:
        print(f"  [{org_name}] SDK not available, falling back to local scraper")
        return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)

    # Build formats list
    formats = ["markdown", "changeTracking"]
    if use_json:
        formats.append({"type": "json", "schema": FIRECRAWL_JOBS_SCHEMA})

    try:
        result = client.scrape(
            url,
            formats=formats,
            only_main_content=True,
            timeout=60000,
            actions=[{"type": "wait", "milliseconds": 5000}],
        )
    except Exception as e:
        print(f"  [{org_name}] SDK error: {e}")
        if _is_quota_error(e):
            # Mark credits exhausted for the rest of the run, then go local.
            _pkg._firecrawl_credits_remaining = 0
            _pkg._last_scrape_status[org_name] = "credit_exhausted"  # U9 reason code
            print(f"  [{org_name}] Quota/rate-limit error — switching to local scraper")
        else:
            print(f"  [{org_name}] Falling back to local scraper")
        return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)

    # --- Handle change tracking if present ---
    change_tracking = getattr(result, "changeTracking", None)
    if change_tracking is None:
        change_tracking = getattr(result, "change_tracking", None)
    if change_tracking:
        status = getattr(change_tracking, "changeStatus", None)
        if status is None:
            status = getattr(change_tracking, "change_status", None)
        _pkg._last_firecrawl_change_status[org_name] = status or "unknown"
        if status == "same":
            print(f"  [{org_name}] Page unchanged since last scrape — skipping")
            return []

    # --- Try JSON extraction first ---
    if use_json:
        json_data = getattr(result, "json", None)
        if json_data:
            jobs = _parse_json_jobs(json_data, org_name, url, url_filter=url_filter)
            if jobs:
                print(f"  [{org_name}] Parsed {len(jobs)} vacancies from JSON extraction")
                # Cache markdown for debugging
                _cache_markdown(org_name, getattr(result, "markdown", "") or "")
                # Enrich blind jobs (no full_description) via individual page scrape
                jobs = _enrich_blind_jobs(jobs, org_name)
                return jobs
            print(f"  [{org_name}] JSON extraction returned 0 valid jobs, trying markdown")

    # --- Fall back to markdown parsing ---
    markdown = getattr(result, "markdown", "") or ""
    _cache_markdown(org_name, markdown)
    if markdown:
        jobs = parse_markdown_jobs(markdown, org_name, url_filter=url_filter)
        print(f"  [{org_name}] Parsed {len(jobs)} vacancies from markdown")
        # Enrich blind jobs (no full_description) via individual page scrape
        jobs = _enrich_blind_jobs(jobs, org_name)
        return jobs

    print(f"  [{org_name}] No content returned from SDK — trying local scraper")
    return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)


def _enrich_blind_jobs(jobs: list[dict], org_name: str) -> list[dict]:
    """Scrape individual job URLs via Firecrawl for jobs missing full_description.

    Modifies jobs in-place, adding full_description from scraped page content.
    Skips blacklisted titles to avoid wasting Firecrawl credits.
    """
    client = _pkg.get_firecrawl_client()
    if not client:
        return jobs

    blind = [(i, j) for i, j in enumerate(jobs) if not j.get("full_description") and j.get("url")]
    if not blind:
        return jobs

    # Pre-filter blacklisted titles
    import filters

    to_enrich = [
        (i, j) for i, j in blind if not filters.title_words_blacklisted(j.get("title", ""))
    ]
    skipped = len(blind) - len(to_enrich)

    if not to_enrich:
        if skipped:
            print(f"  [{org_name}] {skipped} blind jobs skipped (blacklisted)")
        return jobs

    print(
        f"  [{org_name}] Enriching {len(to_enrich)} blind jobs via Firecrawl"
        + (f" ({skipped} blacklisted skipped)" if skipped else "")
    )

    enriched = 0
    for idx, (i, job) in enumerate(to_enrich):
        url = job["url"]
        try:
            result = client.scrape(
                url,
                formats=["markdown"],
                only_main_content=True,
                timeout=60000,
            )
            md = ""
            if hasattr(result, "markdown"):
                md = result.markdown or ""
            elif isinstance(result, dict):
                md = result.get("markdown", "")

            if md:
                # Clean markdown to plain text
                text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", "", md)
                text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
                text = re.sub(r"<[^>]{1,200}>", "", text)
                text = re.sub(r"[*_`#\\]", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()

                if len(text) >= 100:
                    jobs[i]["full_description"] = text[:30000]
                    enriched += 1
        except Exception as e:
            print(f"  [{org_name}] Enrich error for {job.get('title', '?')[:40]}: {e}")

        # Rate limit
        if idx < len(to_enrich) - 1:
            time.sleep(0.5)

    print(f"  [{org_name}] Enriched {enriched}/{len(to_enrich)} blind jobs")
    return jobs


def _cache_markdown(org_name: str, markdown: str, *, source: str = "firecrawl") -> None:
    """Save markdown to cache file for debugging.

    Prepends a one-line provenance marker recording which scraper produced
    the content (firecrawl vs local).
    """
    if not markdown:
        return
    FIRECRAWL_CACHE.mkdir(parents=True, exist_ok=True)
    slug = org_name.lower().replace(" ", "_").replace(".", "")
    output_file = FIRECRAWL_CACHE / f"{slug}.md"
    try:
        header = f"<!-- source: {source} | {time.strftime('%Y-%m-%d %H:%M')} -->\n"
        output_file.write_text(header + markdown, encoding="utf-8")
    except Exception:
        pass


def _fetch_firecrawl_scrape_cli(org_name: str, url: str, *, url_filter: str = "") -> list[dict]:
    """Legacy CLI fallback: scrape via firecrawl CLI subprocess."""
    FIRECRAWL_CACHE.mkdir(parents=True, exist_ok=True)
    slug = org_name.lower().replace(" ", "_").replace(".", "")
    output_file = FIRECRAWL_CACHE / f"{slug}.md"

    try:
        result = subprocess.run(
            [
                "firecrawl",
                "scrape",
                url,
                "--wait-for",
                "5000",
                "--only-main-content",
                "-o",
                str(output_file),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            print(f"  [{org_name}] CLI error: {result.stderr[:200]}")
            return []
        if not output_file.exists():
            print(f"  [{org_name}] No output file created")
            return []
        content = output_file.read_text(encoding="utf-8")
        jobs = parse_markdown_jobs(content, org_name, url_filter=url_filter)
        print(f"  [{org_name}] CLI parsed {len(jobs)} vacancies from markdown")
        return jobs
    except subprocess.TimeoutExpired:
        print(f"  [{org_name}] CLI timeout")
        return []
    except FileNotFoundError:
        print(f"  [{org_name}] firecrawl CLI not found")
        return []
    except Exception as e:
        print(f"  [{org_name}] CLI error: {e}")
        return []


# ---------------------------------------------------------------------------
# UNOPS careers widget (official endpoint, no auth)
# ---------------------------------------------------------------------------


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
    try:
        resp = _pkg.requests.get(url, timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"  [{org_name}] ERROR: {e}")
        return []

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
        resp = _pkg.requests.get(url, timeout=20)
        resp.raise_for_status()
        # Gone jobs 302-redirect to /careersmarketplace/Error — without this
        # guard the error page text would be saved as a full_description.
        if "/error" in resp.url.lower():
            return ""
        html = resp.text
    except Exception:
        return ""

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


# ---------------------------------------------------------------------------
# Job board fetchers
# ---------------------------------------------------------------------------


def fetch_algolia_board(board_cfg: dict) -> list[dict]:
    """Query an Algolia search index directly via REST API (free, no Firecrawl).
    Applies GLOBAL_BLACKLIST + board-specific blacklist. NO caps, NO keyword/location filters.
    """
    app_id = board_cfg["algolia_app_id"]
    api_key = board_cfg["algolia_api_key"]
    index = board_cfg["algolia_index"]
    board_name = board_cfg["name"]

    url = f"https://{app_id}-dsn.algolia.net/1/indexes/{index}/query"
    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
        "Content-Type": "application/json",
    }

    # Fetch all hits from the index (paginate through everything)
    all_hits = []
    page = 0
    per_page = 200

    while True:
        payload = json.dumps(
            {
                "query": "",
                "hitsPerPage": per_page,
                "page": page,
            }
        )
        try:
            resp = _pkg.requests.post(url, data=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [{board_name}] Algolia ERROR page {page}: {e}")
            break

        hits = data.get("hits", [])
        if not hits:
            break
        all_hits.extend(hits)

        if page >= data.get("nbPages", 1) - 1:
            break
        page += 1

    # Apply GLOBAL_BLACKLIST + board-specific blacklist (NO caps, NO location filter, NO keyword filter)
    board_blacklist = board_cfg.get("board_blacklist", [])
    combined_blacklist = GLOBAL_BLACKLIST + board_blacklist
    filtered = _blacklist_filter(
        all_hits,
        combined_blacklist,
        title_fields=["title"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs = []
    generic_filtered_out = 0
    for hit in filtered:  # NO cap — LLM scoring decides relevance
        org = hit.get("company_name") or f"[via {board_name}]"
        title = hit.get("title") or ""
        if _is_generic_pipeline_title(title):
            generic_filtered_out += 1
            continue
        # Location: join city tags, fallback to country tags
        cities = hit.get("tags_city") or []
        location = ", ".join(cities) if cities else ", ".join(hit.get("tags_country") or [])
        job_url = hit.get("url_external") or ""
        # Strip HTML from description_short
        snippet = hit.get("description_short") or ""
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if len(snippet) > 400:
            snippet = snippet[:400].rsplit(" ", 1)[0] + "\u2026"

        # Build full_description from all available Algolia fields
        comp_desc = hit.get("company_description") or ""
        comp_desc = re.sub(r"<[^>]+>", " ", comp_desc)
        comp_desc = re.sub(r"\s+", " ", comp_desc).strip()
        skills = ", ".join(hit.get("tags_skill") or [])
        loc_type = ", ".join(hit.get("tags_location_type") or [])
        exp_req = ", ".join(hit.get("tags_exp_required") or [])
        areas = ", ".join(hit.get("tags_area") or [])
        salary = hit.get("salary") or ""

        desc_parts = [snippet]
        if comp_desc:
            desc_parts.append(f"About {org}: {comp_desc}")
        meta = []
        if areas:
            meta.append(f"Area: {areas}")
        if skills:
            meta.append(f"Skills: {skills}")
        if loc_type:
            meta.append(f"Location type: {loc_type}")
        if exp_req:
            meta.append(f"Experience: {exp_req}")
        if salary:
            meta.append(f"Salary: {salary}")
        if meta:
            desc_parts.append(" | ".join(meta))

        full_description = "\n\n".join(desc_parts)

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": ", ".join(hit.get("tags_area") or []),
                "url": job_url,
                "external_id": hit.get("objectID")
                or hashlib.md5(f"{org}:{title}".encode()).hexdigest()[:12],
                "snippet": snippet,
                "full_description": full_description,
                "compensation": hit.get("salary") or "",
                "org_override": org,
                "org_url": board_cfg["url"],
            }
        )

    print(
        f"  [{board_name}] Algolia: {len(jobs)} relevant from {len(all_hits)} total"
        f" (generic postings filtered: {generic_filtered_out})"
    )
    return jobs


def fetch_firecrawl_board(board_cfg: dict) -> list[dict]:
    """Scrape a job board page via Firecrawl, parse listings, apply GLOBAL_BLACKLIST."""
    board_name = board_cfg["name"]
    url = board_cfg["url"]

    raw_jobs = fetch_firecrawl_scrape(board_name, url, use_json=False)
    # Apply GLOBAL_BLACKLIST only (NO keyword filter, NO cap)
    filtered = _blacklist_filter(
        raw_jobs, GLOBAL_BLACKLIST, title_fields=["title"], substr_blacklist=GLOBAL_BLACKLIST_SUBSTR
    )

    jobs = []
    for job in filtered:  # NO cap — LLM scoring decides relevance
        org = _extract_org_from_listing(job.get("snippet", ""), job.get("title", ""), board_name)
        jobs.append(
            {
                **job,
                "org_override": org,
                "org_url": job.get("url") or url,
            }
        )

    print(
        f"  [{board_name}] Board: {len(jobs)} relevant from {len(raw_jobs)} raw (blacklist applied)"
    )
    return jobs


# ---------------------------------------------------------------------------
# ReliefWeb job board (RSS feed — no auth required)
# ---------------------------------------------------------------------------


def fetch_reliefweb_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from ReliefWeb RSS feed (free, no registration).
    The JSON API requires pre-approved appname since Nov 2025,
    so we use the public RSS feed instead.
    """
    import xml.etree.ElementTree as ET

    board_name = board_cfg["name"]

    # RSS feed with limit=50 (server caps at 20 per request, so we fetch twice)
    base = "https://reliefweb.int/jobs/rss.xml"
    print(f"  [{board_name}] ReliefWeb RSS: fetching...")

    all_items = []
    # Fetch without search (broadest), then filter client-side
    for offset in [0, 20]:
        url = f"{base}?limit=20&offset={offset}"
        try:
            resp = _pkg.requests.get(url, timeout=20)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")
            all_items.extend(items)
            if len(items) < 20:
                break
        except Exception as e:
            print(f"  [{board_name}] RSS ERROR (offset={offset}): {e}")
            break

    total = len(all_items)
    board_blacklist = [kw.lower() for kw in board_cfg.get("board_blacklist", [])]

    jobs = []
    for item in all_items:
        title = item.find("title").text if item.find("title") is not None else ""
        job_url = item.find("link").text if item.find("link") is not None else ""
        desc_html = item.find("description").text if item.find("description") is not None else ""

        # Apply GLOBAL_BLACKLIST (word boundaries) + GLOBAL_BLACKLIST_SUBSTR (substring)
        t_lower = title.lower()
        if any(kw in t_lower for kw in GLOBAL_BLACKLIST_SUBSTR):
            continue
        if any(
            re.search(r"\b" + re.escape(bl.lower()) + r"\b", t_lower) for bl in GLOBAL_BLACKLIST
        ):
            continue
        # Board-specific blacklist (substring match)
        if any(kw in t_lower for kw in board_blacklist):
            continue

        # Extract metadata from description HTML
        org = _extract_rss_field(desc_html, r"Organization:\s*([^<]+)")
        location = _extract_rss_field(desc_html, r"Country:\s*([^<]+)")
        deadline = _extract_rss_field(desc_html, r"Closing date:\s*([^<]+)")

        if not org:
            org = f"[via {board_name}]"

        # Extract snippet (the actual description text after metadata divs)
        snippet = _html_to_snippet(desc_html)

        # External ID from URL (e.g. /job/4199305/...)
        ext_id_match = re.search(r"/job/(\d+)/", job_url)
        ext_id = (
            ext_id_match.group(1)
            if ext_id_match
            else hashlib.md5(job_url.encode()).hexdigest()[:12]
        )

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": "",
                "url": job_url,
                "external_id": ext_id,
                "snippet": snippet,
                "deadline": deadline,
                "org_override": org,
                "org_url": board_cfg["url"],
            }
        )

    print(
        f"  [{board_name}] ReliefWeb: {len(jobs)} relevant from {total} total (blacklist applied)"
    )
    return jobs


def _extract_rss_field(html: str, pattern: str) -> str:
    """Extract a field from ReliefWeb RSS description HTML."""
    m = re.search(pattern, html)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Impactpool board (server-rendered HTML, free)
# ---------------------------------------------------------------------------


def _impactpool_location_ok(loc: str) -> bool:
    # Neutral: no geography is privileged. Every location is accepted; drop a
    # location via the user profile's exclude_countries instead.
    return True


def _impactpool_seniority_ok(level: str) -> bool:
    # Neutral: no seniority is privileged. Drop a seniority via the user
    # profile's exclude_title_keywords instead.
    return True


def fetch_impactpool_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from impactpool.org/search by paginating server-rendered HTML.
    Free (no API key, no Firecrawl). Listing-only: title, org, location, seniority.
    Full description is not fetched — relies on later enrichment via /score pipeline.
    """
    from bs4 import BeautifulSoup

    board_name = board_cfg["name"]
    base_url = board_cfg["url"].rstrip("/")
    max_pages = int(board_cfg.get("max_pages", 5))
    board_blacklist = [kw.lower() for kw in board_cfg.get("board_blacklist", [])]

    # No org-level dedup here: the save layer dedups by canonical company + title
    # (and skips inactive companies). Filtering by the company registry would drop
    # vacancies from orgs we know but don't actually fetch directly (e.g. several
    # UN agencies), losing exactly what this board is best at.
    print(f"  [{board_name}] Impactpool: fetching up to {max_pages} pages...")

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    total_seen = 0
    rej = {"location": 0, "seniority": 0, "blacklist": 0}

    for page in range(1, max_pages + 1):
        url = f"{base_url}?page={page}"
        try:
            resp = _pkg.requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except Exception as exc:
            print(f"  [{board_name}] page {page} ERROR: {exc}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all(
            "a",
            attrs={"data-turbo-frame": "_top", "href": re.compile(r"^/jobs/\d+$")},
        )
        if not cards:
            break

        page_new_ids = 0
        for a in cards:
            href = a.get("href", "")
            m = re.match(r"^/jobs/(\d+)$", href)
            if not m:
                continue
            ext_id = m.group(1)
            if ext_id in seen_ids:
                continue
            seen_ids.add(ext_id)
            total_seen += 1
            page_new_ids += 1

            title_div = a.find("div", attrs={"type": "cardTitle"})
            org_div = a.find("div", attrs={"type": "bodyEmphasis"})
            title = title_div.get_text(strip=True) if title_div else ""
            org = org_div.get_text(strip=True) if org_div else ""
            all_typo = a.find_all("div", class_="ip-typography")
            texts = [d.get_text(strip=True) for d in all_typo]
            location = texts[2] if len(texts) > 2 else ""
            seniority = texts[3] if len(texts) > 3 else ""

            if not title or not org:
                continue

            t_lower = title.lower()
            if any(kw in t_lower for kw in GLOBAL_BLACKLIST_SUBSTR):
                rej["blacklist"] += 1
                continue
            if any(
                re.search(r"\b" + re.escape(bl.lower()) + r"\b", t_lower) for bl in GLOBAL_BLACKLIST
            ):
                rej["blacklist"] += 1
                continue
            if any(kw in t_lower for kw in board_blacklist):
                rej["blacklist"] += 1
                continue

            if not _impactpool_location_ok(location):
                rej["location"] += 1
                continue
            if not _impactpool_seniority_ok(seniority):
                rej["seniority"] += 1
                continue

            job_url = f"https://www.impactpool.org/jobs/{ext_id}"
            snippet = f"{org} — {location}. {seniority}".strip(" .")

            jobs.append(
                {
                    "title": title,
                    "location": location,
                    "department": "",
                    "url": job_url,
                    "external_id": ext_id,
                    "snippet": snippet,
                    "deadline": "",
                    "org_override": org,
                    "org_url": board_cfg["url"],
                }
            )

        if page_new_ids == 0:
            break

    print(
        f"  [{board_name}] Impactpool: {len(jobs)} relevant from {total_seen} total "
        f"(rejected: location={rej['location']}, seniority={rej['seniority']}, "
        f"blacklist={rej['blacklist']})"
    )
    return jobs


# ---------------------------------------------------------------------------
# data.org board (WordPress aggregator → real employer ATS links)
# ---------------------------------------------------------------------------


def _fetch_datadotorg_detail(url: str, BeautifulSoup) -> dict:
    """Parse one data.org job page.

    data.org is an aggregator: each posting links out to the real employer's
    ATS. We pull the external apply URL, the real employer name, the labeled
    summary fields (salary/location/deadline) and the full description body.
    The description container is the structured article — NOT the page text —
    so the cookie-consent banner is never captured. Returns {} on failure.
    """
    out: dict = {}
    try:
        resp = _pkg.requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception:
        return out
    # Expired postings 301 to the /jobs/ index; treat them as gone, not a job.
    if resp.url.rstrip("/").endswith("/jobs"):
        out["expired"] = True
        return out
    s = BeautifulSoup(resp.text, "html.parser")

    # "Apply Now" → the employer's external apply URL (fixes the dead link).
    apply_url = ""
    for a in s.find_all("a", href=True):
        if a.get_text(strip=True).lower().startswith("apply"):
            href = a["href"]
            if href.startswith("http") and "data.org" not in href:
                apply_url = href
                break
    out["apply_url"] = apply_url

    # Employer name from the sidebar org block: the string right after the
    # "About the organization" heading. Attributes the role to the real employer,
    # not to data.org. Falls back to the anchor matching the apply URL's domain.
    org_box = s.find(class_=re.compile(r"\bc-sidebar__org\b"))
    if org_box:
        strings = [t.strip() for t in org_box.stripped_strings if t.strip()]
        for i, t in enumerate(strings):
            if t.lower().startswith("about the organization") and i + 1 < len(strings):
                out["employer"] = strings[i + 1]
                break
    if not out.get("employer") and apply_url:
        root = urllib.parse.urlparse(apply_url).netloc.replace("www.", "")
        for a in s.find_all("a", href=True):
            txt = a.get_text(strip=True)
            if root and root in a["href"] and txt and not txt.lower().startswith("apply"):
                out["employer"] = txt
                break

    # Labeled summary fields: <p>Label</p> followed by a sibling holding the value.
    def field_value(label: str) -> str:
        el = s.find(string=re.compile(r"^\s*" + label + r"\s*$"))
        if not el:
            return ""
        sib = el.find_parent().find_next_sibling()
        return sib.get_text(" ", strip=True) if sib else ""

    out["salary"] = field_value("Salary")
    out["location"] = field_value("Location")
    out["deadline"] = field_value("Deadline") or field_value("Deadline to apply")

    # Full description: the structured details article (summary + responsibilities).
    body = s.find(class_=re.compile(r"\bc-single-job__details\b"))
    if body:
        out["description"] = _html_to_multiline(str(body))[:8000]
    summary = s.find(class_=re.compile(r"\bc-single-job__description\b"))
    out["snippet"] = (
        summary.get_text(" ", strip=True)[:500]
        if summary
        else " — ".join(filter(None, [out.get("employer"), out.get("location")]))
    )
    return out


def fetch_datadotorg_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from data.org's WordPress "Data for Social Impact" board.

    Listing comes from the wp-json ``job`` post type (clean title/link/date,
    newest-first); each detail page yields the real employer, the external apply
    URL, salary, location and deadline. Vacancies are attributed to the real
    employer via ``org_override`` so they never pile up under a "data.org" pseudo
    company, and the external apply URL is stored so the dashboard link works.
    """
    from bs4 import BeautifulSoup

    board_name = board_cfg["name"]
    api = board_cfg.get("api_url", "https://data.org/wp-json/wp/v2/job")
    max_jobs = int(board_cfg.get("max_jobs", 60))
    delay = float(board_cfg.get("request_delay", 0.4))
    board_blacklist = [kw.lower() for kw in board_cfg.get("board_blacklist", [])]

    # 1) Listing (per_page caps at 100; page until we have enough or run dry).
    listing: list[dict] = []
    page = 1
    while len(listing) < max_jobs:
        try:
            resp = _pkg.requests.get(
                api,
                params={
                    "per_page": 100,
                    "page": page,
                    "orderby": "date",
                    "order": "desc",
                    "_fields": "id,title,link,date",
                },
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            batch = resp.json()
        except Exception as exc:
            print(f"  [{board_name}] listing page {page} ERROR: {exc}")
            break
        if not batch:
            break
        listing.extend(batch)
        page += 1
    listing = listing[:max_jobs]
    print(f"  [{board_name}] data.org: {len(listing)} listings, fetching details...")

    jobs: list[dict] = []
    rej_blacklist = 0
    for item in listing:
        title = html_module.unescape((item.get("title") or {}).get("rendered", "")).strip()
        link = item.get("link", "")
        ext_id = str(item.get("id", ""))
        if not title or not link:
            continue

        t_lower = title.lower()
        if (
            any(kw in t_lower for kw in GLOBAL_BLACKLIST_SUBSTR)
            or any(
                re.search(r"\b" + re.escape(b.lower()) + r"\b", t_lower) for b in GLOBAL_BLACKLIST
            )
            or any(kw in t_lower for kw in board_blacklist)
        ):
            rej_blacklist += 1
            continue

        detail = _fetch_datadotorg_detail(link, BeautifulSoup)
        time.sleep(delay)
        if detail.get("expired"):
            continue

        jobs.append(
            {
                "title": title,
                "location": detail.get("location", ""),
                "department": "",
                "url": detail.get("apply_url") or link,
                "external_id": ext_id,
                "snippet": detail.get("snippet", ""),
                "full_description": detail.get("description", ""),
                "compensation": detail.get("salary", ""),
                "deadline": detail.get("deadline", ""),
                "org_override": detail.get("employer", ""),
                "org_url": "https://data.org/jobs/",
            }
        )

    print(f"  [{board_name}] data.org: {len(jobs)} jobs (blacklist={rej_blacklist})")
    return jobs


# ---------------------------------------------------------------------------
# Arbeitnow board (free JSON API, European tech, visa/remote flags)
# ---------------------------------------------------------------------------


def fetch_arbeitnow_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from the Arbeitnow job board API (free, no key).

    GET https://www.arbeitnow.com/api/job-board-api — JSON, paginated via
    ?page=N. European tech focus; jobs carry ``remote`` and (when provided)
    ``visa_sponsorship`` booleans. Set ARBEITNOW_VISA_ONLY=1 to keep only
    postings that explicitly offer visa sponsorship.
    """
    import os

    board_name = board_cfg["name"]
    base = "https://www.arbeitnow.com/api/job-board-api"
    pages = int(board_cfg.get("pages", 3))
    visa_only = os.environ.get("ARBEITNOW_VISA_ONLY", "").strip() == "1"

    raw: list[dict] = []
    for page in range(1, pages + 1):
        try:
            resp = _pkg.requests.get(base, params={"page": page}, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [{board_name}] Arbeitnow ERROR page {page}: {e}")
            break
        batch = data.get("data") or []
        raw.extend(batch)
        if not batch or not (data.get("links") or {}).get("next"):
            break

    total = len(raw)
    if visa_only:
        raw = [j for j in raw if j.get("visa_sponsorship") is True]
        print(f"  [{board_name}] visa-only filter: {len(raw)} of {total} offer sponsorship")

    board_blacklist = board_cfg.get("board_blacklist", [])
    filtered = _blacklist_filter(
        raw,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["title"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs = []
    for j in filtered:
        title = (j.get("title") or "").strip()
        org = (j.get("company_name") or "").strip()
        if not title or not org or _is_generic_pipeline_title(title):
            continue

        loc = (j.get("location") or "").strip()
        if j.get("remote"):
            loc = f"Remote, {loc}" if loc else "Remote"

        desc_html = j.get("description") or ""
        full_description = _html_to_multiline(desc_html)
        meta = []
        if j.get("visa_sponsorship") is True:
            meta.append("Visa sponsorship: yes")
        tags = ", ".join(j.get("tags") or [])
        if tags:
            meta.append(f"Tags: {tags}")
        job_types = ", ".join(j.get("job_types") or [])
        if job_types:
            meta.append(f"Type: {job_types}")
        if meta:
            full_description = full_description + "\n\n" + " | ".join(meta)

        jobs.append(
            {
                "title": title,
                "location": loc,
                "department": "",
                "url": j.get("url") or "",
                "external_id": j.get("slug")
                or hashlib.md5(f"{org}:{title}".encode()).hexdigest()[:12],
                "snippet": _html_to_snippet(desc_html),
                "full_description": full_description,
                "compensation": "",
                "org_override": org,
                "org_url": board_cfg["url"],
            }
        )

    print(f"  [{board_name}] Arbeitnow: {len(jobs)} relevant from {total} total")
    return jobs


# ---------------------------------------------------------------------------
# Remotive board (free JSON API, remote-only jobs)
# ---------------------------------------------------------------------------


def fetch_remotive_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from the Remotive remote-jobs API (free, no key).

    GET https://remotive.com/api/remote-jobs — Remotive asks for at most a few
    requests per day, so this makes a SINGLE request per run — or, when
    REMOTIVE_CATEGORIES is set (comma list of their category slugs, e.g.
    ``product,marketing``), one request per category.
    """
    import os

    board_name = board_cfg["name"]
    base = "https://remotive.com/api/remote-jobs"

    cats_env = os.environ.get("REMOTIVE_CATEGORIES", "").strip()
    categories = [c.strip() for c in cats_env.split(",") if c.strip()] or [None]

    raw: list[dict] = []
    seen_ids: set = set()
    for cat in categories:
        params = {"category": cat} if cat else {}
        try:
            resp = _pkg.requests.get(base, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [{board_name}] Remotive ERROR (category={cat or 'all'}): {e}")
            continue
        for j in data.get("jobs") or []:
            jid = j.get("id")
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            raw.append(j)

    total = len(raw)
    board_blacklist = board_cfg.get("board_blacklist", [])
    filtered = _blacklist_filter(
        raw,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["title"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs = []
    for j in filtered:
        title = (j.get("title") or "").strip()
        org = (j.get("company_name") or "").strip()
        if not title or not org or _is_generic_pipeline_title(title):
            continue

        # All Remotive jobs are remote; candidate_required_location narrows it
        # ("Europe", "Worldwide", "USA"). Prefix "Remote" so parse_location
        # sets work_mode=remote.
        req_loc = (j.get("candidate_required_location") or "").strip()
        loc = f"Remote, {req_loc}" if req_loc else "Remote"

        desc_html = j.get("description") or ""
        full_description = _html_to_multiline(desc_html)
        meta = []
        if req_loc:
            meta.append(f"Candidate location: {req_loc}")
        if j.get("category"):
            meta.append(f"Category: {j['category']}")
        if j.get("job_type"):
            meta.append(f"Type: {j['job_type']}")
        if meta:
            full_description = full_description + "\n\n" + " | ".join(meta)

        jobs.append(
            {
                "title": title,
                "location": loc,
                "department": j.get("category") or "",
                "url": j.get("url") or "",
                "external_id": str(
                    j.get("id") or hashlib.md5(f"{org}:{title}".encode()).hexdigest()[:12]
                ),
                "snippet": _html_to_snippet(desc_html),
                "full_description": full_description,
                "compensation": j.get("salary") or "",
                "org_override": org,
                "org_url": board_cfg["url"],
            }
        )

    print(f"  [{board_name}] Remotive: {len(jobs)} relevant from {total} total")
    return jobs


# ---------------------------------------------------------------------------
# We Work Remotely board (public RSS per category)
# ---------------------------------------------------------------------------


def fetch_wwr_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from We Work Remotely category RSS feeds (free, no key).

    RSS per category: https://weworkremotely.com/categories/remote-{cat}-jobs.rss
    Categories come from WWR_CATEGORIES (comma list) or the board config
    default. Item titles are "Company: Role"; <region> narrows the remote zone.
    Parsed with stdlib xml.etree — no feedparser dependency.
    """
    import os
    import xml.etree.ElementTree as ET

    board_name = board_cfg["name"]
    cats_env = os.environ.get("WWR_CATEGORIES", "").strip()
    categories = (
        [c.strip() for c in cats_env.split(",") if c.strip()]
        or board_cfg.get("default_categories")
        or ["product", "management-and-finance"]
    )

    headers = {"User-Agent": "Mozilla/5.0 (job-pipeline RSS reader)"}
    raw_items = []
    for cat in categories:
        url = f"https://weworkremotely.com/categories/remote-{cat}-jobs.rss"
        try:
            resp = _pkg.requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            raw_items.extend(root.findall(".//item"))
        except Exception as e:
            print(f"  [{board_name}] WWR RSS ERROR ({cat}): {e}")
            continue

    def _field(item, tag):
        el = item.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    total = len(raw_items)
    board_blacklist = [kw.lower() for kw in board_cfg.get("board_blacklist", [])]

    jobs = []
    seen_links: set = set()
    for item in raw_items:
        raw_title = _field(item, "title")
        link = _field(item, "link") or _field(item, "guid")
        if not raw_title or link in seen_links:
            continue
        seen_links.add(link)

        # "Company: Role" — split on the first colon.
        if ":" in raw_title:
            org, title = (s.strip() for s in raw_title.split(":", 1))
        else:
            org, title = f"[via {board_name}]", raw_title.strip()
        if not title or _is_generic_pipeline_title(title):
            continue

        t_lower = title.lower()
        if any(kw in t_lower for kw in GLOBAL_BLACKLIST_SUBSTR):
            continue
        if any(
            re.search(r"\b" + re.escape(bl.lower()) + r"\b", t_lower) for bl in GLOBAL_BLACKLIST
        ):
            continue
        if any(kw in t_lower for kw in board_blacklist):
            continue

        # WWR is remote-only; <region> narrows the zone
        # ("Anywhere in the World", "Europe Only" ...).
        region = _field(item, "region")
        loc = f"Remote, {region}" if region else "Remote"

        desc_html = _field(item, "description")
        full_description = _html_to_multiline(desc_html)
        category = _field(item, "category")

        jobs.append(
            {
                "title": title,
                "location": loc,
                "department": category,
                "url": link,
                "external_id": hashlib.md5(link.encode()).hexdigest()[:12],
                "snippet": _html_to_snippet(desc_html),
                "full_description": full_description,
                "compensation": "",
                "deadline": _field(item, "expires_at"),
                "org_override": org,
                "org_url": board_cfg["url"],
            }
        )

    print(
        f"  [{board_name}] WWR: {len(jobs)} relevant from {total} total "
        f"(categories: {', '.join(categories)})"
    )
    return jobs


# ---------------------------------------------------------------------------
# HN "Who is hiring?" board (Algolia API, monthly thread)
# ---------------------------------------------------------------------------

_HN_SEPARATOR_RE = re.compile(r"\s*[|—]\s*")  # pipe or em dash


def _parse_hn_comment(comment: dict) -> dict | None:
    """Parse one top-level HN comment into a job dict, or None to skip.

    Convention: first line is "Company | Role | Location | ...". org = text
    before the first | or em dash; title = best-effort second segment.
    """
    text = comment.get("text") or ""
    if not text.strip():
        return None

    plain = _html_to_multiline(text)
    first_line = next((l.strip() for l in plain.splitlines() if l.strip()), "")
    if not first_line:
        return None

    segments = [s.strip() for s in _HN_SEPARATOR_RE.split(first_line) if s.strip()]
    if len(segments) >= 2:
        org = segments[0]
        title = segments[1]
    else:
        # No separator — not the standard posting format; keep the whole line
        # as a best-effort title under the board pseudo-org.
        org = ""
        title = first_line

    # Sanity caps: org names longer than ~80 chars are prose, not a company.
    if len(org) > 80:
        return None
    if len(title) < 4 or len(title) > 200:
        return None

    # Location best-effort: first segment after the title mentioning a work
    # mode; otherwise a "Location: ..." line in the body.
    loc = next(
        (s for s in segments[2:] if re.search(r"(?i)\bremote\b|\bon-?site\b|\bhybrid\b", s)),
        "",
    )
    if not loc:
        m = re.search(r"(?im)^location[:\s]+(.{3,80})$", plain)
        if m:
            loc = m.group(1).strip()

    cid = comment.get("id")
    return {
        "title": title,
        "location": loc[:120],
        "department": "",
        "url": f"https://news.ycombinator.com/item?id={cid}",
        "external_id": str(cid),
        "snippet": first_line[:400],
        "full_description": plain,
        "compensation": "",
        "org_override": org,
    }


def fetch_hn_whoishiring_board(board_cfg: dict) -> list[dict]:
    """Fetch postings from the latest HN "Ask HN: Who is hiring?" thread.

    Finds the newest thread via the Algolia search API, then parses TOP-LEVEL
    comments only (each = one posting). The thread is monthly — the board's
    ttl_days should be ~30 so it is not refetched on every run.
    """
    board_name = board_cfg["name"]
    search_url = "https://hn.algolia.com/api/v1/search_by_date"

    try:
        resp = _pkg.requests.get(
            search_url,
            params={
                "query": '"who is hiring"',
                "tags": "story,author_whoishiring",
                "hitsPerPage": 10,
            },
            timeout=20,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except Exception as e:
        print(f"  [{board_name}] HN search ERROR: {e}")
        return []

    story = next(
        (h for h in hits if re.search(r"(?i)who is hiring", h.get("title") or "")),
        None,
    )
    if not story:
        print(f"  [{board_name}] No 'Who is hiring?' thread found")
        return []

    story_id = story["objectID"]
    print(f"  [{board_name}] Thread: {story.get('title')} (id={story_id})")

    try:
        resp = _pkg.requests.get(f"https://hn.algolia.com/api/v1/items/{story_id}", timeout=30)
        resp.raise_for_status()
        children = resp.json().get("children") or []
    except Exception as e:
        print(f"  [{board_name}] HN items ERROR: {e}")
        return []

    total = len(children)
    parsed = [p for p in (_parse_hn_comment(c) for c in children) if p]

    board_blacklist = board_cfg.get("board_blacklist", [])
    filtered = _blacklist_filter(
        parsed,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["title"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs = []
    for p in filtered:
        if _is_generic_pipeline_title(p["title"]):
            continue
        p["org_override"] = p["org_override"] or f"[via {board_name}]"
        p["org_url"] = board_cfg["url"]
        jobs.append(p)

    print(f"  [{board_name}] HN: {len(jobs)} postings from {total} top-level comments")
    return jobs


# ---------------------------------------------------------------------------
# Idealist board (Algolia search-only key embedded in the page — free, no auth)
# ---------------------------------------------------------------------------

_IDEALIST_APP_ID = "NSV3AUESS7"
# Search-only public key baked into idealist.org's page HTML. If it 403s, refetch
# it from the page: curl https://www.idealist.org/en/jobs | grep searchApiKey
_IDEALIST_SEARCH_KEY = "c2730ea10ab82787f2f3cc961e8c1e06"
_IDEALIST_INDEX = "idealist7-production-published-desc"  # newest-first
_IDEALIST_SITE = "https://www.idealist.org"


def fetch_idealist_board(board_cfg: dict) -> list[dict]:
    """Fetch nonprofit/impact jobs from Idealist via its embedded Algolia key.

    Idealist's React SPA bakes the Algolia appId + search-only key into the page
    HTML — no login, no paid API. POSTs to the Algolia query endpoint.

    board_cfg knobs: ``remote_zone`` (default "WORLD" = globally-open remote),
    ``include_onsite`` (default False), ``max_pages`` (default 20).
    """
    board_name = board_cfg["name"]
    board_blacklist = board_cfg.get("board_blacklist", [])
    max_pages = int(board_cfg.get("max_pages", 20))
    remote_zone = board_cfg.get("remote_zone", "WORLD")
    include_onsite = bool(board_cfg.get("include_onsite", False))

    algolia_url = f"https://{_IDEALIST_APP_ID}-dsn.algolia.net/1/indexes/{_IDEALIST_INDEX}/query"
    headers = {
        "X-Algolia-Application-Id": _IDEALIST_APP_ID,
        "X-Algolia-API-Key": _IDEALIST_SEARCH_KEY,
        "Content-Type": "application/json",
    }
    facet = [["type:JOB"]]
    if not include_onsite:
        facet.append(["locationType:REMOTE"])
        if remote_zone:
            facet.append([f"remoteZone:{remote_zone}"])

    print(
        f"  [{board_name}] Idealist/Algolia: up to {max_pages} pages "
        f"(remote_zone={remote_zone!r}, include_onsite={include_onsite})..."
    )

    all_hits: list[dict] = []
    for page in range(max_pages):
        params_str = "&".join(
            [
                "query=",
                "hitsPerPage=200",
                f"page={page}",
                f"facetFilters={json.dumps(facet)}",
            ]
        )
        try:
            resp = _pkg.requests.post(
                algolia_url,
                data=json.dumps({"params": params_str}),
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"  [{board_name}] Algolia ERROR page {page}: {exc}")
            break
        hits = data.get("hits") or []
        if not hits:
            break
        all_hits.extend(hits)
        if page >= int(data.get("nbPages", 1)) - 1:
            break

    total_seen = len(all_hits)
    for h in all_hits:
        h["_title_proxy"] = h.get("name", "")
    filtered = _blacklist_filter(
        all_hits,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["_title_proxy"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs: list[dict] = []
    for hit in filtered:
        title = (hit.get("name") or "").strip()
        if not title or _is_generic_pipeline_title(title):
            continue
        org = (hit.get("orgName") or "").strip()

        slug = (hit.get("url") or {}).get("en") or ""
        job_url = (_IDEALIST_SITE + slug) if slug else ""

        loc_type = hit.get("locationType") or ""
        zone = hit.get("remoteZone") or ""
        country = hit.get("country") or ""
        city = hit.get("city") or ""
        if loc_type == "REMOTE":
            if zone == "WORLD":
                location = "Remote (worldwide)"
            elif zone == "COUNTRY" and country:
                location = f"Remote ({country})"
            else:
                location = "Remote"
        else:
            location = ", ".join(p for p in (city, country) if p) or loc_type

        functions = hit.get("functions") or []
        keywords = hit.get("keywords") or []
        department = ", ".join((keywords or functions)[:3])

        raw_desc = hit.get("description") or ""
        snippet = _html_to_snippet(raw_desc)
        full_description = _html_to_multiline(raw_desc)
        meta = []
        if hit.get("areasOfFocus"):
            meta.append(f"Areas of focus: {', '.join(hit['areasOfFocus'][:5])}")
        if functions:
            meta.append(f"Functions: {', '.join(functions)}")
        level = hit.get("professionalLevel") or ""
        if level and level != "NONE":
            meta.append(f"Level: {level}")
        if meta:
            full_description = full_description + "\n\n" + " | ".join(meta)

        def _as_num(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        sal_min, sal_max = _as_num(hit.get("salaryMinimum")), _as_num(hit.get("salaryMaximum"))
        currency = hit.get("salaryCurrency") or "USD"
        period = (hit.get("salaryPeriod") or "YEAR").lower()
        compensation = ""
        if sal_min and sal_max:
            compensation = (
                f"{currency} {sal_min:,.0f}/{period}"
                if sal_min == sal_max
                else f"{currency} {sal_min:,.0f}-{sal_max:,.0f}/{period}"
            )

        org_slug = (hit.get("orgUrl") or {}).get("en") or ""
        org_url = (_IDEALIST_SITE + org_slug) if org_slug else board_cfg["url"]

        external_id = hit.get("objectID") or hashlib.md5(f"{org}:{title}".encode()).hexdigest()[:12]

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": department,
                "url": job_url,
                "external_id": external_id,
                "snippet": snippet,
                "full_description": full_description,
                "compensation": compensation,
                "org_override": org,
                "org_url": org_url,
            }
        )

    print(f"  [{board_name}] Idealist: {len(jobs)} relevant from {total_seen} total")
    return jobs


# ---------------------------------------------------------------------------
# Fast Forward board (Getro-hosted tech-nonprofit board — free, no auth)
# ---------------------------------------------------------------------------

_GETRO_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_fastforward_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from Fast Forward's tech-nonprofit board (jobs.ffwd.org).

    Platform is Getro (collection 997). Two free, unauthenticated endpoints:
      * POST api.getro.com/api/v2/collections/997/search/jobs  (paginated list)
      * GET  api.getro.com/api/v1/jobs/{slug}?collection_id=997 (full HTML desc)

    ``fetch_descriptions`` defaults False (listing-only, like Impactpool) so the
    run does not make ~1.5k per-job requests; the enrich pass fills descriptions.
    """
    network_id = int(board_cfg.get("getro_collection_id", 997))
    search_url = f"https://api.getro.com/api/v2/collections/{network_id}/search/jobs"
    detail_tpl = f"https://api.getro.com/api/v1/jobs/{{slug}}?collection_id={network_id}"
    max_pages = int(board_cfg.get("max_pages", 20))
    fetch_desc = bool(board_cfg.get("fetch_descriptions", False))

    board_name = board_cfg["name"]
    board_blacklist = board_cfg.get("board_blacklist", [])
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _GETRO_UA,
    }

    raw_listings: list[dict] = []
    seen_ids: set = set()
    for page in range(max_pages):
        payload = {"hits_per_page": 100, "page": page, "filters": "", "query": ""}
        try:
            resp = _pkg.requests.post(search_url, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"  [{board_name}] page {page} ERROR: {exc}")
            break
        batch = (data.get("results") or {}).get("jobs") or []
        if not batch:
            break
        for j in batch:
            jid = j.get("id")
            if jid is None or jid in seen_ids:
                continue
            seen_ids.add(jid)
            raw_listings.append(j)
        if len(seen_ids) >= int((data.get("results") or {}).get("count", 0)):
            break

    filtered = _blacklist_filter(
        raw_listings,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["title"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs: list[dict] = []
    for j in filtered:
        title = (j.get("title") or "").strip()
        org = ((j.get("organization") or {}).get("name") or "").strip()
        if not title or not org or _is_generic_pipeline_title(title):
            continue

        # Getro lists the literal "Remote" as a pseudo-location; drop it and
        # keep the first real place name so remote roles keep their city.
        locs = j.get("locations") or []
        real_locs = [l for l in locs if l and l.lower() != "remote"]
        location = real_locs[0] if real_locs else ""
        if j.get("work_mode") == "remote":
            location = f"Remote, {location}" if location else "Remote"

        comp_min = j.get("compensation_amount_min_cents")
        comp_max = j.get("compensation_amount_max_cents")
        currency = j.get("compensation_currency") or "USD"
        period = j.get("compensation_period") or ""
        compensation = ""
        if j.get("compensation_public") and comp_min and comp_max:
            label = {"year": "/yr", "month": "/mo", "hour": "/hr"}.get(period, f"/{period}")
            compensation = f"{currency} {comp_min / 100:,.0f}-{comp_max / 100:,.0f}{label}"

        slug = (j.get("slug") or "").strip()
        snippet = full_description = ""
        if fetch_desc and j.get("has_description") and slug:
            try:
                dresp = _pkg.requests.get(
                    detail_tpl.format(slug=slug),
                    headers={"Accept": "application/json", "User-Agent": _GETRO_UA},
                    timeout=15,
                )
                if dresp.status_code == 200:
                    desc_html = (dresp.json() or {}).get("description") or ""
                    full_description = _html_to_multiline(desc_html)
                    snippet = _html_to_snippet(desc_html)
            except Exception:
                pass

        org_slug = (j.get("organization") or {}).get("slug") or ""
        org_url = f"https://jobs.ffwd.org/companies/{org_slug}" if org_slug else board_cfg["url"]
        jid = j.get("id")
        external_id = str(jid) if jid else hashlib.md5(f"{org}:{title}".encode()).hexdigest()[:12]

        jobs.append(
            {
                "title": title,
                "location": location,
                "department": "",
                "url": (j.get("url") or "").strip(),
                "external_id": external_id,
                "snippet": snippet,
                "full_description": full_description,
                "compensation": compensation,
                "org_override": org,
                "org_url": org_url,
            }
        )

    print(
        f"  [{board_name}] Fast Forward/Getro: {len(jobs)} relevant from {len(raw_listings)} total"
    )
    return jobs


# ---------------------------------------------------------------------------
# LinkedIn board (public jobs-guest API, no auth) — merges old LinkedIn +
# LinkedIn Non-profits into ONE board driven by a configurable query set.
# ---------------------------------------------------------------------------

_LINKEDIN_GUEST = "https://www.linkedin.com/jobs-guest/jobs/api"
_LINKEDIN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_LI_CARD_RE = re.compile(r"<li>.*?</li>", re.S)


def _parse_linkedin_card(card: str) -> dict | None:
    """Extract one LinkedIn guest job card into a raw record (or None)."""
    m = re.search(r"urn:li:jobPosting:(\d+)", card)
    jid = m.group(1) if m else None
    href_m = re.search(r'base-card__full-link[^>]*href="([^"]+)"', card) or re.search(
        r'href="([^"]*?/jobs/view/[^"]+)"', card
    )
    href = (href_m.group(1) if href_m else "").replace("&amp;", "&").split("?", 1)[0]
    if not jid:
        jm = re.search(r"/jobs/view/[^\"?]*?-(\d+)(?:[/?]|$)", href)
        jid = jm.group(1) if jm else None
    if not jid:
        return None
    title_m = re.search(r'base-search-card__title">(.*?)</h3>', card, re.S)
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_m.group(1))).strip() if title_m else ""
    org_m = re.search(
        r'base-search-card__subtitle">.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', card, re.S
    )
    org_url = org_m.group(1).replace("&amp;", "&").split("?", 1)[0] if org_m else ""
    org = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", org_m.group(2))).strip() if org_m else ""
    loc_m = re.search(r'job-search-card__location">(.*?)</span>', card, re.S)
    loc = re.sub(r"\s+", " ", loc_m.group(1)).strip() if loc_m else ""
    return {
        "title": title,
        "org": org,
        "org_url": org_url,
        "location": loc,
        "url": href,
        "external_id": jid,
    }


def fetch_linkedin_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from LinkedIn's public *guest* API (free, no login).

    Uses the unauthenticated endpoints JobSpy relies on: a listing search that
    returns HTML job cards, and a per-job detail page for the description.
    The nonprofit/impact focus is just extra entries in ``board_cfg["queries"]``
    (list of {keywords, location}) — this is the merged "LinkedIn" +
    "LinkedIn Non-profits" board, not two boards.

    LinkedIn throttles hard (429 after ~10 rapid requests) so every request is
    spaced by ``request_delay`` seconds with back-off on 429.
    """
    board_name = board_cfg["name"]
    list_url = f"{_LINKEDIN_GUEST}/seeMoreJobPostings/search"
    queries = board_cfg.get("queries") or []
    pages = int(board_cfg.get("pages", 2))
    delay = float(board_cfg.get("request_delay", 3.0))
    want_detail = bool(board_cfg.get("fetch_detail", True))
    board_blacklist = board_cfg.get("board_blacklist", [])
    headers = {
        "User-Agent": _LINKEDIN_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _get(url, params=None):
        for attempt in range(3):
            try:
                resp = _pkg.requests.get(url, params=params, headers=headers, timeout=20)
            except Exception as e:
                print(f"  [{board_name}] request error: {e}")
                return None
            if resp.status_code == 429:
                wait = delay * (attempt + 2)
                print(f"  [{board_name}] 429 throttled, sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                return None
            return resp.text
        return None

    def _fetch_detail(jid):
        html = _get(f"{_LINKEDIN_GUEST}/jobPosting/{jid}")
        time.sleep(delay)
        if not html:
            return ""
        m = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", html, re.S) or re.search(
            r"description__text[^>]*>(.*?)</section>", html, re.S
        )
        return m.group(1) if m else ""

    raw: list[dict] = []
    seen_ids: set = set()
    for q in queries:
        kw = (q.get("keywords") or "").strip()
        loc = (q.get("location") or "").strip()
        if not kw:
            continue
        for page in range(pages):
            html = _get(list_url, params={"keywords": kw, "location": loc, "start": page * 25})
            time.sleep(delay)
            if not html:
                break
            cards = _LI_CARD_RE.findall(html)
            if not cards:
                break
            for card in cards:
                rec = _parse_linkedin_card(card)
                if not rec or not rec["title"] or not rec["org"]:
                    continue
                if rec["external_id"] in seen_ids:
                    continue
                seen_ids.add(rec["external_id"])
                raw.append(rec)

    total = len(raw)
    filtered = _blacklist_filter(
        raw,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["title"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs = []
    for rec in filtered:
        if _is_generic_pipeline_title(rec["title"]):
            continue
        desc_html = _fetch_detail(rec["external_id"]) if want_detail else ""
        snippet = _html_to_snippet(desc_html) if desc_html else rec["title"]
        full_description = _html_to_multiline(desc_html) if desc_html else snippet
        jobs.append(
            {
                "title": rec["title"],
                "location": rec["location"],
                "department": "",
                "url": rec["url"],
                "external_id": rec["external_id"],
                "snippet": snippet,
                "full_description": full_description,
                "compensation": "",
                "org_override": rec["org"],
                "org_url": rec["org_url"] or board_cfg["url"],
            }
        )

    print(f"  [{board_name}] LinkedIn guest: {len(jobs)} relevant from {total} total")
    return jobs
