"""Arbeitnow board (free JSON API, European tech, visa/remote flags)."""

import hashlib

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_multiline, _html_to_snippet
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher


@board_fetcher("arbeitnow_api")
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
    last_error = None
    for page in range(1, pages + 1):
        try:
            resp = http.get(base, params={"page": page}, timeout=20)
            data = resp.json()
        except Exception as e:
            print(f"  [{board_name}] Arbeitnow ERROR page {page}: {e}")
            last_error = e
            break
        batch = data.get("data") or []
        raw.extend(batch)
        if not batch or not (data.get("links") or {}).get("next"):
            break

    if not raw and last_error is not None:
        raise last_error  # total failure — let the boundary record the reason

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
