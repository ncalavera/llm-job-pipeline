"""Vacancy fetchers: Greenhouse API, Workday API, Firecrawl scraper, markdown parser."""

import hashlib
import html as html_module
import json
import re
import time
import urllib.parse
import urllib.request

import fetchers as _pkg
from fetchers.html_utils import (
    _html_to_multiline,
    _html_to_snippet,
)
from fetchers.firecrawl import fetch_firecrawl_scrape
from fetchers.parsing import (
    _blacklist_filter,
    _extract_org_from_listing,
    _is_generic_pipeline_title,
)
from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR


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
