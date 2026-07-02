"""data.org "Data for Social Impact" board (WordPress aggregator → real ATS links)."""

import html as html_module
import re
import time
import urllib.parse

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_multiline
from fetchers.registry import board_fetcher


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
        resp = http.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
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


@board_fetcher("datadotorg_wp")
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
    last_error = None
    while len(listing) < max_jobs:
        try:
            resp = http.get(
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
            batch = resp.json()
        except Exception as exc:
            print(f"  [{board_name}] listing page {page} ERROR: {exc}")
            last_error = exc
            break
        if not batch:
            break
        listing.extend(batch)
        page += 1
    listing = listing[:max_jobs]

    if not listing and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

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
