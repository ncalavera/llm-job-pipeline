"""SmartRecruiters public Postings API (free, no auth)."""

from fetchers import http
from fetchers.registry import company_fetcher, register_company

_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"


def _sr_location(loc) -> str:
    """Join a SmartRecruiters ``location`` object into a display string."""
    if not isinstance(loc, dict):
        return ""
    full = (loc.get("fullLocation") or "").strip()
    if full:
        return full
    parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
    return ", ".join(p for p in parts if p)


def _sr_department(posting: dict) -> str:
    """Department label, falling back to the job function (department is
    often left blank while function is always set)."""
    dept = ((posting.get("department") or {}).get("label") or "").strip()
    if dept:
        return dept
    return ((posting.get("function") or {}).get("label") or "").strip()


@company_fetcher
def fetch_smartrecruiters(org_name: str, slug: str) -> list[dict]:
    """Fetch jobs from the SmartRecruiters public Postings API (free, no auth).

    Endpoint: ``api.smartrecruiters.com/v1/companies/<slug>/postings``,
    paginated by ``offset``/``limit`` against the response's ``totalFound``.
    ``slug`` is the SmartRecruiters company identifier — the same string
    used in the public ``jobs.smartrecruiters.com/<slug>/...`` posting URLs.
    The list feed carries no job-ad body, so ``full_description`` is left for
    the blind-vacancy enrichment pass.
    """
    if not slug:
        print(f"  [{org_name}] SmartRecruiters: no slug configured")
        return []

    jobs: list[dict] = []
    seen: set[str] = set()
    offset, limit = 0, 100
    for _ in range(20):  # hard page cap, mirrors the SuccessFactors tile loop
        url = f"{_API.format(slug=slug)}?offset={offset}&limit={limit}"
        print(f"  [{org_name}] SmartRecruiters: {url}")
        resp = http.get(url, headers={"Accept": "application/json"}, timeout=20)
        data = resp.json()
        content = data.get("content") or []

        for posting in content:
            if not isinstance(posting, dict):
                continue
            job_id = str(posting.get("id") or "")
            title = (posting.get("name") or "").strip()
            if not job_id or not title or job_id in seen:
                continue
            seen.add(job_id)
            jobs.append(
                {
                    "title": title,
                    "location": _sr_location(posting.get("location")),
                    "department": _sr_department(posting),
                    "url": f"https://jobs.smartrecruiters.com/{slug}/{job_id}",
                    "external_id": job_id,
                    "snippet": "",
                }
            )

        offset += limit
        if offset >= int(data.get("totalFound") or 0) or not content:
            break

    print(f"  [{org_name}] Found {len(jobs)} vacancies")
    return jobs


@register_company("smartrecruiters")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_smartrecruiters(org_name, config.get("slug") or "")
