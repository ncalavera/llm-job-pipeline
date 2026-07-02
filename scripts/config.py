"""Shared configuration — a thin façade over the settings loader.

Every tunable list, threshold, keyword and board definition lives in
``config/defaults.toml`` (neutral tool mechanics) and ``config/user_profile.md``
(personal taste). This module reads them through ``scripts/settings.py`` and
re-exports the same names it always has, so existing wide imports keep working
unchanged — but the DATA lives in TOML, not here.

For a public reusable pipeline:
- Paths are derived from PROJECT_ROOT (the directory containing /scripts).
- The only filtering that ships ON by default is UNIVERSAL_JUNK — postings that
  are not a specific open role for ANYONE (speculative / evergreen pipeline
  entries: talent pools, expressions of interest, general/open applications).
  Nothing tied to discipline, seniority, career stage, format, geography or
  sector ships on by default. Those are personal taste and activate only when
  the user opts in via their profile (config/user_profile.md, loaded by
  hard_filters.py). See EXCLUDE_COUNTRIES / EXCLUDE_TITLE_KEYWORDS.
- Numeric thresholds (LLM_SCORE_THRESHOLD, tier cutoffs) are neutral defaults
  from defaults.toml, overridable via environment variables.
"""

import os
import sys
from pathlib import Path

import settings

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VACANCIES_DIR = PROJECT_ROOT / "vacancies"
FIRECRAWL_CACHE = PROJECT_ROOT / ".firecrawl" / "vacancies"
FETCH_LOG_DIR = VACANCIES_DIR / "fetch_log"

PUBLIC_DIR = PROJECT_ROOT / "public"
REPORT_PATH = PUBLIC_DIR / "index.html"
DATA_JS_PATH = PUBLIC_DIR / "data.js"

# ---------------------------------------------------------------------------
# Private artifact zone — application artifacts + the personal case bank.
#
# Application prep is private data: which CV version was sent, cover letters,
# interview-question answers, links to research, and the case bank of personal
# stories/typical answers the agent pulls when drafting a letter. It lives ONLY
# in this gitignored zone and the database — never in public code or on the
# public dashboard (STRATEGY: "Application artifacts are private data ... never
# in public code").
#
# The location is a CONFIG KEY, never a hardcoded personal path: point
# JOBSEARCH_PRIVATE_DIR at your private space (e.g. a separate private repo).
# The default is a gitignored ``private/`` under the project root, so a fresh
# clone works out of the box for a nurse, a game designer or a policy analyst
# alike. Dirs are created on demand (ensure_private_dirs), not at import.
# ---------------------------------------------------------------------------

PRIVATE_DIR = Path(
    os.environ.get("JOBSEARCH_PRIVATE_DIR", str(PROJECT_ROOT / "private"))
).expanduser()

#: Personal stories / cases / typical answers, pulled by the agent when it drafts
#: a cover letter or question answers. Sits next to your user_profile.md.
CASE_BANK_DIR = PRIVATE_DIR / "case_bank"

#: Per-application artifacts (the CV version sent, the cover letter, saved
#: answers, research notes). Referenced from the ``application`` DB rows.
APPLICATION_ARTIFACTS_DIR = PRIVATE_DIR / "applications"


def ensure_private_dirs() -> None:
    """Create the private zone (case bank + application artifacts) on demand.

    Called by the writers (never at import — a plain read of config must not
    touch the filesystem). Idempotent.
    """
    for d in (CASE_BANK_DIR, APPLICATION_ARTIFACTS_DIR):
        d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Firecrawl SDK client (lazy singleton)
# ---------------------------------------------------------------------------

_firecrawl_client = None
_firecrawl_import_failed = False


def get_firecrawl_client():
    """Return a shared Firecrawl client, or None if SDK not installed."""
    global _firecrawl_client, _firecrawl_import_failed
    if _firecrawl_import_failed:
        return None
    if _firecrawl_client is None:
        try:
            from firecrawl import Firecrawl

            _firecrawl_client = Firecrawl()
        except ImportError:
            _firecrawl_import_failed = True
            return None
    return _firecrawl_client


# ---------------------------------------------------------------------------
# Company registry — loaded from Supabase via company_registry module
# ---------------------------------------------------------------------------

from company_registry import (  # noqa: E402
    COMPANIES,
    PARSING_ARTIFACTS,
    _ALL_KNOWN_NAMES as _ALL_CSV_NAMES,  # backward-compat alias
    _COMPANIES_LOWER,
    _STRATEGY_REQUIRES_SLUG,
    _STRATEGY_REQUIRES_URL,
    resolve_canonical_name,
)


# ---------------------------------------------------------------------------
# Region keyword buckets — neutral DATA loaded from config/defaults.toml. Each
# bucket (the TOML key, e.g. "europe"/"us"/"remote") maps to its keyword list.
# No bucket is privileged: this is a plain lookup table, not a preference. The
# real geography classification lives in geo.py (computed on the fly); the
# stored `region` field is display-only legacy. No code branches on a specific
# bucket name here — callers index REGION_KEYWORDS by a bucket value if needed.
# ---------------------------------------------------------------------------

#: {bucket_name: [keywords]} — bucket names are data keys from TOML, not code.
REGION_KEYWORDS: dict[str, list[str]] = {
    bucket: list(keywords) for bucket, keywords in settings.region_keywords().items()
}

# ---------------------------------------------------------------------------
# Personal HARD filters — geography + title-keyword exclusions.
#
# These are NOT hardcoded to anyone's taste. They come from the
# `## HARD_FILTERS` section of YOUR user profile (config/user_profile.md) and
# are EMPTY by default — so out of the box nothing is dropped on geography or
# on a title's discipline. Edit them with /jobs-profile, or by hand in the
# profile. See scripts/hard_filters.py for the loader.
# ---------------------------------------------------------------------------

from hard_filters import load_hard_filters  # noqa: E402

_HARD_FILTERS = load_hard_filters()

#: Countries the user never wants. A vacancy is dropped only when EVERY one of
#: its locations is in one of these countries. Empty → drop nothing on geo.
EXCLUDE_COUNTRIES = list(_HARD_FILTERS["exclude_countries"])

#: Title keywords the user never wants (e.g. "engineer"). Matched on word
#: boundaries against the job title. Empty → drop nothing on title discipline.
EXCLUDE_TITLE_KEYWORDS = list(_HARD_FILTERS["exclude_title_keywords"])

# --- Geo policy (region-based, see scripts/geo.py + filter_vacancies.py) ------
#: World regions the user hard-bans (e.g. "africa", "south_asia"). A vacancy is
#: dropped when EVERY location resolves to a banned region — unless the country
#: is whitelisted in GEO_KEEP_COUNTRIES or the role is remote. Empty → ban none.
GEO_BAN_REGIONS = list(_HARD_FILTERS["ban_regions"])

#: Countries that override a region ban (e.g. keep "georgia" though "ex_ussr" is
#: banned). Exact (canonical) country names. Empty → no overrides.
GEO_KEEP_COUNTRIES = list(_HARD_FILTERS["keep_countries"])

#: Extra explicit country bans on top of GEO_BAN_REGIONS. Empty → none.
GEO_BAN_COUNTRIES = list(_HARD_FILTERS["ban_countries"])

#: When True, drop roles the scorer flags us_eligibility == "us_only"
#: (US/Canada-residency-bound, unreachable from abroad). Default False.
GEO_BAN_US_ONLY = bool(_HARD_FILTERS["ban_us_only"])

#: Regions where an on-site role gets NO soft penalty (e.g. "europe"). Remote
#: roles are never penalised regardless. Empty → every on-site role penalised.
GEO_ONSITE_OK_REGIONS = list(_HARD_FILTERS["onsite_ok_regions"])

#: Points subtracted from the score for an on-site role outside
#: GEO_ONSITE_OK_REGIONS (makes remote preferable). 0 → no soft penalty.
GEO_ONSITE_PENALTY = int(_HARD_FILTERS["onsite_penalty"])

# Canonicalised, ready-to-query caches built ONCE here (not per vacancy) so the
# pre-score filter, the post-score ban, and the soft penalty share one normalised
# view of the policy. geo.canonical_country only depends on settings → no cycle.
from geo import canonical_country as _canon  # noqa: E402

GEO_BANNED_COUNTRIES = frozenset(
    _canon(c) for c in (list(EXCLUDE_COUNTRIES) + list(GEO_BAN_COUNTRIES)) if _canon(c)
)
GEO_BANNED_REGIONS = frozenset(r.lower().strip() for r in GEO_BAN_REGIONS if r and r.strip())
GEO_KEEP_COUNTRIES_SET = frozenset(_canon(c) for c in GEO_KEEP_COUNTRIES if _canon(c))
GEO_ONSITE_OK_SET = frozenset(r.lower().strip() for r in GEO_ONSITE_OK_REGIONS if r and r.strip())
#: True when ANY geography ban is configured (explicit country or whole region).
GEO_ACTIVE = bool(GEO_BANNED_COUNTRIES or GEO_BANNED_REGIONS)

# Backward-compat: a few call sites still import LOCATION_BLACKLIST as a list of
# location-text substrings. It is now empty by default and only filled from the
# profile's exclude_countries (so legacy substring matching keeps working for
# users who opt in). The authoritative geo check is country-based via
# EXCLUDE_COUNTRIES in filter_vacancies.py.
LOCATION_BLACKLIST = list(EXCLUDE_COUNTRIES)

# ---------------------------------------------------------------------------
# LLM score threshold — vacancies below this are auto-archived.
# Only unseen vacancies are archived; liked/passed (user decisions) are kept.
#
# Default comes from defaults.toml ([thresholds] llm_score_threshold). The
# LLM_SCORE_THRESHOLD env var overrides it (e.g. 0 disables auto-archive).
# ---------------------------------------------------------------------------

LLM_SCORE_THRESHOLD = int(
    os.environ.get("LLM_SCORE_THRESHOLD", settings.thresholds()["llm_score_threshold"])
)

# ---------------------------------------------------------------------------
# Latency-protection thresholds (see architecture-notes plan, KTD1).
#
# The pipeline's scarce resource is the candidate's decision attention on the
# rare high-fit roles, not the volume of vacancies. These thresholds protect
# that decision instead of optimising for a fresh feed.
#
# PROTECT_SCORE and SLA_SCORE are unified at 60 (Open Question #1 resolved):
# every SLA-tracked role is also protected from silent archive/pass, so a
# 60-69 role can no longer leak before it is ever seen.
# ---------------------------------------------------------------------------

#: Unseen roles at/above this are never silently archived or auto-passed; they
#: flip to `expiring`, get a loud alert, and stay visible for a decision.
PROTECT_SCORE = int(os.environ.get("PROTECT_SCORE", 60))

#: Roles at/above this must move (be decided) within SLA_DAYS or surface as
#: "stuck". Unified with PROTECT_SCORE.
SLA_SCORE = int(os.environ.get("SLA_SCORE", 60))

#: Decision SLA, in days. A SLA_SCORE+ role untouched longer than this is stuck.
SLA_DAYS = int(os.environ.get("SLA_DAYS", 7))

#: A company's "applyable" roles are open vacancies scoring at/above this that
#: pass red flags (see scripts/report applyable-count computation).
APPLYABLE_SCORE = int(os.environ.get("APPLYABLE_SCORE", 60))

#: A vacancy not confirmed by its source for this many days is shown as
#: "probably closed" on the catalog card.
STALE_SOURCE_DAYS = int(os.environ.get("STALE_SOURCE_DAYS", 14))

#: The catalog hides vacancies scoring below this by default ("показать все"
#: reveals them).
CATALOG_MIN_SCORE = int(os.environ.get("CATALOG_MIN_SCORE", 40))

# ---------------------------------------------------------------------------
# Universal junk — applied to ALL sources (companies + job boards).
#
# Catches postings that are not a specific open role for ANYONE: speculative /
# evergreen pipeline entries (talent pools, expressions of interest,
# general/open applications). Contains NO geography and NO discipline, seniority,
# career stage, format or sector — those are personal taste and live in the
# profile's EXCLUDE_TITLE_KEYWORDS. The data ships in defaults.toml ([junk]).
#
# `UNIVERSAL_JUNK` is matched on word boundaries against lower(title);
# `UNIVERSAL_JUNK_SUBSTR` is matched as a substring (no word boundaries).
# ---------------------------------------------------------------------------

_JUNK = settings.junk()
UNIVERSAL_JUNK = list(_JUNK["words"])
UNIVERSAL_JUNK_SUBSTR = list(_JUNK["substr"])

# ---------------------------------------------------------------------------
# Combined title blacklist used by the pre-score filter
# (filters.title_words_blacklisted).
#
# = UNIVERSAL_JUNK + the user's personal EXCLUDE_TITLE_KEYWORDS (from the
# profile, empty by default). Importers keep using GLOBAL_BLACKLIST /
# GLOBAL_BLACKLIST_SUBSTR; the universal half is always present, the personal
# half appears only when the user lists keywords. With an empty profile this
# equals UNIVERSAL_JUNK, so no discipline/format is dropped for anyone.
# ---------------------------------------------------------------------------

GLOBAL_BLACKLIST = list(UNIVERSAL_JUNK) + list(EXCLUDE_TITLE_KEYWORDS)
GLOBAL_BLACKLIST_SUBSTR = list(UNIVERSAL_JUNK_SUBSTR)

# Description-level kill phrases — substring-matched against full_description.
# Kept narrow (visa/citizenship boilerplate only) to avoid false positives.
# Ships in defaults.toml ([junk] desc_substr).
GLOBAL_BLACKLIST_DESC_SUBSTR = list(_JUNK["desc_substr"])

# ---------------------------------------------------------------------------
# Job board aggregators — fetched on a freshness schedule (TTL days).
# Each board needs a strategy implemented in fetchers.py. All six below use
# free APIs / feeds (no key) and work out of the box.
#
# Boards are OPT-IN. Each is niche and floods a general search when it does
# not match your sectors. By default NO boards are fetched. Enable the ones
# you want via the JOB_BOARDS env var:
#
#     JOB_BOARDS=remotive,weworkremotely   # remote product/marketing roles
#     JOB_BOARDS=arbeitnow                 # Europe / visa-sponsorship tech
#     JOB_BOARDS=hn_whoishiring            # startups, monthly HN thread
#     JOB_BOARDS=80k_hours,reliefweb       # EA/AI-safety + humanitarian
#     JOB_BOARDS=all                       # every defined board
#
# Which board fits which search:
#   80k_hours      — effective altruism / AI safety / policy
#   reliefweb      — humanitarian / development NGOs
#   arbeitnow      — European tech; remote + (when listed) visa-sponsorship
#                    flags; ARBEITNOW_VISA_ONLY=1 keeps sponsorship-only jobs
#   remotive       — remote-first jobs; REMOTIVE_CATEGORIES=product,marketing
#                    narrows by their category slugs (one request per category)
#   weworkremotely — remote jobs via category RSS; defaults to ALL categories,
#                    narrow with WWR_CATEGORIES (comma list of slugs)
#   hn_whoishiring — monthly Hacker News "Who is hiring?" thread (startup /
#                    engineering heavy; 30-day TTL matches the cadence)
#
# Leave JOB_BOARDS unset to fetch only your tracked companies.
# ---------------------------------------------------------------------------

# All defined boards, loaded from defaults.toml ([boards.*]). Each ships with an
# empty board_blacklist; narrow per board via the documented env vars.
_ALL_JOB_BOARDS = settings.boards()


def _select_enabled_boards() -> dict:
    """Return the boards enabled via the JOB_BOARDS env var.

    Default (unset/empty) → no boards. ``JOB_BOARDS=all`` → every defined
    board. Otherwise a comma-separated list of board ids (e.g.
    ``80k_hours,reliefweb``). Unknown ids are ignored with a warning.
    """
    raw = os.environ.get("JOB_BOARDS", "").strip()
    if not raw:
        return {}
    if raw.lower() == "all":
        return dict(_ALL_JOB_BOARDS)
    enabled = {}
    for board_id in (s.strip() for s in raw.split(",") if s.strip()):
        if board_id in _ALL_JOB_BOARDS:
            enabled[board_id] = _ALL_JOB_BOARDS[board_id]
        else:
            print(
                f"  WARNING: JOB_BOARDS lists unknown board '{board_id}' "
                f"(known: {', '.join(_ALL_JOB_BOARDS)})",
                file=sys.stderr,
            )
    return enabled


#: Boards actually fetched this run. Empty unless JOB_BOARDS opts in.
JOB_BOARDS = _select_enabled_boards()
