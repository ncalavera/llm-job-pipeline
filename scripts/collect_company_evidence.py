#!/usr/bin/env python3
"""Collect raw company evidence from multiple sources into company_evidence table.

Primary-source-first: facts must come from primary text the company published, not
generated prose. After a real incident (Perplexity fabricated a "within 3 hours
of Pacific" remote constraint that the real Greenhouse posting contradicts), Perplexity
is DEMOTED to URL discovery only and is NOT a default source. Facts come from the
company website, its real ATS / job board (Greenhouse / Lever / Ashby / Workable), and
Exa page contents (real page text).

Usage:
    python3 scripts/collect_company_evidence.py --company "Acme Foundation,Example Org"
    python3 scripts/collect_company_evidence.py --company "Name" --sources website,exa
    python3 scripts/collect_company_evidence.py --company "Acme Foundation" \\
        --manual-urls "https://example.com/page1,https://example.com/page2"

Sources:
    website           -- Firecrawl scrape of about/mission pages (primary)
    careers           -- ATS / job-board scrape (Greenhouse / Lever / Ashby / Workable):
                         the board index + a few real postings → offices, remote, visa,
                         comp straight from primary text (primary)
    exa               -- General profile search via Exa, real page text (primary)
    exa_offices       -- Offices/remote/visa-focused Exa search, real page text (primary)
    perplexity        -- DEMOTED: generated prose, NOT a fact source. Not in defaults.
    perplexity_offices -- DEMOTED: generated prose, NOT a fact source. Not in defaults.
    deep_research     -- OpenAI deep-research stub (not called by default)
    manual_url        -- Explicit URL(s) passed via --manual-urls (Firecrawl scrape)
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_conn import get_conn

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# Primary-source-first defaults. Perplexity is intentionally excluded — it returns
# generated prose, never primary text, so it must not drive any fact-based score.
_DEFAULT_SOURCES = "website,careers,exa,exa_offices"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Collect raw evidence for companies")
    p.add_argument("--company", required=True, help="Canonical company name(s), comma-separated")
    p.add_argument(
        "--sources",
        default=_DEFAULT_SOURCES,
        help=f"Comma-separated source list (default: {_DEFAULT_SOURCES})",
    )
    p.add_argument(
        "--manual-urls", help="Extra URLs to scrape as source='manual_url', comma-separated"
    )
    return p


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _resolve_company(conn, name: str) -> "dict | None":
    """Return company dict (id, website, careers_url, ats_slug, ats_config,
    fetch_strategy) or None if not found. The ATS fields drive primary-source
    careers collection."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, website, careers_url, ats_slug, ats_config, fetch_strategy "
            "FROM company WHERE canonical_name = %s",
            (name,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "company_id": str(row[0]),
        "website": row[1] or "",
        "careers_url": row[2] or "",
        "ats_slug": row[3] or "",
        "ats_config": row[4] if isinstance(row[4], dict) else {},
        "fetch_strategy": (row[5] or "").strip(),
    }


def _store_evidence(
    conn, company_id: str, source: str, url: str, content: str, meta: "dict | None"
) -> None:
    """Idempotent by (company_id, source): delete previous row, insert fresh."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM company_evidence WHERE company_id = %s AND source = %s",
            (company_id, source),
        )
        cur.execute(
            """INSERT INTO company_evidence (company_id, source, url, content, meta)
               VALUES (%s, %s, %s, %s, %s)""",
            (company_id, source, url, content, json.dumps(meta) if meta is not None else None),
        )
    conn.commit()


def _store_evidence_by_url(
    conn, company_id: str, source: str, url: str, content: str, meta: "dict | None"
) -> None:
    """Idempotent by (company_id, source, url): used for manual_url rows."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM company_evidence WHERE company_id = %s AND source = %s AND url = %s",
            (company_id, source, url),
        )
        cur.execute(
            """INSERT INTO company_evidence (company_id, source, url, content, meta)
               VALUES (%s, %s, %s, %s, %s)""",
            (company_id, source, url, content, json.dumps(meta) if meta is not None else None),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Firecrawl helpers
# ---------------------------------------------------------------------------


def _get_firecrawl_client():
    try:
        from firecrawl import FirecrawlApp

        key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not key:
            print("  WARNING: FIRECRAWL_API_KEY not set", file=sys.stderr)
            return None
        return FirecrawlApp(api_key=key)
    except ImportError:
        print("  WARNING: firecrawl package not installed", file=sys.stderr)
        return None


def _scrape_url(client, url: str) -> str:
    """Scrape a single URL and return full markdown. Empty string on failure."""
    try:
        result = client.scrape(url, formats=["markdown"], only_main_content=True, timeout=60000)
        md = (
            result.markdown
            if hasattr(result, "markdown")
            else (result.get("markdown") if isinstance(result, dict) else "")
        )
        return md or ""
    except Exception as e:
        print(f"    scrape failed for {url}: {str(e)[:80]}")
        return ""


def _scrape_with_links(client, url: str) -> "tuple[str, list[str]]":
    """Scrape a URL and return (markdown, links). ('', []) on failure.

    The links list is used to walk from an ATS board index into individual job
    postings (where offices / remote / visa / comp live as primary text)."""
    try:
        result = client.scrape(
            url, formats=["markdown", "links"], only_main_content=True, timeout=60000
        )
        if hasattr(result, "markdown"):
            md = result.markdown or ""
            links = list(getattr(result, "links", None) or [])
        elif isinstance(result, dict):
            md = result.get("markdown") or ""
            links = list(result.get("links") or [])
        else:
            md, links = "", []
        return md, links
    except Exception as e:
        print(f"    scrape failed for {url}: {str(e)[:80]}")
        return "", []


_ABOUT_KEYWORDS = {
    "about",
    "mission",
    "team",
    "values",
    "impact",
    "who-we-are",
    "our-story",
    "what-we-do",
}


def _discover_about_pages(client, url: str) -> list[str]:
    """Map site and pick up to 3 about/mission pages."""
    root = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    try:
        result = client.map(
            root, search="about mission team values", limit=20, include_subdomains=False
        )
        links = (
            result.links
            if hasattr(result, "links")
            else (result if isinstance(result, list) else [])
        )
    except Exception as e:
        print(f"    map() failed: {str(e)[:80]}")
        return [url]

    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for link in links:
        if isinstance(link, str):
            link_url = link
        elif isinstance(link, dict):
            link_url = link.get("url", "")
        elif hasattr(link, "url"):
            link_url = link.url or ""
        else:
            continue
        if not link_url or link_url in seen:
            continue
        seen.add(link_url)
        path = urlparse(link_url).path.lower().rstrip("/")
        if any(
            skip in path
            for skip in ("/login", "/signup", "/privacy", "/terms", "/cookie", "/legal", "/404")
        ):
            continue
        score = 100 if path in ("", "/") else 0
        for kw in _ABOUT_KEYWORDS:
            if kw in path:
                score = max(score, 90)
        if score > 0:
            scored.append((score, link_url))

    scored.sort(key=lambda x: -x[0])
    urls = [u for _, u in scored]
    if url not in urls:
        urls.insert(0, url)
    selected = urls[:3]
    print(
        f"    Mapped {len(links)} pages → selected {len(selected)}: "
        + ", ".join(urlparse(u).path or "/" for u in selected)
    )
    return selected


# ---------------------------------------------------------------------------
# Source: website
# ---------------------------------------------------------------------------


def collect_website(conn, company_id: str, name: str, website: str, careers_url: str = "") -> bool:
    """Scrape about/mission pages via Firecrawl, store full markdown."""
    if not website:
        print(f"  [{name}] website: no URL in DB — skipping")
        return False

    client = _get_firecrawl_client()
    if client is None:
        import requests

        _UA = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
        )
        try:
            resp = requests.get(website, headers={"User-Agent": _UA}, timeout=15)
            resp.raise_for_status()
            content = resp.text[:200_000]
        except Exception as e:
            print(f"  [{name}] website: requests fallback failed: {e}")
            return False
        _store_evidence(conn, company_id, "website", website, content, None)
        print(f"  [{name}] website: stored {len(content):,} chars (requests fallback)")
        return True

    pages = _discover_about_pages(client, website)
    combined = ""
    for page_url in pages:
        md = _scrape_url(client, page_url)
        if md:
            combined += f"=== PAGE: {page_url} ===\n{md}\n\n"
        time.sleep(0.5)

    if not combined:
        print(f"  [{name}] website: no content scraped")
        return False

    _store_evidence(conn, company_id, "website", website, combined, None)
    print(f"  [{name}] website: stored {len(combined):,} chars")
    return True


# ---------------------------------------------------------------------------
# Source: careers (ATS / job-board primary text)
# ---------------------------------------------------------------------------

_CAREERS_SUFFIXES = ["/careers", "/jobs", "/about/jobs", "/work-with-us", "/join-us"]

# How many real job postings to walk into from an ATS board index. The board index
# only lists titles + location tags; offices / remote / visa / comp prose lives on
# the individual postings, so we pull a few to capture primary text for the anchor rule.
_CAREERS_MAX_POSTINGS = 3

# Build the public board URL(s) for a known ATS provider from its slug.
_ATS_BOARD_BUILDERS = {
    "greenhouse": lambda s: [
        f"https://job-boards.greenhouse.io/{s}",
        f"https://boards.greenhouse.io/{s}",
    ],
    "lever": lambda s: [f"https://jobs.lever.co/{s}"],
    "ashby": lambda s: [f"https://jobs.ashbyhq.com/{s}"],
    "workable": lambda s: [f"https://apply.workable.com/{s}/", f"https://{s}.workable.com"],
}

# Detect an ATS board URL in a page link → provider name.
_ATS_URL_PATTERNS = [
    (re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([a-z0-9_-]+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I), "ashby"),
    (re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I), "workable"),
    (re.compile(r"([a-z0-9_-]+)\.workable\.com", re.I), "workable"),
]

# Detect an individual job-posting URL → provider name (deeper than the board index).
_ATS_POSTING_PATTERNS = {
    "greenhouse": re.compile(r"greenhouse\.io/[^/]+/jobs/\d+", re.I),
    "lever": re.compile(r"jobs\.lever\.co/[^/]+/[0-9a-fA-F-]{8,}", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/[^/]+/[0-9a-fA-F-]{8,}", re.I),
    "workable": re.compile(r"workable\.com/[^/]+/j/[A-Za-z0-9]+", re.I),
}


def _ats_board_candidates(
    fetch_strategy: str, ats_slug: str, ats_config: dict
) -> "list[tuple[str, str]]":
    """Board URL candidates from stored ATS columns → [(url, provider), ...].

    Reads ats_config['url'] (explicit board/careers URL) and ats_config['slug']
    /ats_slug + fetch_strategy. ats_config takes precedence over ats_slug."""
    cfg = ats_config if isinstance(ats_config, dict) else {}
    candidates: list[tuple[str, str]] = []

    cfg_url = (cfg.get("url") or "").strip()
    if cfg_url:
        # provider unknown for a bare careers URL — treat as company-own page
        prov = next((p for pat, p in _ATS_URL_PATTERNS if pat.search(cfg_url)), "")
        candidates.append((cfg_url, prov))

    slug = (cfg.get("slug") or ats_slug or "").strip()
    builder = _ATS_BOARD_BUILDERS.get(fetch_strategy)
    if slug and builder:
        candidates.extend((u, fetch_strategy) for u in builder(slug))

    return candidates


def _detect_board_in_links(links: "list[str]") -> "list[tuple[str, str]]":
    """Find ATS board URLs in a page's links → [(board_url, provider), ...]."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        for pat, provider in _ATS_URL_PATTERNS:
            m = pat.search(link)
            if m:
                slug = m.group(1).lower()
                key = (provider, slug)
                if key in seen:
                    continue
                seen.add(key)
                for board_url in _ATS_BOARD_BUILDERS[provider](slug):
                    found.append((board_url, provider))
    return found


def _posting_links(links: "list[str]", provider: str) -> "list[str]":
    """Pick individual job-posting URLs for a provider from a board's links."""
    pat = _ATS_POSTING_PATTERNS.get(provider)
    if not pat:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for link in links:
        if pat.search(link) and link not in seen:
            seen.add(link)
            out.append(link)
    return out


def collect_careers(
    conn,
    company_id: str,
    name: str,
    website: str,
    careers_url: str = "",
    ats_slug: str = "",
    ats_config: "dict | None" = None,
    fetch_strategy: str = "",
) -> bool:
    """Scrape the company's real ATS / job board as PRIMARY text.

    Resolution order: stored ATS columns (ats_config / ats_slug + fetch_strategy)
    → careers_url → ATS board detected in the homepage links → common careers paths.
    When the chosen page is a known ATS board, also walk into a few real postings,
    because offices / remote / visa / comp prose lives on the postings, not the index.
    """
    client = _get_firecrawl_client()
    if client is None:
        print(f"  [{name}] careers: Firecrawl unavailable — skipping")
        return False

    # 1) Build prioritized (url, provider) candidates.
    candidates: list[tuple[str, str]] = _ats_board_candidates(
        fetch_strategy, ats_slug, ats_config or {}
    )
    if careers_url:
        prov = next((p for pat, p in _ATS_URL_PATTERNS if pat.search(careers_url)), "")
        candidates.append((careers_url, prov))

    # 2) If no ATS info in DB, detect a board from the homepage links.
    if not candidates and website:
        print(f"  [{name}] careers: no ATS in DB — probing homepage links")
        _, home_links = _scrape_with_links(client, website)
        candidates.extend(_detect_board_in_links(home_links))

    # 3) Common careers paths as the weak fallback (company-own page, no provider).
    if website:
        root = f"{urlparse(website).scheme}://{urlparse(website).netloc}"
        candidates.extend((root + suffix, "") for suffix in _CAREERS_SUFFIXES)

    if not candidates:
        print(f"  [{name}] careers: no website / ATS / careers_url in DB — skipping")
        return False

    # 4) Scrape candidates in order; first with real content wins.
    used_url = ""
    provider = ""
    board_links: list[str] = []
    combined = ""
    seen_urls: set[str] = set()
    for url, prov in candidates:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        print(f"  [{name}] careers: trying {url}" + (f" [{prov}]" if prov else ""))
        md, links = _scrape_with_links(client, url)
        if md and len(md) > 200:
            combined = f"=== PAGE: {url} ===\n{md}\n\n"
            used_url, provider, board_links = url, prov, links
            break
        time.sleep(0.3)

    if not combined:
        print(f"  [{name}] careers: no content found on any candidate URL")
        return False

    # 5) For a known ATS board, walk into a few real postings for primary facts.
    postings_added = 0
    if provider:
        postings = _posting_links(board_links, provider)[:_CAREERS_MAX_POSTINGS]
        for purl in postings:
            md = _scrape_url(client, purl)
            if md and len(md) > 200:
                combined += f"=== POSTING: {purl} ===\n{md}\n\n"
                postings_added += 1
            time.sleep(0.3)
        print(f"  [{name}] careers: board {provider}, {postings_added} posting(s) added")

    _store_evidence(conn, company_id, "careers", used_url, combined, None)
    print(f"  [{name}] careers: stored {len(combined):,} chars from {used_url}")
    return True


# ---------------------------------------------------------------------------
# Source: perplexity (general profile) — DEMOTED, NOT a default source
#
# Perplexity returns GENERATED PROSE, never primary text, and invents specifics
# (a real incident: it fabricated a "within 3 hours of Pacific" remote
# constraint that the real Greenhouse posting flatly contradicts). It must NEVER
# be a fact source. Kept here only for optional URL discovery (run explicitly via
# --sources perplexity); excluded from _DEFAULT_SOURCES.
# ---------------------------------------------------------------------------

_PERPLEXITY_GENERAL_PROMPT = """Profile {company} for a job-seeker. Cover:
1. Mission and what they actually do
2. Scale and annual budget or revenue
3. Funding sources and notable backers / donors
4. Founders and notable current leaders
5. Office locations (cities / countries)
6. Whether there are Russian-speaking staff or ex-USSR founders / leadership
7. How selective or prestigious they are (acceptance rates, employer brand)
Keep the answer factual and concise."""


def _call_perplexity(name: str, prompt: str) -> "tuple[str, list, str]":
    """Return (answer_text, citations, model). Raises on API error."""
    import requests as _req

    key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not key:
        raise RuntimeError("PERPLEXITY_API_KEY not set")
    payload = {
        "model": "sonar",
        "messages": [
            {"role": "system", "content": "You are a factual research assistant."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1024,
    }
    resp = _req.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    answer = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    citations = data.get("citations", [])
    model = data.get("model", "sonar")
    return answer, citations, model


def collect_perplexity(
    conn, company_id: str, name: str, website: str = "", careers_url: str = ""
) -> bool:
    """General job-seeker profile via Perplexity Sonar."""
    try:
        answer, citations, model = _call_perplexity(
            name, _PERPLEXITY_GENERAL_PROMPT.format(company=name)
        )
    except Exception as e:
        print(f"  [{name}] perplexity: API error: {e}")
        return False

    if not answer:
        print(f"  [{name}] perplexity: empty response")
        return False

    meta = {"model": model, "citations": citations}
    _store_evidence(conn, company_id, "perplexity", "", answer, meta)
    print(f"  [{name}] perplexity: stored {len(answer):,} chars, {len(citations)} citation(s)")
    return True


# ---------------------------------------------------------------------------
# Source: perplexity_offices
# ---------------------------------------------------------------------------

_PERPLEXITY_OFFICES_PROMPT = """For {company}: list ALL office locations and countries.
What is their remote-work policy for employees outside HQ?
Do they sponsor work visas or hire internationally?
Include any mention of relocation support, time-zone requirements, or countries where they actively hire.
Keep the answer factual and cite sources."""


def collect_perplexity_offices(
    conn, company_id: str, name: str, website: str = "", careers_url: str = ""
) -> bool:
    """Offices / remote / visa-focused Perplexity query."""
    try:
        answer, citations, model = _call_perplexity(
            name, _PERPLEXITY_OFFICES_PROMPT.format(company=name)
        )
    except Exception as e:
        print(f"  [{name}] perplexity_offices: API error: {e}")
        return False

    if not answer:
        print(f"  [{name}] perplexity_offices: empty response")
        return False

    meta = {"model": model, "citations": citations, "note": "offices_remote_visa"}
    _store_evidence(conn, company_id, "perplexity_offices", "", answer, meta)
    print(
        f"  [{name}] perplexity_offices: stored {len(answer):,} chars, {len(citations)} citation(s)"
    )
    return True


# ---------------------------------------------------------------------------
# Source: exa (general profile)
# ---------------------------------------------------------------------------


def _call_exa(name: str, query: str) -> "tuple[str, list[str]]":
    """Return (combined_text, result_urls). Raises on API error."""
    import requests as _req

    key = os.environ.get("EXA_API_KEY", "")
    if not key:
        raise RuntimeError("EXA_API_KEY not set")
    payload = {
        "query": query,
        "numResults": 3,
        "type": "neural",
        "contents": {"text": True},
    }
    resp = _req.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": key, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    parts = []
    urls_list = []
    for r in results:
        url = r.get("url", "")
        title = r.get("title", "")
        text = r.get("text", "") or ""
        urls_list.append(url)
        parts.append(f"=== {title} ({url}) ===\n{text}")
    return "\n\n".join(parts), urls_list


def collect_exa(conn, company_id: str, name: str, website: str = "", careers_url: str = "") -> bool:
    """General profile search via Exa, top 3 results."""
    query = f"{name} organization mission funding offices leadership"
    try:
        combined, urls_list = _call_exa(name, query)
    except Exception as e:
        print(f"  [{name}] exa: API error: {e}")
        return False

    if not combined:
        print(f"  [{name}] exa: no results")
        return False

    meta = {"result_urls": urls_list, "query": query}
    _store_evidence(conn, company_id, "exa", "", combined, meta)
    print(f"  [{name}] exa: stored {len(combined):,} chars from {len(urls_list)} result(s)")
    return True


# ---------------------------------------------------------------------------
# Source: exa_offices
# ---------------------------------------------------------------------------


def collect_exa_offices(
    conn, company_id: str, name: str, website: str = "", careers_url: str = ""
) -> bool:
    """Offices / remote / visa-focused Exa search, top 3 results."""
    query = (
        f"{name} office locations countries remote work policy "
        f"visa sponsorship international hiring"
    )
    try:
        combined, urls_list = _call_exa(name, query)
    except Exception as e:
        print(f"  [{name}] exa_offices: API error: {e}")
        return False

    if not combined:
        print(f"  [{name}] exa_offices: no results")
        return False

    meta = {"result_urls": urls_list, "query": query, "note": "offices_remote_visa"}
    _store_evidence(conn, company_id, "exa_offices", "", combined, meta)
    print(f"  [{name}] exa_offices: stored {len(combined):,} chars from {len(urls_list)} result(s)")
    return True


# ---------------------------------------------------------------------------
# Source: manual_url
# ---------------------------------------------------------------------------


def collect_manual_urls(conn, company_id: str, name: str, urls: list[str]) -> bool:
    """Scrape explicit URLs via Firecrawl, one evidence row per URL."""
    client = _get_firecrawl_client()
    if client is None:
        print(f"  [{name}] manual_url: Firecrawl unavailable — skipping")
        return False

    ok_count = 0
    for url in urls:
        print(f"  [{name}] manual_url: scraping {url}")
        md = _scrape_url(client, url)
        if not md:
            print(f"  [{name}] manual_url: no content from {url}")
            continue
        _store_evidence_by_url(conn, company_id, "manual_url", url, md, None)
        print(f"  [{name}] manual_url: stored {len(md):,} chars from {url}")
        ok_count += 1
        time.sleep(0.5)

    return ok_count > 0


# ---------------------------------------------------------------------------
# Source: deep_research (stub — expensive, not called by default)
# ---------------------------------------------------------------------------


def collect_deep_research(
    conn, company_id: str, name: str, website: str = "", careers_url: str = ""
) -> bool:
    """Stub: OpenAI deep-research call. NOT called unless --sources deep_research."""
    print(f"  [{name}] deep_research: stub — not implemented yet")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_COLLECTORS = {
    "website": collect_website,
    "careers": collect_careers,
    "perplexity": collect_perplexity,
    "perplexity_offices": collect_perplexity_offices,
    "exa": collect_exa,
    "exa_offices": collect_exa_offices,
    "deep_research": collect_deep_research,
    # manual_url is handled separately via --manual-urls
}


def main() -> int:
    args = build_parser().parse_args()
    companies = [c.strip() for c in args.company.split(",") if c.strip()]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    manual_urls = (
        [u.strip() for u in args.manual_urls.split(",") if u.strip()] if args.manual_urls else []
    )

    unknown_sources = [s for s in sources if s not in _COLLECTORS]
    if unknown_sources:
        print(f"ERROR: unknown source(s): {', '.join(unknown_sources)}", file=sys.stderr)
        print(f"Valid sources: {', '.join(_COLLECTORS)}", file=sys.stderr)
        return 1

    conn = get_conn()
    results: dict[str, dict[str, bool]] = {}

    for name in companies:
        print(f"\n=== {name} ===")
        row = _resolve_company(conn, name)
        if row is None:
            print(f"  [{name}] ERROR: not found in company table")
            results[name] = {s: False for s in sources}
            continue

        company_id = row["company_id"]
        website = row["website"]
        careers_url = row["careers_url"]
        results[name] = {}

        for source in sources:
            fn = _COLLECTORS[source]
            try:
                if source == "careers":
                    ok = fn(
                        conn,
                        company_id,
                        name,
                        website=website,
                        careers_url=careers_url,
                        ats_slug=row["ats_slug"],
                        ats_config=row["ats_config"],
                        fetch_strategy=row["fetch_strategy"],
                    )
                else:
                    ok = fn(conn, company_id, name, website=website, careers_url=careers_url)
            except Exception as e:
                # Belt-and-suspenders: a timeout/network error that escapes a
                # collector's own try/except must not kill the whole batch —
                # mark this source FAILED and move on to the next.
                print(f"  [{name}] {source}: ERROR caught — {type(e).__name__}: {e}")
                ok = False
            results[name][source] = ok

        if manual_urls:
            ok = collect_manual_urls(conn, company_id, name, manual_urls)
            results[name]["manual_url"] = ok

    print("\n=== Summary ===")
    for company, src_results in results.items():
        for src, ok in src_results.items():
            print(f"  {company} / {src}: {'OK' if ok else 'FAILED'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
