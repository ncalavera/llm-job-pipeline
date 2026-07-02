"""Vacancy fetchers package: ATS adapters, job boards, parsing, Firecrawl.

This package replaces the old single-file ``scripts/fetchers.py`` monolith.
Layout:

    fetchers/
      http.py        — shared HTTP skeleton (FetchError, get/post helpers)
      registry.py    — strategy registry + per-source failure recording
      html_utils.py  — HTML → text/snippet/markdown helpers
      parsing.py     — markdown/JSON job parsing + blacklist filters
      firecrawl.py   — Firecrawl scraper + zero-cost local fallbacks
      ats/           — one file per ATS provider (greenhouse, lever, …)
      boards/        — one file per job board (arbeitnow, idealist, …)

Patchable surface
-----------------
Mutable per-run state and hot-swappable collaborators live HERE, on the
package namespace, exactly where the old monolith kept them. Tests (and any
caller) monkeypatch ``fetchers.requests``, ``fetchers._last_scrape_status``,
``fetchers._firecrawl_credits_remaining`` or ``fetchers._fetch_local_scrape``
and every submodule picks the patch up, because submodules resolve these
names through the package at call time — never through a module-local copy.
"""

import time  # noqa: F401 — patch surface: tests monkeypatch fetchers.time.sleep
import requests  # patch surface: submodules resolve fetchers.requests at call time

from config import get_firecrawl_client  # patch surface: resolved at call time

# ---------------------------------------------------------------------------
# Per-run mutable state (canonical home — do not move into submodules)
# ---------------------------------------------------------------------------

# Firecrawl change tracking (org_name → changeStatus string).
_last_firecrawl_change_status: dict[str, str] = {}

# Scrape outcome overrides (org_name → fetch_status override, e.g. "js_required").
_last_scrape_status: dict[str, str] = {}

# Fetch failures (source name → "error: <reason>"); see registry.record_fetch_error.
_last_fetch_errors: dict[str, str] = {}

# Firecrawl credit balance, checked once per run. None = not yet checked.
_firecrawl_credits_remaining: "int | None" = None

# ---------------------------------------------------------------------------
# Public surface (kept import-compatible with the old fetchers.py monolith)
# ---------------------------------------------------------------------------

from fetchers.http import FetchError, _LOCAL_UA
from fetchers.registry import (
    BOARD_FETCHERS,
    COMPANY_FETCHERS,
    get_fetch_errors,
    record_fetch_error,
)
from fetchers.html_utils import (
    _html_to_text,
    _html_to_snippet,
    _html_to_markdown,
    _html_to_multiline,
)
from fetchers.parsing import (
    parse_markdown_jobs,
    _parse_json_jobs,
    _is_non_job_url,
    _looks_like_job_title,
    _blacklist_filter,
    _is_generic_pipeline_title,
)
from fetchers.firecrawl import (
    fetch_firecrawl_scrape,
    get_firecrawl_change_statuses,
    get_scrape_statuses,
    FIRECRAWL_JOBS_SCHEMA,
    _fetch_local_scrape,
    _enrich_blind_jobs,
)
from fetchers.legacy import (
    # ATS / company fetchers
    fetch_greenhouse,
    fetch_workday_api,
    fetch_lever,
    fetch_ashby,
    fetch_workable,
    fetch_recruitee,
    fetch_teamtailor_rss,
    fetch_bamboohr,
    fetch_successfactors,
    fetch_adp_json,
    fetch_amazon_jobs,
    fetch_apple_jobs,
    fetch_unops_widget,
    # Job boards
    fetch_algolia_board,
    fetch_firecrawl_board,
    fetch_reliefweb_board,
    fetch_impactpool_board,
    fetch_datadotorg_board,
    fetch_arbeitnow_board,
    fetch_remotive_board,
    fetch_wwr_board,
    fetch_hn_whoishiring_board,
    fetch_idealist_board,
    fetch_fastforward_board,
    fetch_linkedin_board,
    # Adapter-specific helpers used by tests / sibling scripts
    _parse_hn_comment,
    _parse_successfactors_tiles,
    _sf_base_url,
    _adp_cid,
    _adp_location,
    _adp_job_url,
    _adp_snippet,
    _fetch_unops_job_detail,
)
