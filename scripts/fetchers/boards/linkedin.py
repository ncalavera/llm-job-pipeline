"""LinkedIn board (public jobs-guest API, no auth) — merges old LinkedIn +
LinkedIn Non-profits into ONE board driven by a configurable query set."""

import re
import time

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_multiline, _html_to_snippet
from fetchers.http import FetchError
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher

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


@board_fetcher("linkedin_guest")
def fetch_linkedin_board(board_cfg: dict) -> list[dict]:
    """Fetch jobs from LinkedIn's public *guest* API (free, no login).

    Uses the unauthenticated endpoints JobSpy relies on: a listing search that
    returns HTML job cards, and a per-job detail page for the description.
    The nonprofit/impact focus is just extra entries in ``board_cfg["queries"]``
    (list of {keywords, location}) — this is the merged "LinkedIn" +
    "LinkedIn Non-profits" board, not two boards.

    LinkedIn throttles hard (429 after ~10 rapid requests) so every request is
    spaced by ``request_delay`` seconds with back-off on 429.

    Queries come from the user profile, never a shipped default: an explicit
    query set in ``board_cfg["queries"]`` (a one-off override) wins; otherwise
    they are resolved from the profile's target roles + geography via
    ``profile_targeting.resolve_linkedin_queries`` (STRATEGY guardrail 1).
    """
    board_name = board_cfg["name"]
    list_url = f"{_LINKEDIN_GUEST}/seeMoreJobPostings/search"
    queries = board_cfg.get("queries")
    if not queries:
        from profile_targeting import resolve_linkedin_queries

        queries = resolve_linkedin_queries()
    if not queries:
        print(
            f"  [{board_name}] no LinkedIn queries — add a ## TARGET_ROLES (or an explicit "
            "## LINKEDIN_QUERIES) section to config/user_profile.md, then re-run"
        )
        return []
    pages = int(board_cfg.get("pages", 2))
    delay = float(board_cfg.get("request_delay", 3.0))
    want_detail = bool(board_cfg.get("fetch_detail", True))
    board_blacklist = board_cfg.get("board_blacklist", [])
    headers = {
        "User-Agent": _LINKEDIN_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_error: list[Exception] = []

    def _get(url, params=None):
        for attempt in range(3):
            try:
                resp = http.get(url, params=params, headers=headers, timeout=20, check=False)
            except FetchError as e:
                print(f"  [{board_name}] request error: {e}")
                last_error.append(e)
                return None
            if resp.status_code == 429:
                wait = delay * (attempt + 2)
                print(f"  [{board_name}] 429 throttled, sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                last_error.append(FetchError(f"http_{resp.status_code}", url))
                return None
            return resp.text
        last_error.append(FetchError("http_429", "throttled after 3 attempts"))
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

    if not raw and last_error:
        raise last_error[-1]  # every request failed — let the boundary record why

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
