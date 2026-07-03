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
# One guest results page holds up to 25 cards; a shorter page is the last one.
_PAGE_SIZE = 25
# A genuine no-match page is tiny ("<!---->"); anything larger with no parseable
# card is drifted/blocked markup, not an honest empty result.
_EMPTY_PAGE_MAX = 200
# Tolerate <li> with attributes — LinkedIn serves a bare <li> today but has
# shipped attributed cards before; either way one card = one <li>…</li>.
_LI_CARD_RE = re.compile(r"<li\b[^>]*>.*?</li>", re.S)
# Markers only a bot-wall / challenge page carries. A real results fragment and
# a genuine no-match page carry NONE of these, so an empty result stays honest.
# Checked ONLY on the listing endpoint — a normal job DETAIL page legitimately
# links to /uas/login and session_redirect, which must never read as a block.
_BLOCK_MARKERS = ("authwall", "captcha", "unusual traffic", "security verification")


def _looks_blocked(html: str) -> bool:
    """True when a 200 body is really an auth wall / bot challenge.

    Used only on the listing endpoint so a block is recorded as ``error:``
    instead of masquerading as an empty-but-ok run. A genuine
    zero-result page carries no block marker and stays an honest empty.
    """
    if not html:
        return False
    low = html[:4000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


def _card_summary(rec: dict) -> str:
    """A human-readable one-liner built from the listing card's own fields.

    The listing already carries a real title, company and location — enough to
    be worth reading. When the (throttle-prone) detail page can't be fetched
    this becomes the row's snippet (the description is left empty on purpose),
    so a listing survives even when LinkedIn refuses the job description.
    """
    title = (rec.get("title") or "").strip()
    org = (rec.get("org") or "").strip()
    loc = (rec.get("location") or "").strip()
    line = title
    if org:
        line = f"{line} at {org}" if line else org
    if loc:
        line = f"{line} — {loc}" if line else loc
    return line.strip()


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
    title_m = re.search(r"base-search-card__title[^>]*>(.*?)</h3>", card, re.S)
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_m.group(1))).strip() if title_m else ""
    org_m = re.search(
        r'base-search-card__subtitle[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', card, re.S
    )
    if org_m:
        org_url = org_m.group(1).replace("&amp;", "&").split("?", 1)[0]
        org = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", org_m.group(2))).strip()
    else:
        # Subtitle without a company link (the org has no LinkedIn page) — take
        # its plain text so a real card is not dropped for a missing org.
        sub_m = re.search(r"base-search-card__subtitle[^>]*>(.*?)</(?:h4|a|span|div)>", card, re.S)
        org = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", sub_m.group(1))).strip() if sub_m else ""
        org_url = ""
    loc_m = re.search(r"job-search-card__location[^>]*>(.*?)</span>", card, re.S)
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
    # Once LinkedIn throttles/blocks us, every remaining detail request will fail
    # too — stop firing them (politeness) and let the card summary carry the row.
    state = {"stop_detail": False}

    def _get(url, params=None, block_check=False):
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
            if block_check and _looks_blocked(resp.text):
                # A 200 that is really an auth wall / challenge — an honest
                # failure, not an empty result (which carries no block marker).
                last_error.append(
                    FetchError("blocked", f"{url} — LinkedIn guest auth wall/challenge")
                )
                state["stop_detail"] = True
                return None
            return resp.text
        # Exhausted the 429 back-off: record it and stop hammering the detail
        # endpoint for the rest of this run (it will only be throttled too).
        last_error.append(FetchError("http_429", "throttled after 3 attempts"))
        state["stop_detail"] = True
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
        # `pages` is the per-query cap so one query can't fan out into dozens of
        # throttled requests (STRATEGY simplicity + polite pacing).
        for page in range(pages):
            html = _get(
                list_url,
                params={"keywords": kw, "location": loc, "start": page * _PAGE_SIZE},
                block_check=True,
            )
            time.sleep(delay)
            if not html:
                break
            cards = _LI_CARD_RE.findall(html)
            if not cards:
                # A tiny body is a genuine no-match page; a substantial body with
                # no parseable card is drifted/blocked markup — record it so a
                # format break cannot hide as an empty-but-ok run.
                if len(html.strip()) > _EMPTY_PAGE_MAX:
                    last_error.append(
                        FetchError("unparseable", f"{list_url} — no job cards in a non-empty page")
                    )
                break
            for card in cards:
                rec = _parse_linkedin_card(card)
                if not rec or not rec["title"] or not rec["org"]:
                    continue
                # Within-run de-dup only avoids re-fetching the same job's detail
                # twice; the authoritative board de-dup stays in the save layer
                # (STRATEGY guardrail: board de-dup lives ONLY there).
                if rec["external_id"] in seen_ids:
                    continue
                seen_ids.add(rec["external_id"])
                raw.append(rec)
            # The guest feed wraps around past the last real page (a high `start`
            # re-serves page 1), so a short page means the end — stop paging.
            if len(cards) < _PAGE_SIZE:
                break

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
        desc_html = ""
        if want_detail and not state["stop_detail"]:
            desc_html = _fetch_detail(rec["external_id"])
        # No detail (LinkedIn throttles the guest API hard): keep an honest
        # snippet from the card's own fields and leave the description EMPTY.
        # A short summary in full_description (1–49 chars) trips the save-layer
        # "navigation_snippet" junk gate and the row is dropped; an EMPTY
        # description is explicitly allowed through (URL keeps the row), so the
        # listing survives instead of vanishing silently.
        snippet = _html_to_snippet(desc_html) if desc_html else _card_summary(rec)
        full_description = _html_to_multiline(desc_html) if desc_html else ""
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
