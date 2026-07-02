"""HN "Who is hiring?" board (Algolia API, monthly thread)."""

import re

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_multiline
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher

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


@board_fetcher("hn_whoishiring")
def fetch_hn_whoishiring_board(board_cfg: dict) -> list[dict]:
    """Fetch postings from the latest HN "Ask HN: Who is hiring?" thread.

    Finds the newest thread via the Algolia search API, then parses TOP-LEVEL
    comments only (each = one posting). The thread is monthly — the board's
    ttl_days should be ~30 so it is not refetched on every run.
    """
    board_name = board_cfg["name"]
    search_url = "https://hn.algolia.com/api/v1/search_by_date"

    resp = http.get(
        search_url,
        params={
            "query": '"who is hiring"',
            "tags": "story,author_whoishiring",
            "hitsPerPage": 10,
        },
        timeout=20,
    )
    hits = resp.json().get("hits", [])

    story = next(
        (h for h in hits if re.search(r"(?i)who is hiring", h.get("title") or "")),
        None,
    )
    if not story:
        print(f"  [{board_name}] No 'Who is hiring?' thread found")
        return []

    story_id = story["objectID"]
    print(f"  [{board_name}] Thread: {story.get('title')} (id={story_id})")

    resp = http.get(f"https://hn.algolia.com/api/v1/items/{story_id}", timeout=30)
    children = resp.json().get("children") or []

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
