"""Remotive board (free JSON API, remote-only jobs)."""

import hashlib

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_multiline, _html_to_snippet
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher


@board_fetcher("remotive_api")
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
    last_error = None
    for cat in categories:
        params = {"category": cat} if cat else {}
        try:
            resp = http.get(base, params=params, timeout=20)
            data = resp.json()
        except Exception as e:
            print(f"  [{board_name}] Remotive ERROR (category={cat or 'all'}): {e}")
            last_error = e
            continue
        for j in data.get("jobs") or []:
            jid = j.get("id")
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            raw.append(j)

    if not raw and last_error is not None:
        raise last_error  # every category failed — let the boundary record why

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
