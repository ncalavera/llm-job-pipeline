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
from fetchers.ats.greenhouse import fetch_greenhouse
from fetchers.ats.workday import fetch_workday_api
from fetchers.ats.lever import fetch_lever
from fetchers.ats.ashby import fetch_ashby
from fetchers.ats.workable import fetch_workable
from fetchers.ats.recruitee import fetch_recruitee
from fetchers.ats.teamtailor import fetch_teamtailor_rss
from fetchers.ats.bamboohr import fetch_bamboohr
from fetchers.ats.successfactors import (
    fetch_successfactors,
    _parse_successfactors_tiles,
    _sf_base_url,
)
from fetchers.ats.adp import fetch_adp_json, _adp_cid, _adp_location, _adp_job_url, _adp_snippet
from fetchers.ats.amazon import fetch_amazon_jobs
from fetchers.ats.apple import fetch_apple_jobs
from fetchers.ats.unops import fetch_unops_widget, _fetch_unops_job_detail
from fetchers.boards.algolia import fetch_algolia_board
from fetchers.boards.firecrawl_board import fetch_firecrawl_board
from fetchers.boards.reliefweb import fetch_reliefweb_board
from fetchers.boards.impactpool import fetch_impactpool_board
from fetchers.boards.datadotorg import fetch_datadotorg_board
from fetchers.boards.arbeitnow import fetch_arbeitnow_board
from fetchers.boards.remotive import fetch_remotive_board
from fetchers.boards.wwr import fetch_wwr_board
from fetchers.boards.hn_whoishiring import fetch_hn_whoishiring_board, _parse_hn_comment
from fetchers.boards.idealist import fetch_idealist_board
from fetchers.boards.fastforward import fetch_fastforward_board
from fetchers.boards.linkedin import fetch_linkedin_board

# Auto-discover any extra adapters dropped into ats/ or boards/ (one-file sources).
from fetchers import ats as _ats  # noqa: F401
from fetchers import boards as _boards  # noqa: F401
