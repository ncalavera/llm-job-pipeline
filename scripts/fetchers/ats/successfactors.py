"""SAP SuccessFactors — Career Site Builder tile-search feed (free)."""

import hashlib
import html as html_module
import re
import urllib.parse
import xml.etree.ElementTree as ET

from fetchers import http
from fetchers.html_utils import _html_to_snippet, _html_to_text
from fetchers.http import FetchError, _LOCAL_UA
from fetchers.registry import company_fetcher, register_company

_SITEMAP_NS = {"sm": "http://www.google.com/schemas/sitemap/0.9"}


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


def _sf_sitemap_job_urls(base: str) -> list[str]:
    """Return the job URLs listed in the host's SEO ``sitemap.xml``.

    Some SuccessFactors hosts serve a live, auto-generated sitemap even when
    their Career Site Builder ``tile-search-results`` endpoint returns an
    empty shell — e.g. ILO, whose actual candidate-facing backend is the
    classic Recruiting (RCM) UI rather than CSB. The sitemap stays accurate
    because it is generated straight from the same job-requisition data.
    """
    resp = http.get(f"{base}/sitemap.xml", headers={"User-Agent": _LOCAL_UA}, timeout=20)
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return []
    return [
        loc.strip()
        for el in root.findall("sm:url", _SITEMAP_NS)
        if (loc := (el.findtext("sm:loc", namespaces=_SITEMAP_NS) or "").strip())
    ]


def _sf_job_detail(url: str) -> dict | None:
    """Fetch one SF RCM job detail page and pull it via the page's
    schema.org ``JobPosting`` microdata (``itemprop="title"``/``"description"``).

    The description is split across several ``itemprop="description"`` spans
    (one per content block: header, body, conditions of employment, …), all
    nested inside a single ``<div class="job">`` container — so instead of
    chasing each span's own boundary, this grabs that container's full
    (depth-balanced) inner HTML, strips embedded ``<style>``/``<script>``
    blocks, and flattens the rest to text.
    """
    try:
        resp = http.get(url, headers={"User-Agent": _LOCAL_UA}, timeout=20)
    except FetchError:
        return None
    page = resp.text

    m_title = re.search(r'itemprop="title"[^>]*>\s*(.*?)\s*<', page, re.DOTALL)
    title = html_module.unescape(m_title.group(1)).strip() if m_title else ""
    if not title:
        return None

    full_desc = ""
    m_job_div = re.search(r'<div class="job">', page)
    if m_job_div:
        inner = _extract_balanced_div(page, m_job_div.end())
        inner = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", inner)
        full_desc = _html_to_text(inner)

    m_id = re.search(r"/(\d+)/?$", url)
    external_id = m_id.group(1) if m_id else hashlib.md5(url.encode()).hexdigest()[:12]

    return {
        "title": title,
        "location": "",
        "department": "",
        "url": url,
        "external_id": external_id,
        "snippet": _html_to_snippet(full_desc) if full_desc else "",
        "full_description": full_desc,
    }


def _extract_balanced_div(html: str, content_start: int) -> str:
    """Return the inner HTML of a ``<div ...>`` whose opening tag ends at
    ``content_start``, up to its matching closing ``</div>`` (a simple
    depth counter — good enough for well-formed adapter-controlled HTML,
    no need for a full parser dependency)."""
    depth = 1
    for m in re.finditer(r"<(/?)div\b", html[content_start:], re.IGNORECASE):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return html[content_start : content_start + m.start()]
    return html[content_start:]


def _fetch_successfactors_sitemap(org_name: str, base: str) -> list[dict]:
    """Backend variant for SF hosts whose CSB tile-search returns nothing:
    walk the SEO sitemap.xml and fetch each job's detail page directly."""
    urls = _sf_sitemap_job_urls(base)
    jobs: list[dict] = []
    for url in urls:
        job = _sf_job_detail(url)
        if job:
            jobs.append(job)
    return jobs


@company_fetcher
def fetch_successfactors(org_name: str, config: dict) -> list[dict]:
    """Fetch jobs from a SAP SuccessFactors company career site (free).

    Two backends, selected by ``config["sf_backend"]``:

    * default ("csb") — Career Site Builder tile feed at
      ``<base>/tile-search-results/?q=&startrow=<N>``, paginated by
      ``startrow`` (25 tiles/page). The tile endpoint returns an empty shell
      without a session cookie, so we GET the search page first to establish
      one. Unblocks reframe[Tech], Bertelsmann Stiftung, Robert Bosch (WS3/U4).
    * "sitemap" — for hosts where the CSB tile feed is a dead end (e.g. ILO,
      whose live backend is the classic RCM UI, not CSB): walk
      ``<base>/sitemap.xml`` and fetch each job detail page directly. See
      :func:`_fetch_successfactors_sitemap`.

    ``base`` comes from config via :func:`_sf_base_url`, so both the
    ``createyourowncareer.com/<Site>`` host and a directly configured
    ``config["url"]`` backend host are supported either way.
    """
    base = _sf_base_url(config)
    if not base:
        print(f"  [{org_name}] SuccessFactors: no url/ats_slug configured")
        return []

    if config.get("sf_backend") == "sitemap":
        print(f"  [{org_name}] SuccessFactors (sitemap backend): {base}/sitemap.xml")
        jobs = _fetch_successfactors_sitemap(org_name, base)
        print(f"  [{org_name}] Found {len(jobs)} vacancies")
        return jobs

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
