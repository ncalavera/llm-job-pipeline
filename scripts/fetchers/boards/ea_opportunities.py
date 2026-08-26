"""EA Opportunities Board (whole board embedded in the page it serves — free, no auth)."""

import json
import re

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR
from fetchers import http
from fetchers.html_utils import _html_to_snippet
from fetchers.parsing import _blacklist_filter, _is_generic_pipeline_title
from fetchers.registry import board_fetcher

_DEFAULT_PAGE_URL = "https://www.effectivealtruism.org/opportunities"

# The page is server-rendered Next.js: every opportunity arrives inside this
# one script tag, so there is no pagination and no cap to work around.
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

# The board lists more than employment — it also carries grant calls, reading
# groups, conferences and career-advising slots. Those are dropped here because
# they are not vacancies at all, which is a different judgment from whether a
# vacancy suits anyone: unpaid and junior WORK (Volunteer, Internship,
# Fellowship, Part-time) stays and goes to scoring like every other row.
# A row survives when any one of its types is employment, so something tagged
# both Fellowship and Course still comes through.
_NON_VACANCY_TYPES = {"funding", "course", "event", "contest", "advising", "independent project"}


@board_fetcher("ea_opportunities_next_data")
def fetch_ea_opportunities_board(board_cfg: dict) -> list[dict]:
    """Fetch opportunities from the EA Opportunities Board.

    Airtable behind it, no public API, and the JSON twin at
    ``/_next/data/<buildId>/opportunities.json`` is deliberately not used: the
    buildId changes on every deploy of their site and a stale one answers 404.
    The page address never changes, and it carries the whole board (~1000 rows,
    ~700 KB gzipped), so one request is the complete source.

    Uncapped on purpose. The board is smaller than Probably Good, which is also
    uncapped, and its rows arrive newest-first with no relevance ranking, so a
    cap would only trade completeness for nothing. Repeat rows cost nothing —
    the board is deduplicated against what the database already holds.

    board_cfg knobs: ``page_url`` (overridable if the board moves).
    """
    board_name = board_cfg["name"]
    page_url = board_cfg.get("page_url", _DEFAULT_PAGE_URL)
    board_blacklist = board_cfg.get("board_blacklist", [])

    print(f"  [{board_name}] EA Opportunities: fetching {page_url}...")
    resp = http.get(page_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    match = _NEXT_DATA.search(resp.text)
    if not match:
        # Refusal ≠ empty: a board that changed shape must not read as "no jobs".
        raise ValueError(
            f"{board_name}: no __NEXT_DATA__ in {len(resp.text)} chars of page "
            f"({page_url}) — the board changed shape"
        )
    props = json.loads(match.group(1))["props"]["pageProps"]
    raw = props.get("opportunities") or []
    total_count = props.get("totalCount")
    if total_count and len(raw) < total_count:
        print(
            f"  [{board_name}] EA Opportunities: page carried {len(raw)} of "
            f"{total_count} opportunities — remaining rows skipped this run"
        )

    total_seen = len(raw)
    vacancies = [r for r in raw if _is_vacancy(r)]

    for r in vacancies:
        r["_title_proxy"] = r.get("title", "")
    filtered = _blacklist_filter(
        vacancies,
        GLOBAL_BLACKLIST + board_blacklist,
        title_fields=["_title_proxy"],
        substr_blacklist=GLOBAL_BLACKLIST_SUBSTR,
    )

    jobs: list[dict] = []
    for row in filtered:
        title = (row.get("title") or "").strip()
        url = (row.get("applicationLink") or "").strip()
        external_id = (row.get("id") or "").strip()
        if not title or not url or not external_id or _is_generic_pipeline_title(title):
            continue

        # A row with no employer is the board's own index of other listings
        # ("List of Recurring Fellowships…"), never a real opening — and it
        # would otherwise open a nameless company in the registry.
        org, org_url = _organisation(row, board_cfg["url"])
        if not org:
            continue

        description = (row.get("description") or "").strip()

        jobs.append(
            {
                "title": title,
                "location": _location(row),
                "department": ", ".join(row.get("causeAreas") or []),
                "url": url,
                "external_id": external_id,
                "snippet": _html_to_snippet(description),
                "full_description": _full_description(row, description),
                "compensation": _compensation(row),
                "deadline": (row.get("applicationDeadline") or "").strip(),
                "org_override": org,
                "org_url": org_url,
            }
        )

    print(
        f"  [{board_name}] EA Opportunities: {len(jobs)} relevant from {total_seen} total "
        f"({total_seen - len(vacancies)} not vacancies)"
    )
    return jobs


def _is_vacancy(row: dict) -> bool:
    """True unless every one of the row's types is something other than work."""
    types = [t.lower() for t in (row.get("opportunityTypes") or []) if t]
    return not types or any(t not in _NON_VACANCY_TYPES for t in types)


def _organisation(row: dict, board_url: str) -> tuple[str, str]:
    """The employer's name and site. ``organization`` is the board's name-only twin."""
    for org in row.get("organizations") or []:
        name = (org.get("name") or "").strip()
        if name:
            return name, (org.get("link") or "").strip() or board_url
    for name in row.get("organization") or []:
        if str(name).strip():
            return str(name).strip(), board_url
    return "", board_url


def _compensation(row: dict) -> str:
    """What the employer wrote. ``salary`` is the board's own numeric index of it.

    ``salaryOriginal`` is the human range ("$90,000 – $150,000", "Competitive"),
    ``salary`` a plain number the board derives for sorting — so the written
    form wins, and the number only stands in when it is all there is.
    """
    written = str(row.get("salaryOriginal") or "").strip()
    if written:
        return written
    amount = row.get("salary")
    return str(amount).strip() if amount else ""


def _location(row: dict) -> str:
    """The free-text location, or the board's own tidy filter vocabulary."""
    written = (row.get("location") or "").strip()
    if written:
        return written
    return ", ".join(p.strip() for p in (row.get("locationFilter") or []) if p and p.strip())


def _full_description(row: dict, description: str) -> str:
    """The description plus the structured facts that only live in the tags."""
    meta = []
    for label, key in (
        ("Opportunity type", "opportunityTypes"),
        ("Routes to impact", "routesToImpact"),
        ("Skills", "skillSet"),
        ("Education", "education"),
    ):
        values = row.get(key) or []
        if isinstance(values, str):
            values = [values]
        joined = ", ".join(str(v).strip() for v in values if str(v).strip())
        if joined:
            meta.append(f"{label}: {joined}")
    if not meta:
        return description
    return (description + "\n\n" + " | ".join(meta)).strip()
