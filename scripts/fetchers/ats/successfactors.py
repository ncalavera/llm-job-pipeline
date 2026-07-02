"""SAP SuccessFactors — Career Site Builder tile-search feed (free)."""

import hashlib
import html as html_module
import re
import urllib.parse

from fetchers import http
from fetchers.http import FetchError, _LOCAL_UA
from fetchers.registry import company_fetcher, register_company


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


@company_fetcher
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
        boot = http.get(search_url, headers={"User-Agent": _LOCAL_UA}, timeout=20)
        cookies = boot.cookies
    except FetchError as e:
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
            resp = http.get(page_url, headers=headers, cookies=cookies, timeout=20)
            text = resp.text
        except FetchError as e:
            print(f"  [{org_name}] SuccessFactors page error: {e}")
            if not jobs:
                raise  # total failure — let the boundary record the reason
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


@register_company("successfactors")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_successfactors(org_name, config)
