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
"""

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
            is_overload = "429" in err_str or "overloaded" in err_str.lower()
            if is_overload and attempt < len(delays):
                continue
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


def _is_unscrapable_host(url: str) -> bool:
    """Hosts Firecrawl demonstrably cannot scrape — spending credits is pure
    waste. LinkedIn blocks scrapers outright (verified live 2026-07-03: a
    guest job page returns 0 chars); such rows heal on the next fetch when
    the detail pages aren't throttled, or age out via the stale-blind sweep."""
    host = urlparse(url).netloc.lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


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


def main():
    dry_run = "--dry-run" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    from database_supabase import load_vacancies, get_conn

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
            enriched += 1
            print(f"  -> {len(cleaned)} chars")
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
    else:
        main()
