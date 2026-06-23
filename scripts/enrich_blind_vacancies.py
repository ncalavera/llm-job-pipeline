#!/usr/bin/env python3
"""
Enrich blind vacancies (no full_description) by scraping their job URLs via Firecrawl.

Loads blind vacancies from Supabase, scrapes each URL, updates full_description.
Cookie/consent boilerplate is stripped from scrape results; pages that are
nothing but a cookie wall are NOT saved (logged as js_required instead).
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

from config import get_firecrawl_client
from fetchers import (_fetch_unops_job_detail, _html_to_markdown,
                      _html_to_text, _LOCAL_UA)
from quality import (_COOKIE_BANNER_RE, COOKIE_MIN_REMAINDER,
                     COOKIE_SCORE_POLLUTION, _find_cookie_banner_end,
                     is_cookie_boilerplate, strip_cookie_boilerplate)
import filters
from filter_vacancies import _all_locations_excluded


# ---------------------------------------------------------------------------
# Direct (no-Firecrawl) detail fetchers for server-rendered ATS hosts
# ---------------------------------------------------------------------------

def _fetch_pageup_detail(url: str) -> str:
    """PageUp (jobs.unicef.org) detail pages are server-rendered — plain GET.

    Gone jobs redirect to the listing with ?jobnotfound=true; return "" so the
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
    if len(html) < 2000:
        return ""
    start = html.find('id="job-content"')
    if start != -1:
        end = html.find("<script", start)
        block = html[start:end] if end != -1 else html[start:]
        text = _html_to_text("<div " + block + "</div>")
        if len(text) > 200:
            return text
    return _extract_text_from_markdown(_html_to_markdown(html))


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
    md = re.sub(r'!\[([^\]]*)\]\([^)]+\)', '', md)
    # Convert links to text
    md = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', md)
    # Remove HTML tags
    md = re.sub(r'<[^>]{1,200}>', '', md)
    # Remove markdown formatting
    md = re.sub(r'[*_`#\\]', '', md)
    # Collapse whitespace
    md = re.sub(r'\n{3,}', '\n\n', md)
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


def main():
    dry_run = "--dry-run" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    from database_supabase import load_vacancies, get_conn
    from psycopg2.extras import Json

    all_vacs = load_vacancies(unscored_only=False, include_inactive_companies=True)
    conn = get_conn()

    # Find blind vacancies: no full_description or < 100 chars, has URL
    # Pre-filter: skip blacklisted titles and excluded-country locations
    blind = []
    skipped_blacklist = 0
    skipped_excluded = 0
    skipped_no_url = 0
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
        blind.append((vid, vac, url))

    if not blind:
        print("No blind vacancies with URLs found.")
        return

    if limit:
        blind = blind[:limit]

    if skipped_blacklist or skipped_excluded or skipped_no_url:
        print(f"Pre-filtered: {skipped_blacklist} blacklisted, {skipped_excluded} excluded-country, {skipped_no_url} no URL")
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

    for i, (vid, vac, url) in enumerate(blind, 1):
        print(f"  [{i}/{len(blind)}] {vac['org']:25s} {vac['title'][:45]:45s}", end="", flush=True)

        text = _fetch_description(client, url)

        if text and is_cookie_boilerplate(text):
            # Cookie wall with no real content behind it — page needs JS.
            # Saving it would poison scoring, so treat as a failed scrape.
            print(f"  -> cookie/consent page, NOT saved (js_required)", flush=True)
            cookie_pages += 1
            errors += 1
        else:
            if text:
                stripped = strip_cookie_boilerplate(text)
                if len(stripped) < len(text):
                    print(f"  [banner -{len(text) - len(stripped)} chars]", end="")
                text = stripped

            if text and len(text) >= 100:
                cur.execute(
                    "UPDATE vacancy SET full_description = %s WHERE id = %s::uuid",
                    (text[:30000], vid),  # cap at 30K chars
                )
                enriched += 1
                print(f"  -> {len(text)} chars")
            elif text:
                print(f"  -> too short ({len(text)} chars)")
                errors += 1
            else:
                print(f"  -> empty")
                errors += 1

        # Commit every 10
        if i % 10 == 0:
            conn.commit()
            print(f"  --- committed ({enriched} enriched) ---")

        # Rate limit: 0.5s between requests
        time.sleep(0.5)

    conn.commit()
    print(f"\nDone! Enriched {enriched}/{len(blind)} blind vacancies.")
    print(f"Errors/empty: {errors} (of which cookie/consent pages: {cookie_pages})")


def clean_cookie_pages():
    """Maintenance mode: find saved descriptions that start with a cookie
    banner, strip the banner, and reset llm_score where the banner had eaten
    the scoring window. Dry-run by default; --apply executes the UPDATEs."""
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
        end = _find_cookie_banner_end(desc)
        if end is None:
            continue  # cookie mention is not a leading banner (e.g. footer)
        stripped = desc[end:].strip()
        rescore = end >= COOKIE_SCORE_POLLUTION
        if len(stripped) < COOKIE_MIN_REMAINDER:
            to_blind.append((vid, org, title, desc, end))
        else:
            to_strip.append((vid, org, title, desc, end, stripped, rescore))

    rescore_n = sum(1 for r in to_strip if r[6]) + len(to_blind)
    print(f"Found {len(to_strip) + len(to_blind)} descriptions with a leading "
          f"cookie banner ({len(to_blind)} pure cookie walls, "
          f"{rescore_n} need rescoring)", flush=True)

    for vid, org, title, desc, end, stripped, rescore in to_strip:
        action = "strip+rescore" if rescore else "strip        "
        print(f"  {action} | {vid} | {org[:28]:28s} | {title[:38]:38s} "
              f"| -{end} chars | {desc[:100]!r}")
    for vid, org, title, desc, end in to_blind:
        print(f"  blind+rescore | {vid} | {org[:28]:28s} | {title[:38]:38s} "
              f"| -{end} chars | {desc[:100]!r}")

    if not apply:
        print("\nDry run — nothing changed. Re-run with --apply to execute.")
        return

    for vid, org, title, desc, end, stripped, rescore in to_strip:
        if rescore:
            cur.execute(
                "UPDATE vacancy SET full_description = %s, llm_score = NULL, "
                "llm_scored_at = NULL WHERE id = %s::uuid",
                (stripped[:30000], vid),
            )
        else:
            cur.execute(
                "UPDATE vacancy SET full_description = %s WHERE id = %s::uuid",
                (stripped[:30000], vid),
            )
    for vid, org, title, desc, end in to_blind:
        cur.execute(
            "UPDATE vacancy SET full_description = NULL, llm_score = NULL, "
            "llm_scored_at = NULL WHERE id = %s::uuid",
            (vid,),
        )
    conn.commit()
    print(f"\nDone! Stripped banner from {len(to_strip)} descriptions, "
          f"reset {len(to_blind)} to blind, {rescore_n} queued for rescoring.")


if __name__ == "__main__":
    if "--clean-cookie-pages" in sys.argv:
        clean_cookie_pages()
    else:
        main()
