"""Firecrawl scraper + zero-cost local fallbacks (PageUp XHR, Wagtail API).

Strategy "firecrawl_scrape": scrape a careers page via the Firecrawl SDK
(JSON extraction + markdown), falling back to a local requests+markdown
scraper when credits run out or the SDK is unavailable. Records per-run
scrape outcomes (js_required / credit_exhausted) and change-tracking
statuses on the package namespace so ``fetch_status`` stays honest.
"""

import hashlib
import html as html_module
import json
import re
import subprocess
import time

import fetchers as _pkg
from config import FIRECRAWL_CACHE
from fetchers import http
from fetchers.http import FetchError, _LOCAL_UA
from fetchers.html_utils import _absolutize_links, _html_to_markdown
from fetchers.parsing import _parse_json_jobs, parse_markdown_jobs
from fetchers.registry import company_fetcher, record_fetch_error, register_company

FIRECRAWL_JOBS_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "location": {"type": "string"},
                    "url": {"type": "string"},
                    "snippet": {"type": "string"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["jobs"],
}

# Per-run state (change statuses, scrape statuses, credit balance)
# lives on the fetchers package namespace — see fetchers/__init__.py.


class UnchangedListing(list):
    """Sentinel result for a careers page Firecrawl reports as byte-identical to
    the last scrape (change-tracking ``changeStatus == "same"``).

    It is an EMPTY list — change-tracking gives us nothing to diff, so the save
    layer must neither import nor fabricate any role — but it is NOT the same as
    a genuinely empty page: every role captured on the previous scrape is STILL
    listed. The ``unchanged`` flag lets the fetch driver tell an unchanged page
    apart from a real empty one and bump ``last_seen`` on the company's own live
    rows, so the whole roster does not freeze and falsely age into Triage's
    "Expired" column. Duck-typed downstream via ``getattr(jobs, "unchanged",
    False)`` — any plain ``list`` reads as changed.
    """

    unchanged = True


def get_firecrawl_change_statuses() -> dict[str, str]:
    """Return the change tracking statuses from the last fetch run."""
    return dict(_pkg._last_firecrawl_change_status)


def get_scrape_statuses() -> dict[str, str]:
    """Return fetch_status overrides set by the scraper (e.g. js_required)."""
    return dict(_pkg._last_scrape_status)


def _firecrawl_credits_available() -> bool:
    """Check Firecrawl credit balance once per run.

    Queries GET /v2/team/credit-usage with $FIRECRAWL_API_KEY. Caches the
    result for the rest of the process. Returns True only when credits > 0.
    On any error (no key, network), assumes credits available and lets the
    normal Firecrawl path surface the real error.
    """
    if _pkg._firecrawl_credits_remaining is not None:
        return _pkg._firecrawl_credits_remaining > 0

    import os

    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        # No key: can't check, but Firecrawl client likely unusable anyway.
        _pkg._firecrawl_credits_remaining = -1  # unknown → treat as "try anyway"
        return True
    try:
        resp = _pkg.requests.get(
            "https://api.firecrawl.dev/v2/team/credit-usage",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        data = resp.json().get("data", {}) if resp.ok else {}
        remaining = int(data.get("remainingCredits", -1))
        _pkg._firecrawl_credits_remaining = remaining
        if remaining == 0:
            print("  Firecrawl credits exhausted — using local scraper")
        return remaining != 0  # >0 → use Firecrawl; -1 (unknown) → try anyway
    except Exception as e:
        print(f"  Firecrawl credit check failed ({e}); will attempt Firecrawl")
        _pkg._firecrawl_credits_remaining = -1
        return True


# Errors from the Firecrawl SDK that signal quota exhaustion / rate limits.
_QUOTA_ERROR_MARKERS = (
    "402",
    "429",
    "payment required",
    "insufficient credit",
    "out of credit",
    "rate limit",
    "quota",
    "too many requests",
)


def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _QUOTA_ERROR_MARKERS)


# Browser-like User-Agent lives in fetchers.http (shared skeleton).


def _fetch_pageup_xhr(org_name: str, url: str, *, url_filter: str = "") -> list[dict]:
    """PageUp ATS (e.g. jobs.unicef.org): facet filters apply only via XHR.

    Plain GET ignores ?optionsFacetsDD_* facets and returns the unfiltered
    board. With X-Requested-With the server returns {"results": "<html>"}
    honoring the facet. PageUp throttles bursts (HTTP 202 + empty body), so
    retry with backoff; production cadence is one request per TTL cycle.
    """
    import time

    print(f"  [{org_name}] PageUp XHR scrape: {url}")
    headers = {
        "User-Agent": _LOCAL_UA,
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _get_throttled(req_url, *, xhr=True, retries=3):
        """GET with backoff: PageUp answers bursts with HTTP 202 + empty body."""
        h = headers if xhr else {"User-Agent": _LOCAL_UA}
        for attempt in range(retries):
            if attempt:
                time.sleep(60 * attempt)
            try:
                resp = _pkg.requests.get(req_url, headers=h, timeout=20)
                if resp.status_code == 200 and resp.text:
                    return resp.text
                print(
                    f"  [{org_name}] PageUp throttled "
                    f"(HTTP {resp.status_code}), retry {attempt + 1}/{retries}..."
                )
            except Exception as e:
                print(f"  [{org_name}] PageUp fetch error: {e}")
        return ""

    url_filter_re = re.compile(url_filter) if url_filter else None
    jobs, seen_urls, all_md = [], set(), []
    sep = "&" if "?" in url else "?"
    for page in range(1, 6):
        page_url = url if page == 1 else f"{url}{sep}page={page}"
        raw = _get_throttled(page_url)
        if not raw.strip().startswith("{"):
            break
        html = _absolutize_links(json.loads(raw).get("results", ""), url)
        all_md.append(_html_to_markdown(html))

        # Parse the PageUp list structure directly: markdown-parsing drops
        # most rows (titles routinely exceed the 100-char title limit).
        page_new = 0
        for m in re.finditer(
            r'class="job-link"\s+href="([^"]+)"\s*>\s*([^<]+)</a>(.{0,2000}?)'
            r'(?=class="job-link"|$)',
            html,
            re.DOTALL,
        ):
            job_url, title, tail = (
                m.group(1),
                html_module.unescape(m.group(2)).strip(),
                m.group(3),
            )
            if job_url in seen_urls:
                continue
            seen_urls.add(job_url)
            if url_filter_re and not url_filter_re.search(job_url):
                continue
            snippet_m = re.search(r"<p[^>]*>\s*([^<]{30,})</p>", tail)
            loc_m = re.search(r"location[^>]*>\s*<[^>]*>\s*([^<]+)<", tail, re.IGNORECASE)
            jobs.append(
                {
                    "title": title,
                    "location": (loc_m.group(1).strip() if loc_m else ""),
                    "department": "",
                    "url": job_url,
                    "external_id": hashlib.md5(job_url.encode()).hexdigest()[:12],
                    "snippet": (
                        html_module.unescape(snippet_m.group(1).strip()) if snippet_m else ""
                    ),
                }
            )
            page_new += 1
        if page_new == 0:  # page param ignored or past the end
            break
        time.sleep(10)
    if not jobs:
        _pkg._last_scrape_status[org_name] = "js_required"
        return []
    _cache_markdown(org_name, "\n".join(all_md), source="pageup")
    print(f"  [{org_name}] PageUp parsed {len(jobs)} vacancies")

    # Detail pages are server-rendered; fetch gently to respect throttling.
    for job in jobs:
        time.sleep(10)
        detail_html = _get_throttled(job["url"], xhr=False, retries=2)
        if len(detail_html) > 2000:
            detail_md = _html_to_markdown(detail_html)
            if len(detail_md) > len(job.get("snippet", "")):
                job["full_description"] = detail_md
    with_desc = sum(1 for j in jobs if j.get("full_description"))
    print(f"  [{org_name}] PageUp descriptions: {with_desc}/{len(jobs)}")
    return jobs


def _fetch_wagtail_jobs_api(org_name: str, url: str) -> list[dict]:
    """Wagtail CMS pages API (e.g. /api/v2/pages/?type=jobs.JobPage).

    The jobs *listing* page is a JS app, but the underlying Wagtail API and
    the per-job detail pages are server-rendered — zero-cost to fetch.
    """
    import time

    print(f"  [{org_name}] Wagtail jobs API: {url}")
    try:
        resp = _pkg.requests.get(url, headers={"User-Agent": _LOCAL_UA}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [{org_name}] Wagtail API error: {e}")
        _pkg._last_scrape_status[org_name] = "js_required"
        return []

    jobs = []
    for it in data.get("items", []):
        title = (it.get("title") or "").strip()
        job_url = (it.get("meta", {}).get("html_url") or "").strip()
        if not title or not job_url:
            continue

        def _s(key):
            v = it.get(key)
            return v.strip() if isinstance(v, str) else ""

        extras = " | ".join(
            filter(
                None,
                [
                    _s("location"),
                    _s("salary"),
                    _s("contract_type"),
                    f"closes {it['closes'][:10]}" if isinstance(it.get("closes"), str) else "",
                ],
            )
        )
        snippet = " ".join(filter(None, [_s("listing_summary"), extras]))
        jobs.append(
            {
                "title": title,
                "location": _s("location"),
                "department": "",
                "url": job_url,
                "external_id": hashlib.md5(job_url.encode()).hexdigest()[:12],
                "snippet": snippet,
            }
        )
    print(f"  [{org_name}] Wagtail API: {len(jobs)} vacancies")

    for job in jobs:
        time.sleep(2)
        try:
            resp = _pkg.requests.get(job["url"], headers={"User-Agent": _LOCAL_UA}, timeout=20)
            if resp.status_code == 200 and len(resp.text) > 2000:
                job["full_description"] = _html_to_markdown(resp.text)
        except Exception as e:
            print(f"  [{org_name}] detail fetch failed for {job['title']}: {e}")
    with_desc = sum(1 for j in jobs if j.get("full_description"))
    print(f"  [{org_name}] Wagtail descriptions: {with_desc}/{len(jobs)}")
    return jobs


def _fetch_local_scrape(org_name: str, url: str, *, url_filter: str = "") -> list[dict]:
    """Zero-cost fallback: fetch the page with requests → markdown → parse.

    Records a 'js_required' status override when the page looks like a
    JS-rendered shell (little text / no links) so the company row is marked
    honestly instead of faking a successful empty fetch.
    """
    if "optionsFacetsDD" in url or "/filter/?" in url:
        return _fetch_pageup_xhr(org_name, url, url_filter=url_filter)
    if "/api/v2/pages/" in url:
        return _fetch_wagtail_jobs_api(org_name, url)
    print(f"  [{org_name}] Local scrape (no Firecrawl credits): {url}")
    try:
        resp = http.get(
            url,
            headers={
                "User-Agent": _LOCAL_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
        )
    except FetchError as e:
        print(f"  [{org_name}] Local fetch error: {e}")
        record_fetch_error(org_name, e.status)
        return []

    markdown = _html_to_markdown(resp.text)
    _cache_markdown(org_name, markdown, source="local")

    # JS-shell detection: thin text or no links → can't scrape without a browser.
    text_len = len(re.sub(r"\s+", " ", markdown).strip())
    has_links = "](" in markdown
    if text_len < 500 or not has_links:
        print(
            f"  [{org_name}] Page looks JS-rendered "
            f"(text={text_len} chars, links={has_links}) → js_required"
        )
        _pkg._last_scrape_status[org_name] = "js_required"
        return []

    jobs = parse_markdown_jobs(markdown, org_name, url_filter=url_filter)
    print(f"  [{org_name}] Local scraper parsed {len(jobs)} vacancies")
    if not jobs:
        # HTML had links but parser found no job-like rows: likely JS-gated list.
        _pkg._last_scrape_status[org_name] = "js_required"
    return jobs


@company_fetcher
def fetch_firecrawl_scrape(
    org_name: str, url: str, *, use_json: bool = True, url_filter: str = ""
) -> list[dict]:
    """Scrape a careers page via Firecrawl SDK (preferred) or CLI fallback.

    With use_json=True (default for companies): requests both JSON extraction
    and markdown in a single API call (5 credits). Falls back to markdown
    parsing if JSON yields 0 results, then to CLI if SDK unavailable.

    With use_json=False (boards): requests markdown only (1 credit).
    """
    FIRECRAWL_CACHE.mkdir(parents=True, exist_ok=True)

    # PageUp facets (?optionsFacetsDD_*, /filter/?) apply only via XHR with
    # X-Requested-With — Firecrawl's plain render gets the unfiltered board,
    # so route these straight to the local PageUp scraper.
    if "optionsFacetsDD" in url or "/filter/?" in url:
        return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)

    # Quota guard: if credits are exhausted, skip Firecrawl entirely (saves
    # ~60s of latency per company) and go straight to the local scraper.
    # Record the reason (U9) so an empty result is marked 'credit_exhausted'
    # rather than an ambiguous no_data — the local fallback may override this
    # with 'js_required' or clear it by returning rows.
    if not _firecrawl_credits_available():
        _pkg._last_scrape_status[org_name] = "credit_exhausted"
        return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)

    print(f"  [{org_name}] Firecrawl scrape: {url}")

    client = _pkg.get_firecrawl_client()
    if client is None:
        print(f"  [{org_name}] SDK not available, falling back to local scraper")
        return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)

    # Build formats list
    formats = ["markdown", "changeTracking"]
    if use_json:
        formats.append({"type": "json", "schema": FIRECRAWL_JOBS_SCHEMA})

    try:
        result = client.scrape(
            url,
            formats=formats,
            only_main_content=True,
            timeout=60000,
            actions=[{"type": "wait", "milliseconds": 5000}],
        )
    except Exception as e:
        print(f"  [{org_name}] SDK error: {e}")
        if _is_quota_error(e):
            # Mark credits exhausted for the rest of the run, then go local.
            _pkg._firecrawl_credits_remaining = 0
            _pkg._last_scrape_status[org_name] = "credit_exhausted"  # U9 reason code
            print(f"  [{org_name}] Quota/rate-limit error — switching to local scraper")
        else:
            print(f"  [{org_name}] Falling back to local scraper")
        return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)

    # --- Handle change tracking if present ---
    change_tracking = getattr(result, "changeTracking", None)
    if change_tracking is None:
        change_tracking = getattr(result, "change_tracking", None)
    if change_tracking:
        status = getattr(change_tracking, "changeStatus", None)
        if status is None:
            status = getattr(change_tracking, "change_status", None)
        _pkg._last_firecrawl_change_status[org_name] = status or "unknown"
        if status == "same":
            # Byte-identical page: nothing to diff, so DON'T fabricate roles.
            # Return an empty sentinel the driver recognises to refresh last_seen
            # on this company's still-listed rows (see UnchangedListing).
            print(f"  [{org_name}] Page unchanged since last scrape — refreshing last_seen")
            return UnchangedListing()

    # --- Try JSON extraction first ---
    if use_json:
        json_data = getattr(result, "json", None)
        if json_data:
            jobs = _parse_json_jobs(json_data, org_name, url, url_filter=url_filter)
            if jobs:
                print(f"  [{org_name}] Parsed {len(jobs)} vacancies from JSON extraction")
                # Cache markdown for debugging
                _cache_markdown(org_name, getattr(result, "markdown", "") or "")
                # Enrich blind jobs (no full_description) via individual page scrape
                jobs = _enrich_blind_jobs(jobs, org_name)
                return jobs
            print(f"  [{org_name}] JSON extraction returned 0 valid jobs, trying markdown")

    # --- Fall back to markdown parsing ---
    markdown = getattr(result, "markdown", "") or ""
    _cache_markdown(org_name, markdown)
    if markdown:
        jobs = parse_markdown_jobs(markdown, org_name, url_filter=url_filter)
        print(f"  [{org_name}] Parsed {len(jobs)} vacancies from markdown")
        # Enrich blind jobs (no full_description) via individual page scrape
        jobs = _enrich_blind_jobs(jobs, org_name)
        return jobs

    print(f"  [{org_name}] No content returned from SDK — trying local scraper")
    return _pkg._fetch_local_scrape(org_name, url, url_filter=url_filter)


def _enrich_blind_jobs(jobs: list[dict], org_name: str) -> list[dict]:
    """Scrape individual job URLs via Firecrawl for jobs missing full_description.

    Modifies jobs in-place, adding full_description from scraped page content.
    Skips blacklisted titles to avoid wasting Firecrawl credits.
    """
    client = _pkg.get_firecrawl_client()
    if not client:
        return jobs

    blind = [(i, j) for i, j in enumerate(jobs) if not j.get("full_description") and j.get("url")]
    if not blind:
        return jobs

    # Pre-filter blacklisted titles
    import filters

    to_enrich = [
        (i, j) for i, j in blind if not filters.title_words_blacklisted(j.get("title", ""))
    ]
    skipped = len(blind) - len(to_enrich)

    if not to_enrich:
        if skipped:
            print(f"  [{org_name}] {skipped} blind jobs skipped (blacklisted)")
        return jobs

    print(
        f"  [{org_name}] Enriching {len(to_enrich)} blind jobs via Firecrawl"
        + (f" ({skipped} blacklisted skipped)" if skipped else "")
    )

    enriched = 0
    for idx, (i, job) in enumerate(to_enrich):
        url = job["url"]
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
                # Clean markdown to plain text
                text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", "", md)
                text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
                text = re.sub(r"<[^>]{1,200}>", "", text)
                text = re.sub(r"[*_`#\\]", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()

                if len(text) >= 100:
                    jobs[i]["full_description"] = text[:30000]
                    enriched += 1
        except Exception as e:
            print(f"  [{org_name}] Enrich error for {job.get('title', '?')[:40]}: {e}")

        # Rate limit
        if idx < len(to_enrich) - 1:
            time.sleep(0.5)

    print(f"  [{org_name}] Enriched {enriched}/{len(to_enrich)} blind jobs")
    return jobs


def _cache_markdown(org_name: str, markdown: str, *, source: str = "firecrawl") -> None:
    """Save markdown to cache file for debugging.

    Prepends a one-line provenance marker recording which scraper produced
    the content (firecrawl vs local).
    """
    if not markdown:
        return
    FIRECRAWL_CACHE.mkdir(parents=True, exist_ok=True)
    slug = org_name.lower().replace(" ", "_").replace(".", "")
    output_file = FIRECRAWL_CACHE / f"{slug}.md"
    try:
        header = f"<!-- source: {source} | {time.strftime('%Y-%m-%d %H:%M')} -->\n"
        output_file.write_text(header + markdown, encoding="utf-8")
    except Exception:
        pass


def _fetch_firecrawl_scrape_cli(org_name: str, url: str, *, url_filter: str = "") -> list[dict]:
    """Legacy CLI fallback: scrape via firecrawl CLI subprocess."""
    FIRECRAWL_CACHE.mkdir(parents=True, exist_ok=True)
    slug = org_name.lower().replace(" ", "_").replace(".", "")
    output_file = FIRECRAWL_CACHE / f"{slug}.md"

    try:
        result = subprocess.run(
            [
                "firecrawl",
                "scrape",
                url,
                "--wait-for",
                "5000",
                "--only-main-content",
                "-o",
                str(output_file),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            print(f"  [{org_name}] CLI error: {result.stderr[:200]}")
            return []
        if not output_file.exists():
            print(f"  [{org_name}] No output file created")
            return []
        content = output_file.read_text(encoding="utf-8")
        jobs = parse_markdown_jobs(content, org_name, url_filter=url_filter)
        print(f"  [{org_name}] CLI parsed {len(jobs)} vacancies from markdown")
        return jobs
    except subprocess.TimeoutExpired:
        print(f"  [{org_name}] CLI timeout")
        return []
    except FileNotFoundError:
        print(f"  [{org_name}] firecrawl CLI not found")
        return []
    except Exception as e:
        print(f"  [{org_name}] CLI error: {e}")
        return []


@register_company("firecrawl_scrape")
def _firecrawl_scrape_entry(org_name: str, config: dict) -> list[dict]:
    return fetch_firecrawl_scrape(org_name, config["url"], url_filter=config.get("url_filter", ""))
