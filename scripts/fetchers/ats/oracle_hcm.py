"""Oracle HCM Recruiting Cloud — public candidate-experience REST API (free, no auth).

Careers sites of the shape
``<host>.oraclecloud.com/hcmUI/CandidateExperience/en/sites/<SITE>/...`` render
their listing from JavaScript, so a scrape saves the navigation shell instead of
the job. The same site serves two unauthenticated REST endpoints — a paged
requisition search and a per-requisition detail — which is what this adapter
reads. Unblocks UNDP (site ``CX_1`` on ``estm.fa.em2.oraclecloud.com``).
"""

import re
import urllib.parse

from fetchers import http
from fetchers.html_utils import _html_to_snippet, _html_to_text
from fetchers.http import FetchError, _LOCAL_UA
from fetchers.registry import company_fetcher, register_company

_PAGE_SIZE = 50
# Hard page cap: an unkeyworded UN-scale site lists hundreds of roles and every
# one of them costs a detail GET. 20 pages × 50 = 1000 mirrors the SF ceiling.
_MAX_PAGES = 20


def _oracle_config(config: dict) -> tuple[str, str, str]:
    """Parse (host, site number, keyword) from the configured careers URL.

    ``keyword`` falls back to the URL's own ``?keyword=`` — a site's careers URL
    is normally copied straight out of the browser with the filter already on.
    """
    url = (config.get("url") or config.get("careers_url") or "").strip()
    m_host = re.match(r"(https?://[^/]+)", url)
    m_site = re.search(r"/sites/([^/?#]+)", url)
    keyword = (config.get("keyword") or "").strip()
    if not keyword:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        keyword = (query.get("keyword") or [""])[0].strip()
    return (m_host.group(1) if m_host else ""), (m_site.group(1) if m_site else ""), keyword


def _oracle_get(host: str, resource: str, expand: str, finder: str) -> dict:
    """GET one candidate-experience REST resource. The finder's inner quotes are
    literal and must survive as ``%22``, so the finder is encoded on its own."""
    url = (
        f"{host}/hcmRestApi/resources/latest/{resource}"
        f"?onlyData=true&expand={expand}"
        f"&finder={urllib.parse.quote(finder, safe=';,=')}"
    )
    return http.get(url, headers={"User-Agent": _LOCAL_UA}, timeout=30).json()


def _oracle_location(src: dict) -> str:
    """Join primary + secondary locations, with the workplace type appended so
    ``parse_location`` can see a remote/hybrid role."""
    names = [(src.get("PrimaryLocation") or "").strip()]
    names += [
        (loc.get("Name") or "").strip()
        for loc in (src.get("secondaryLocations") or [])
        if isinstance(loc, dict)
    ]
    location = " | ".join(dict.fromkeys(n for n in names if n))
    workplace = (src.get("WorkplaceType") or "").strip()
    if not workplace:
        return location
    return f"{location} ({workplace})" if location else workplace


def _oracle_description(src: dict) -> str:
    """Flatten the HTML description blocks and append the flex fields.

    The flex fields (Agency, Grade, Vacancy Type, Contract Duration, Required
    Languages, …) live outside the description HTML, but they carry exactly what
    the hard filters and the scorer need — so they go into the text as
    ``Prompt: Value`` lines.
    """
    parts = [
        _html_to_text(src.get(key) or "")
        for key in (
            "ExternalDescriptionStr",
            "ExternalResponsibilitiesStr",
            "ExternalQualificationsStr",
        )
    ]
    for field in src.get("requisitionFlexFields") or []:
        if not isinstance(field, dict):
            continue
        prompt = (field.get("Prompt") or "").strip()
        value = (field.get("Value") or "").strip()
        if prompt and value:
            parts.append(f"{prompt}: {value}")
    return "\n".join(p for p in parts if p)


def _oracle_detail(org_name: str, host: str, site: str, job_id: str) -> dict:
    """Fetch one requisition's detail record. A single failed job must not kill
    the whole listing, so the error is printed and the row falls back to its
    search-result fields."""
    try:
        data = _oracle_get(
            host,
            "recruitingCEJobRequisitionDetails",
            "all",
            f'ById;Id="{job_id}",siteNumber="{site}"',
        )
    except FetchError as exc:
        print(f"  [{org_name}] Oracle HCM detail {job_id} failed: {exc.reason} — {exc.detail}")
        return {}
    items = data.get("items") or []
    return items[0] if items and isinstance(items[0], dict) else {}


@company_fetcher
def fetch_oracle_hcm(org_name: str, config: dict) -> list[dict]:
    """Fetch jobs from an Oracle HCM Recruiting Cloud careers site (free, no auth).

    Config: ``url`` — any careers URL of the site, e.g.
    ``https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs?keyword=accelerator%20lab``
    (host and site number are parsed out of it); optional ``keyword`` overrides
    the URL's own filter. Without a keyword the whole site lists, which on UNDP
    is hundreds of unrelated roles — keep the filter.
    """
    host, site, keyword = _oracle_config(config)
    if not host or not site:
        print(f"  [{org_name}] Oracle HCM: url must contain a host and /sites/<SITE>")
        return []
    print(f"  [{org_name}] Oracle HCM: {host} site={site} keyword={keyword or '(none)'}")

    requisitions: list[dict] = []
    seen: set[str] = set()
    for page in range(_MAX_PAGES):
        finder = (
            f'findReqs;siteNumber="{site}"'
            + (f',keyword="{keyword}"' if keyword else "")
            + f',limit="{_PAGE_SIZE}",offset="{page * _PAGE_SIZE}",sortBy="POSTING_DATES_DESC"'
        )
        items = _oracle_get(
            host, "recruitingCEJobRequisitions", "requisitionList.secondaryLocations", finder
        ).get("items") or [{}]
        page_reqs = items[0].get("requisitionList") or []
        total = items[0].get("TotalJobsCount")
        for req in page_reqs:
            job_id = str(req.get("Id") or "")
            if job_id and job_id not in seen:
                seen.add(job_id)
                requisitions.append(req)
        if not page_reqs or (isinstance(total, int) and len(requisitions) >= total):
            break

    jobs = []
    for req in requisitions:
        job_id = str(req.get("Id") or "")
        # Detail nulls must not blank a value the search result already carried.
        src = {
            **req,
            **{k: v for k, v in _oracle_detail(org_name, host, site, job_id).items() if v},
        }
        full_desc = _oracle_description(src)
        jobs.append(
            {
                "title": (src.get("Title") or "").strip(),
                "location": _oracle_location(src),
                "department": (src.get("Department") or "").strip(),
                "url": f"{host}/hcmUI/CandidateExperience/en/sites/{site}/requisitions/job/{job_id}",
                "external_id": job_id,
                "snippet": _html_to_snippet(full_desc)
                if full_desc
                else (src.get("ShortDescriptionStr") or ""),
                "full_description": full_desc,
                "deadline": src.get("ExternalPostedEndDate") or src.get("PostingEndDate") or "",
            }
        )

    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


@register_company("oracle_hcm")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_oracle_hcm(org_name, config)
