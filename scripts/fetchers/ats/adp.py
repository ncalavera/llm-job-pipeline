"""ADP Workforce Now — public job-requisitions JSON feed (free, no auth)."""

import hashlib

from fetchers import http
from fetchers.http import _LOCAL_UA
from fetchers.registry import company_fetcher, register_company


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


@company_fetcher
def fetch_adp_json(org_name: str, config: dict) -> list[dict]:
    """Fetch jobs from the ADP Workforce Now public requisitions feed (free, no auth).

    Endpoint: ``workforcenow.adp.com/mascsr/default/careercenter/public/events/
    staffing/v1/job-requisitions?cid=<CID>`` where ``cid`` comes from config.
    Some portals (e.g. Skoll) return an empty feed unless the career-center id
    ``ccId`` is also passed; set ``ats_config.ccId`` to append it.
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
    cc_id = ((config.get("ats_config") or {}).get("ccId") or config.get("ccId") or "").strip()
    if cc_id:
        url += f"&ccId={cc_id}&lang=en_US"
    print(f"  [{org_name}] ADP Workforce Now: {url}")
    resp = http.get(
        url, headers={"Accept": "application/json", "User-Agent": _LOCAL_UA}, timeout=20
    )
    data = resp.json()

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


@register_company("adp_json")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_adp_json(org_name, config)
