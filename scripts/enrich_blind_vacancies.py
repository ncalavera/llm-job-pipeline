#!/usr/bin/env python3
"""
Enrich blind vacancies (no full_description) by scraping their job URLs via Firecrawl.

Loads blind vacancies from Supabase, scrapes each URL, updates full_description.
Every scrape result runs through quality.clean_description() before it is
saved: a leading OR trailing cookie/consent banner is stripped, and pages
that are nothing but boilerplate (cookie wall, error page, nav chrome) are
NOT saved (logged and left blind for a future enrich run instead).
UNOPS (careers.unops.org) and UNICEF (jobs.unicef.org) detail pages are
server-rendered — fetched with plain requests, zero Firecrawl credits.

Usage:
    python3 scripts/enrich_blind_vacancies.py [--limit N] [--dry-run]
    python3 scripts/enrich_blind_vacancies.py --clean-cookie-pages [--org unops] [--apply]
    python3 scripts/enrich_blind_vacancies.py --source-text [--dry-run] [--limit N] [--ids id1,id2]
        Upgrades summary-only board rows (config.SUMMARY_ONLY_BOARDS) from the
        board's own text to the real posting page. Runs whether or not
        FIRECRAWL_API_KEY is set (plain requests+bs4 first; Firecrawl is only
        a fallback for a JS-shell page). See fetch_source_text_for_summary_boards.
"""

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from bs4 import BeautifulSoup

from config import get_firecrawl_client
from fetchers import _fetch_unops_job_detail, _LOCAL_UA
from fetchers.html_utils import _html_to_text
from quality import (
    _COOKIE_BANNER_RE,
    COOKIE_MIN_REMAINDER,
    COOKIE_SCORE_POLLUTION,
    _strip_trailing_cookie_banner,
    clean_description,
    strip_cookie_boilerplate,
)
import filters
import run_status  # progress heartbeat (vacancies/run_status.json)
from filter_vacancies import _all_locations_excluded


# ---------------------------------------------------------------------------
# Direct (no-Firecrawl) detail fetchers for server-rendered ATS hosts
# ---------------------------------------------------------------------------


def _fetch_pageup_detail(url: str) -> str:
    """PageUp (jobs.unicef.org) detail pages are server-rendered — plain GET.

    Gone jobs may redirect to a listing without any jobnotfound marker; return "" so the
    listing page never gets saved as a description. The JD lives in
    <div id="job-content"> — extracting it directly skips the cookie banner
    and nav chrome entirely (html2text on the full page chokes on PageUp's
    inline GTM scripts).
    """
    try:
        resp = requests.get(url, headers={"User-Agent": _LOCAL_UA}, timeout=20)
        if resp.status_code != 200 or "jobnotfound" in resp.url.lower():
            return ""
        html = resp.text
    except Exception:
        return ""
    content = BeautifulSoup(html, "html.parser").find(id="job-content")
    if content is None:
        return ""
    text = content.get_text(" ", strip=True)
    return text if len(text) > 200 else ""


# host → zero-cost fetcher (Firecrawl is never tried for these hosts: the
# pages are server-rendered, so a Firecrawl failure means the job is gone)
_DIRECT_HOST_FETCHERS = {
    "careers.unops.org": _fetch_unops_job_detail,
    "jobs.unops.org": _fetch_unops_job_detail,  # 301 → careers.unops.org
    "jobs.unicef.org": _fetch_pageup_detail,
}


def _extract_text_from_markdown(md: str) -> str:
    """Convert markdown to plain text for vacancy description."""
    # Remove images
    md = re.sub(r"!\[([^\]]*)\]\([^)]+\)", "", md)
    # Convert links to text
    md = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)
    # Remove HTML tags
    md = re.sub(r"<[^>]{1,200}>", "", md)
    # Remove markdown formatting
    md = re.sub(r"[*_`#\\]", "", md)
    # Collapse whitespace
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _scrape_job_page(client, url: str) -> str:
    """Scrape a single job page via Firecrawl. Returns description text or empty string."""
    delays = [5, 15, 45]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            time.sleep(delay)
        try:
            result = client.scrape(
                url,
                formats=["markdown"],
                only_main_content=True,
                timeout=60000,
            )
            md = ""
            if hasattr(result, "markdown"):
                md = result.markdown or ""
            elif isinstance(result, dict):
                md = result.get("markdown", "")

            if md:
                text = _extract_text_from_markdown(md)
                return text
            return ""
        except Exception as e:
            err_str = str(e)
            # The SDK's RateLimitError says "Rate Limit Exceeded", with no "429".
            is_overload = (
                "429" in err_str
                or "overloaded" in err_str.lower()
                or "rate limit" in err_str.lower()
            )
            if is_overload and attempt < len(delays):
                continue
            # Never silent: an expired key looked like "every JS page is empty".
            print(f"  Firecrawl scrape failed for {url}: {type(e).__name__}: {err_str[:300]}")
            return ""
    return ""


def _fetch_description(client, url: str) -> str:
    """Fetch a job description: direct fetcher for known server-rendered
    hosts (zero Firecrawl credits), Firecrawl scrape for everything else."""
    host = urlparse(url).netloc.lower()
    direct = _DIRECT_HOST_FETCHERS.get(host)
    if direct:
        return direct(url)
    return _scrape_job_page(client, url)


def _get_vacancy_url(vac: dict) -> str:
    """Get best URL for a vacancy."""
    locs = vac.get("locations", [])
    for loc in locs:
        url = loc.get("url", "")
        if url:
            return url
    return ""


#: drive.google.com/forms.gle/sheets.google.com are shared or multi-role
#: pages (a form, a spreadsheet, a shared drive folder) — never one role's
#: own posting, so they are never fetched at all. docs.google.com is handled
#: separately below: a single /document/d/<id> doc CAN be exported as plain
#: text (see _google_doc_id/_fetch_google_doc_text), but a bug once stored a
#: DIFFERENT org's doc as the posting ("Chargé de mission - Opérations" for a
#: vacancy titled "Operations Manager") — looks_like_this_role() guards every
#: fetch against exactly that, so a doc export is safe to try.
_SHARED_DOC_HOSTS = {"drive.google.com", "forms.gle", "sheets.google.com"}

_GOOGLE_DOC_ID_RE = re.compile(r"^https://docs\.google\.com/document/d/([\w-]+)", re.IGNORECASE)


def _google_doc_id(url: str) -> str | None:
    """The doc id of a docs.google.com/document/d/<id>/... URL, else None.
    A docs.google.com FORM or other non-/document/ path returns None, so it
    stays blocked by _is_unscrapable_host — only a single doc is fetchable."""
    m = _GOOGLE_DOC_ID_RE.match(url)
    return m.group(1) if m else None


def _is_unscrapable_host(url: str) -> bool:
    """Hosts that cannot be trusted to return THIS role's own posting text —
    spending a fetch (Firecrawl credits or otherwise) is pure waste, or worse,
    wrong. LinkedIn blocks scrapers outright (verified live 2026-07-03: a
    guest job page returns 0 chars); such rows heal on the next fetch when
    the detail pages aren't throttled, or age out via the stale-blind sweep.
    A shared drive/form/spreadsheet is never a single posting, so it is never
    fetched. docs.google.com is fetchable only for a single /document/d/<id>
    doc — a form under the same host stays blocked."""
    host = urlparse(url).netloc.lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return True
    if host == "docs.google.com":
        return _google_doc_id(url) is None
    return host in _SHARED_DOC_HOSTS


def _gate_scraped_description(text: str) -> tuple[str | None, str]:
    """Quality-gate freshly scraped text before it is written to full_description.

    Thin, unit-testable wrapper around quality.clean_description() — the
    single gate every description-writing path must share (this write path
    used to run its own cookie-only check, which missed a trailing
    consent-widget banner entirely). Returns (text_to_save, verdict);
    text_to_save is not None only when verdict == "ok". Any other verdict
    means the vacancy stays blind and is retried on a future enrich run.
    """
    return clean_description(text)


#: A plain-fetched page shorter than this is presumed a JS shell (React/Vue app
#: root div, no server-rendered content) — same threshold the chat-screen
#: prototype (fetch_missing.py) verified live against real ATS pages.
_JS_SHELL_MAX_CHARS = 1200


#: id/class tokens that mark chrome the blacklist tags don't catch (a
#: cookie widget or share bar is usually a <div>, not one of the tags above).
_CHROME_ATTR_RE = re.compile(r"cookie|consent|share|social|breadcrumb", re.IGNORECASE)

#: Narrowed container must keep at least this many chars, and this share of
#: the body's own text, or it's discarded as a mis-detection (e.g. a page
#: where <main> wraps only a sidebar widget) in favour of the full body.
_CONTAINER_MIN_CHARS = 400
_CONTAINER_MIN_BODY_SHARE = 0.2
#: ponytail: the "largest block" fallback only fires above this share of the
#: body's text, so it never grabs a random big <div> that isn't the content.
_LARGEST_BLOCK_MIN_BODY_SHARE = 0.6


def _get_text_block(el) -> str:
    """Element text with paragraph breaks kept: one line per text node,
    internal whitespace collapsed, runs of blank lines capped at one."""
    raw = el.get_text("\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip()


def _extract_jsonld_job_description(soup) -> str | None:
    """A `JobPosting.description` in a JSON-LD block is a legit, pre-cleaned
    source many ATS pages embed for search engines — top priority when
    present and substantial, ahead of guessing at the right HTML container."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                desc = item.get("description")
                if desc:
                    return _get_text_block(BeautifulSoup(desc, "html.parser"))
    return None


def _strip_chrome_elements(soup) -> None:
    """Drop tags and id/class-flagged elements that are never posting body:
    scripts/nav/chrome tags outright, plus ARIA-marked nav/dialog regions and
    anything whose id/class says cookie/consent/share/social/breadcrumb."""
    for tag in soup(
        ["script", "style", "nav", "footer", "header", "aside", "form", "button", "svg", "noscript"]
    ):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": ["navigation", "banner", "contentinfo", "dialog"]}):
        tag.decompose()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed:  # child of an element removed earlier in this loop
            continue
        ident = f"{tag.get('id') or ''} {' '.join(tag.get('class') or [])}"
        # Chrome blocks are small; a big block named e.g. "social-impact-role"
        # is the posting itself, so it stays.
        if ident.strip() and _CHROME_ATTR_RE.search(ident) and len(tag.get_text()) < 1000:
            tag.decompose()


def _find_main_container(soup):
    """Best-guess posting container, cheapest signal first: a known ATS
    container (PageUp's #job-content), then the semantic HTML5 landmarks,
    then a naive largest-text-block heuristic.
    ponytail: heuristic, not a readability port — upgrade if it misfires often.
    """
    job_content = soup.find(id="job-content")
    if job_content is not None:
        return job_content
    for selector in ("main", "article", "[role=main]"):
        el = soup.select_one(selector)
        if el is not None:
            return el
    body = soup.find("body")
    if body is None:
        return None
    body_len = len(_get_text_block(body))
    if not body_len:
        return None
    best, best_len = None, 0
    for el in body.find_all(["div", "section"]):
        el_len = len(_get_text_block(el))
        if el_len > best_len:
            best, best_len = el, el_len
    if best is not None and best_len / body_len >= _LARGEST_BLOCK_MIN_BODY_SHARE:
        return best
    return None


#: Workday hosts a public (undocumented but keyless) JSON API for a job's own
#: posting: https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}
#: /job/{rest}, mirroring the job page's own
#: https://{tenant}.{wdN}.myworkdayjobs.com/{site}/job/{rest}.
_WORKDAY_HOST_RE = re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$", re.IGNORECASE)


def _workday_cxs_url(url: str) -> str | None:
    """The job's cxs JSON detail URL, or None when url isn't a Workday job page."""
    parsed = urlparse(url)
    if not _WORKDAY_HOST_RE.match(parsed.netloc.lower()):
        return None
    tenant = parsed.netloc.split(".", 1)[0].lower()
    parts = parsed.path.strip("/").split("/", 1)
    if len(parts) < 2 or not parts[1]:
        return None
    site, rest = parts
    return f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/{rest}"


def _fetch_workday_detail_text(cxs_url: str, diag: dict) -> tuple[str, dict]:
    diag["cxs_url"] = cxs_url
    try:
        resp = requests.get(
            cxs_url, headers={"User-Agent": _LOCAL_UA, "Accept": "application/json"}, timeout=20
        )
    except Exception as e:
        diag["error"] = str(e)
        return "", diag
    diag["status"] = resp.status_code
    if resp.status_code != 200:
        diag["body_head"] = resp.text[:300]
        return "", diag
    try:
        data = resp.json()
    except Exception as e:
        diag["error"] = f"bad json: {e}"
        diag["body_head"] = resp.text[:300]
        return "", diag
    html_desc = (data.get("jobPostingInfo") or {}).get("jobDescription", "") or ""
    return _html_to_text(html_desc), diag


def _fetch_google_doc_text(doc_id: str, diag: dict) -> tuple[str, dict]:
    """A public Google Doc exports as plain text at this URL — no HTML parsing,
    no auth. looks_like_this_role() still gates it before it's ever saved."""
    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    diag["export_url"] = export_url
    try:
        resp = requests.get(export_url, headers={"User-Agent": _LOCAL_UA}, timeout=20)
    except Exception as e:
        diag["error"] = str(e)
        return "", diag
    diag["status"] = resp.status_code
    diag["body_head"] = resp.text[:300]
    if resp.status_code != 200:
        return "", diag
    return resp.text.strip(), diag


def _looks_like_pdf(resp) -> bool:
    ctype = resp.headers.get("Content-Type", "")
    if "application/pdf" in ctype.lower():
        return True
    return getattr(resp, "content", b"")[:5] == b"%PDF-"


def _extract_pdf_text(content: bytes) -> str:
    """Local PDF text extraction (pypdf). Returns "" on any failure — the
    caller's Firecrawl fallback (which also parses PDFs) picks it up from
    there, so a missing/broken extractor never crashes the run."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    from io import BytesIO

    try:
        reader = PdfReader(BytesIO(content))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:
        return ""


def _fetch_plain_page_text(url: str) -> tuple[str, dict]:
    """Zero-cost requests+bs4 fetch of an arbitrary job page.

    Host/type dispatch before the generic HTML fetch: a Workday job page's
    own JSON API, a Google Doc's text export, a PDF posting's extracted text.
    Otherwise, extracts the posting body, not the whole page: JSON-LD
    JobPosting description first, else the best-guess main container (see
    _find_main_container), falling back to the full body when the container
    doesn't hold enough of the page's text to trust.

    Returns (text, diagnostics). ``diagnostics`` always carries enough to log
    a failure with full context — url, status, response headers, first 300
    bytes of body — per the "no lazy design" rule (errors need the machine
    context from day one, not just "it failed").
    """
    diag: dict = {"url": url}

    doc_id = _google_doc_id(url)
    if doc_id:
        return _fetch_google_doc_text(doc_id, diag)

    cxs_url = _workday_cxs_url(url)
    if cxs_url:
        return _fetch_workday_detail_text(cxs_url, diag)

    try:
        resp = requests.get(url, headers={"User-Agent": _LOCAL_UA}, timeout=20)
    except Exception as e:
        diag["error"] = str(e)
        return "", diag
    diag["status"] = resp.status_code
    diag["headers"] = dict(resp.headers)
    if resp.status_code != 200:
        diag["body_head"] = resp.text[:300]
        return "", diag

    if _looks_like_pdf(resp):
        diag["content_type"] = "pdf"
        return _extract_pdf_text(resp.content), diag

    diag["body_head"] = resp.text[:300]
    soup = BeautifulSoup(resp.text, "html.parser")

    jsonld_text = _extract_jsonld_job_description(soup)
    if jsonld_text and len(jsonld_text) >= _CONTAINER_MIN_CHARS:
        return jsonld_text, diag

    _strip_chrome_elements(soup)
    body = soup.find("body") or soup
    body_text = _get_text_block(body)

    container = _find_main_container(soup)
    if container is not None:
        container_text = _get_text_block(container)
        if len(container_text) >= _CONTAINER_MIN_CHARS and (
            not body_text or len(container_text) / len(body_text) >= _CONTAINER_MIN_BODY_SHARE
        ):
            return container_text, diag

    return body_text, diag


#: Below this share of the title's content words found in the fetched text,
#: the org name alone must carry the match (see looks_like_this_role).
TITLE_WORD_MATCH_THRESHOLD = 0.6

_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "and",
    "or",
    "for",
    "to",
    "in",
    "on",
    "at",
    "by",
    "with",
    "is",
    "are",
}

#: Legal-entity suffixes stripped before matching an org name — "Acme NGO"
#: must still match "Acme" in running prose that never spells out "NGO".
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(ltd|inc|llc|gmbh|foundation|corp|corporation|plc|ngo|nonprofit|co)\.?\b",
    re.IGNORECASE,
)


def _content_words(text: str) -> set[str]:
    """Lowercased words of 3+ chars, stopwords dropped — the title's meaning,
    not its grammar."""
    words = re.findall(r"[\w'-]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) >= 3}


def _has_whole_word_phrase(text_lower: str, phrase_lower: str) -> bool:
    if not phrase_lower:
        return False
    return re.search(r"(?<!\w)" + re.escape(phrase_lower) + r"(?!\w)", text_lower) is not None


#: Whole-line chrome tokens dropped by _strip_chrome_lines. Exact matches
#: only (whitespace/case-insensitive) so posting prose is never touched —
#: e.g. a line that merely mentions "share" in a sentence survives.
_CHROME_LINE_TOKENS = {
    "skip to content",
    "search",
    "sign in",
    "log in",
    "share",
    "facebook",
    "twitter",
    "x",
    "linkedin",
    "copy url",
    "copy link",
    "email",
    "print",
    "apply",
    "apply now",
    "menu",
}
_BACK_TO_RE = re.compile(r"^back to\b", re.IGNORECASE)
_EMPTY_MD_LINK_RE = re.compile(r"^-?\s*\[\]\([^)]*\)\s*$")
#: A Firecrawl markdown nav list renders each item as "- Sign In" — strip the
#: bullet before comparing against the chrome-token whitelist so those still
#: count as whole-line matches (the token check itself stays exact).
_MD_BULLET_RE = re.compile(r"^(?:[-*+]|\d+\.)\s+")


def _strip_chrome_lines(text: str) -> str:
    """Shared post-clean for BOTH the plain-fetch and Firecrawl-markdown
    text, before clean_description()/looks_like_this_role() ever see it:
    drops empty markdown links (``- [](url)``) and lines that are exactly a
    chrome token (nav/share/apply buttons page chrome leaves behind).
    Line-exact only, so real prose is never touched."""
    if not text:
        return text
    kept = []
    for line in text.split("\n"):
        stripped = line.strip()
        low = _MD_BULLET_RE.sub("", stripped).lower()
        if stripped and (
            _EMPTY_MD_LINK_RE.match(stripped)
            or low in _CHROME_LINE_TOKENS
            or _BACK_TO_RE.match(low)
        ):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept))


def looks_like_this_role(text: str, org: str, title: str) -> tuple[bool, str]:
    """True when ``text`` plausibly IS the posting for (org, title) — the
    general guard behind the Google-Doc bug: whatever a fetch returns, before
    it is trusted as THIS role's posting, it must actually mention this role.

    Passes on either signal: the org's own name appears (legal suffix
    stripped, whole word, case-insensitive) OR at least
    TITLE_WORD_MATCH_THRESHOLD of the title's content words do. A title with
    no usable content words (all stopwords / short words) falls back to
    requiring the org match alone. Returns (ok, reason) — the reason is
    printed on refusal so a wrong-page fetch is diagnosable, not silent.
    """
    text_lower = (text or "").lower()
    if not text_lower:
        return False, "empty text"

    org_norm = re.sub(r"\s+", " ", _LEGAL_SUFFIX_RE.sub("", org or "")).strip().lower()
    if org_norm and _has_whole_word_phrase(text_lower, org_norm):
        return True, "org name matched"

    title_words = _content_words(title)
    if not title_words:
        return False, "no usable title words and org name not found"

    hits = sum(1 for w in title_words if _has_whole_word_phrase(text_lower, w))
    ratio = hits / len(title_words)
    if ratio >= TITLE_WORD_MATCH_THRESHOLD:
        return True, f"title words matched {hits}/{len(title_words)} ({ratio:.0%})"
    return (
        False,
        f"org name not found, title words {hits}/{len(title_words)} ({ratio:.0%}) "
        f"below {TITLE_WORD_MATCH_THRESHOLD:.0%}",
    )


def fetch_source_text_for_summary_boards(ids=None, dry_run=False, limit=None):
    """The source-text pass: upgrade a summary-only board's row from the
    board's own text to the real posting.

    Selection: source_board in config.SUMMARY_ONLY_BOARDS, status='unseen',
    still on board text (description_source NULL/'board_summary' once the
    column exists; pre-migration, a length proxy), first_seen within
    [enrich] source_fetch_max_age_days — the retry bound, so a dead apply URL
    is not refetched forever.

    Plain requests+bs4 first (free); Firecrawl fallback only when a key is
    configured AND the plain text looks like a JS shell (< _JS_SHELL_MAX_CHARS).
    Every fetch, however long or clean, is content-checked before it is
    trusted (looks_like_this_role) — a shared/misdirected page can return
    something else's posting entirely (found live: a Google Doc URL returned
    a different org's role). Success: full_description +
    description_source='source_page' (column permitting), the old board
    summary preserved into snippet if that was empty, deadline backfilled
    from the new text. Failure — fetch failure OR a content mismatch —
    description_source resets to 'board_summary' (column permitting) and full
    diagnostics are printed; never silently dropped.
    """
    from database_supabase import (
        get_conn,
        _vacancy_has_column,
        backfill_deadline_from_text,
        backfill_compensation_from_text,
    )
    from psycopg2.extras import RealDictCursor
    import config
    import settings

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    has_desc_source = _vacancy_has_column("description_source")
    max_age_days = settings.enrich()["source_fetch_max_age_days"]

    if ids:
        cur.execute(
            "SELECT v.id, v.title, v.source_board, v.full_description, v.snippet, "
            "v.locations, v.first_seen, c.canonical_name AS org "
            "FROM vacancy v JOIN company c ON v.company_id = c.id "
            "WHERE v.id = ANY(%s::uuid[])",
            (list(ids),),
        )
    else:
        source_cond = (
            "(v.description_source IS NULL OR v.description_source = 'board_summary')"
            if has_desc_source
            else "(v.full_description IS NULL OR length(v.full_description) < 400)"
        )
        query = f"""
            SELECT v.id, v.title, v.source_board, v.full_description, v.snippet,
                   v.locations, v.first_seen, c.canonical_name AS org
            FROM vacancy v JOIN company c ON v.company_id = c.id
            WHERE v.status = 'unseen'
              AND v.source_board = ANY(%s)
              AND {source_cond}
              AND v.first_seen >= (CURRENT_DATE - %s * INTERVAL '1 day')
            ORDER BY v.first_seen DESC
        """
        params = [list(config.SUMMARY_ONLY_BOARDS), max_age_days]
        if limit:
            query += " LIMIT %s"
            params.append(limit)
        cur.execute(query, params)
    rows = cur.fetchall()

    print(f"Source-text pass: {len(rows)} candidate row(s){' (dry-run)' if dry_run else ''}")
    client = get_firecrawl_client()
    upgraded = 0
    for row in rows:
        url = _get_vacancy_url(row)
        before_len = len(row.get("full_description") or "")
        if not url:
            print(f"  [{row['source_board']}] {row['title'][:45]:45s} -> SKIP, no apply URL")
            continue
        if _is_unscrapable_host(url):
            print(
                f"  [{row['source_board']}] {row['title'][:45]:45s} -> SKIP, unscrapable/shared-doc host ({url})"
            )
            continue

        text, diag = _fetch_plain_page_text(url)
        text = _strip_chrome_lines(text)
        cleaned, verdict = clean_description(text)
        if (verdict != "ok" or len(cleaned or "") < _JS_SHELL_MAX_CHARS) and client:
            fc_text = _strip_chrome_lines(_scrape_job_page(client, url))
            fc_cleaned, fc_verdict = clean_description(fc_text)
            if fc_verdict == "ok" and len(fc_cleaned or "") > len(cleaned or ""):
                cleaned, verdict, diag = (
                    fc_cleaned,
                    fc_verdict,
                    {**diag, "firecrawl_fallback": True},
                )

        method = "firecrawl" if diag.get("firecrawl_fallback") else "plain"
        length_ok = verdict == "ok" and len(cleaned or "") >= filters.MIN_JUDGEABLE_DESC_CHARS
        content_ok, content_reason = (
            looks_like_this_role(cleaned, row.get("org", ""), row["title"])
            if length_ok
            else (False, "n/a (verdict/length gate failed)")
        )
        success = length_ok and content_ok
        updates: dict = {}
        if success:
            updates["full_description"] = cleaned[:30000]
            if has_desc_source:
                updates["description_source"] = "source_page"
            old_summary = (row.get("full_description") or "").strip()
            if not (row.get("snippet") or "").strip() and old_summary:
                updates["snippet"] = old_summary[:400]
            upgraded += 1
            print(
                f"  [{row['source_board']}] {row['title'][:45]:45s} -> "
                f"{before_len} -> {len(cleaned)} chars [{method}] ({content_reason})"
            )
        elif length_ok and not content_ok:
            # Fetched something long and clean-looking, but it does not
            # mention this role at all — a shared/misdirected page (the
            # Google Doc bug), not a real posting. Never save it.
            if has_desc_source:
                updates["description_source"] = "board_summary"
            print(
                f"  [{row['source_board']}] {row['title'][:45]:45s} -> CONTENT MISMATCH "
                f"org={row.get('org', '')!r} url={url} method={method} reason={content_reason!r} "
                f"first_200={cleaned[:200]!r}"
            )
        else:
            if has_desc_source:
                updates["description_source"] = "board_summary"
            print(
                f"  [{row['source_board']}] {row['title'][:45]:45s} -> FAILED "
                f"(verdict={verdict}, len={len(cleaned or '')}) diagnostics={diag}"
            )

        if dry_run or not updates:
            continue
        set_parts = [f"{k} = %s" for k in updates]
        cur.execute(
            f"UPDATE vacancy SET {', '.join(set_parts)} WHERE id = %s::uuid",
            list(updates.values()) + [row["id"]],
        )
        if success:
            backfill_deadline_from_text(cur, row["id"], cleaned)
            backfill_compensation_from_text(cur, row["id"], cleaned)

    if not dry_run:
        conn.commit()
    print(f"Source-text pass done: {upgraded}/{len(rows)} upgraded to source_page.")
    return upgraded, len(rows)


def main():
    dry_run = "--dry-run" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    from database_supabase import (
        load_vacancies,
        get_conn,
        backfill_deadline_from_text,
        backfill_compensation_from_text,
    )

    # Scope to active-company, unscored vacancies only: enriching inactive or
    # already-scored rows wastes Firecrawl credits and re-parses vacancies the
    # pipeline has deliberately dropped (inactive companies stay unscored).
    all_vacs = load_vacancies(unscored_only=True)
    conn = get_conn()

    # Find blind vacancies: no full_description or < 100 chars, has URL
    # Pre-filter: skip blacklisted titles and excluded-country locations
    blind = []
    skipped_blacklist = 0
    skipped_excluded = 0
    skipped_no_url = 0
    skipped_unscrapable = 0
    for vid, vac in all_vacs.items():
        desc = (vac.get("full_description") or "").strip()
        if len(desc) >= 100:
            continue
        if filters.title_words_blacklisted(vac.get("title", "")):
            skipped_blacklist += 1
            continue
        if _all_locations_excluded(vac):
            skipped_excluded += 1
            continue
        url = _get_vacancy_url(vac)
        if not url:
            skipped_no_url += 1
            continue
        if _is_unscrapable_host(url):
            skipped_unscrapable += 1
            continue
        blind.append((vid, vac, url))

    if skipped_blacklist or skipped_excluded or skipped_no_url or skipped_unscrapable:
        print(
            f"Pre-filtered: {skipped_blacklist} blacklisted, {skipped_excluded} excluded-country, "
            f"{skipped_no_url} no URL, {skipped_unscrapable} unscrapable host "
            "(scraper-blocked; heals on the next fetch or ages out)"
        )

    if not blind:
        print("No blind vacancies with URLs found.")
        return

    if limit:
        blind = blind[:limit]

    print(f"Found {len(blind)} blind vacancies with URLs to enrich")
    print(f"Estimated Firecrawl credits: ~{len(blind)} (1 per page)")

    if dry_run:
        for i, (vid, vac, url) in enumerate(blind[:20], 1):
            print(f"  {i}. {vac['org']:30s} {vac['title'][:45]:45s} {url[:60]}")
        if len(blind) > 20:
            print(f"  ... and {len(blind) - 20} more")
        return

    client = get_firecrawl_client()
    if not client:
        print("ERROR: Firecrawl SDK not available")
        sys.exit(1)

    enriched = 0
    errors = 0
    cookie_pages = 0
    cur = conn.cursor()
    run_status.begin("enrich", len(blind))

    for i, (vid, vac, url) in enumerate(blind, 1):
        run_status.step(vac["org"], i - 1, enriched=enriched)
        print(f"  [{i}/{len(blind)}] {vac['org']:25s} {vac['title'][:45]:45s}", end="", flush=True)

        text = _fetch_description(client, url)
        cleaned, verdict = _gate_scraped_description(text)

        if verdict == "ok":
            if len(cleaned) < len(text or ""):
                print(f"  [banner -{len(text) - len(cleaned)} chars]", end="")
            cur.execute(
                "UPDATE vacancy SET full_description = %s WHERE id = %s::uuid",
                (cleaned[:30000], vid),  # cap at 30K chars
            )
            filled_deadline = backfill_deadline_from_text(cur, vid, cleaned)
            filled_comp = backfill_compensation_from_text(cur, vid, cleaned)
            enriched += 1
            tags = [
                t
                for t in (
                    "+deadline" if filled_deadline else "",
                    "+compensation" if filled_comp else "",
                )
                if t
            ]
            print(f"  -> {len(cleaned)} chars" + (f" [{' '.join(tags)}]" if tags else ""))
        elif verdict == "cookie_wall":
            # Cookie wall with no real content behind it — page needs JS, or
            # the banner (leading or trailing) ate almost everything. Saving
            # it would poison scoring, so treat as a failed scrape: the
            # vacancy stays blind and is retried on a future enrich run.
            print("  -> cookie/consent page, NOT saved (js_required)", flush=True)
            cookie_pages += 1
            errors += 1
        elif verdict in ("error_page", "nav_junk", "marketing_page"):
            print(f"  -> {verdict.replace('_', ' ')}, NOT saved", flush=True)
            errors += 1
        elif verdict == "too_short":
            print(f"  -> too short ({len(text or '')} chars)")
            errors += 1
        else:  # "empty"
            print("  -> empty")
            errors += 1

        # Commit every 10
        if i % 10 == 0:
            conn.commit()
            print(f"  --- committed ({enriched} enriched) ---")

        # Rate limit: 0.5s between requests
        time.sleep(0.5)

    conn.commit()
    run_status.finish(enriched=enriched)
    print(f"\nDone! Enriched {enriched}/{len(blind)} blind vacancies.")
    print(f"Errors/empty: {errors} (of which cookie/consent pages: {cookie_pages})")


def clean_cookie_pages():
    """Maintenance mode: find saved descriptions carrying a cookie/consent
    banner — leading (before the JD) or trailing (a widget appended after
    it) — strip it, and reset llm_score where the removed chunk had eaten
    enough of the scoring window to matter. Dry-run by default; --apply
    executes the UPDATEs."""
    apply = "--apply" in sys.argv
    org_filter = ""
    for i, arg in enumerate(sys.argv):
        if arg == "--org" and i + 1 < len(sys.argv):
            org_filter = sys.argv[i + 1]

    from database_supabase import get_conn

    conn = get_conn()
    cur = conn.cursor()
    sql = """
        SELECT v.id, c.canonical_name, v.title, v.full_description, v.llm_score
        FROM vacancy v JOIN company c ON v.company_id = c.id
        WHERE v.full_description ~* %s
    """
    params = [_COOKIE_BANNER_RE.pattern]
    if org_filter:
        sql += " AND c.canonical_name ILIKE %s"
        params.append(f"%{org_filter}%")
    sql += " ORDER BY c.canonical_name, v.title"
    cur.execute(sql, params)

    to_strip, to_blind = [], []
    for vid, org, title, desc, score in cur.fetchall():
        cleaned = _strip_trailing_cookie_banner(strip_cookie_boilerplate(desc))
        removed = len(desc) - len(cleaned)
        if removed == 0:
            continue  # anchor matched but nothing to strip (defensive)
        rescore = removed >= COOKIE_SCORE_POLLUTION
        if len(cleaned) < COOKIE_MIN_REMAINDER:
            to_blind.append((vid, org, title, desc, removed))
        else:
            to_strip.append((vid, org, title, desc, removed, cleaned, rescore))

    rescore_n = sum(1 for r in to_strip if r[6]) + len(to_blind)
    print(
        f"Found {len(to_strip) + len(to_blind)} descriptions with a cookie "
        f"banner ({len(to_blind)} pure cookie walls, "
        f"{rescore_n} need rescoring)",
        flush=True,
    )

    for vid, org, title, desc, removed, cleaned, rescore in to_strip:
        action = "strip+rescore" if rescore else "strip        "
        print(
            f"  {action} | {vid} | {org[:28]:28s} | {title[:38]:38s} "
            f"| -{removed} chars | {desc[:100]!r}"
        )
    for vid, org, title, desc, removed in to_blind:
        print(
            f"  blind+rescore | {vid} | {org[:28]:28s} | {title[:38]:38s} "
            f"| -{removed} chars | {desc[:100]!r}"
        )

    if not apply:
        print("\nDry run — nothing changed. Re-run with --apply to execute.")
        return

    for vid, org, title, desc, removed, cleaned, rescore in to_strip:
        if rescore:
            cur.execute(
                "UPDATE vacancy SET full_description = %s, llm_score = NULL, "
                "llm_scored_at = NULL WHERE id = %s::uuid",
                (cleaned[:30000], vid),
            )
        else:
            cur.execute(
                "UPDATE vacancy SET full_description = %s WHERE id = %s::uuid",
                (cleaned[:30000], vid),
            )
    for vid, org, title, desc, removed in to_blind:
        cur.execute(
            "UPDATE vacancy SET full_description = NULL, llm_score = NULL, "
            "llm_scored_at = NULL WHERE id = %s::uuid",
            (vid,),
        )
    conn.commit()
    print(
        f"\nDone! Stripped banner from {len(to_strip)} descriptions, "
        f"reset {len(to_blind)} to blind, {rescore_n} queued for rescoring."
    )


if __name__ == "__main__":
    if "--clean-cookie-pages" in sys.argv:
        clean_cookie_pages()
    elif "--source-text" in sys.argv:
        _dry_run = "--dry-run" in sys.argv
        _ids = None
        _limit = None
        for _i, _arg in enumerate(sys.argv):
            if _arg == "--ids" and _i + 1 < len(sys.argv):
                _ids = sys.argv[_i + 1].split(",")
            if _arg == "--limit" and _i + 1 < len(sys.argv):
                _limit = int(sys.argv[_i + 1])
        fetch_source_text_for_summary_boards(ids=_ids, dry_run=_dry_run, limit=_limit)
    else:
        main()
