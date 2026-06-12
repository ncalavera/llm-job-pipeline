"""Shared configuration: paths, keyword lists, blacklists.

For a public reusable pipeline:
- Paths are derived from PROJECT_ROOT (the directory containing /scripts).
- Blacklists below are EXAMPLES — extend them for your own search. The
  comments in each block tell you what they catch.
- Region keywords are generic Europe / US / Remote buckets used by the
  dashboard. Adjust REGION_EUROPE / REGION_US for your target geography.
"""

import os
import sys
from pathlib import Path

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
# Relevance keywords (matched against job title, case-insensitive)
# Tweak for your target functions.
# ---------------------------------------------------------------------------

RELEVANCE_HIGH = [
    "chief of staff", "head of", "director", "vp ", "vp,", "vice president",
    "general manager", "gm,", "gm ", " gm,", " gm ",
]
RELEVANCE_MEDIUM = [
    "program manager", "product manager", "product owner",
    "project manager", "community", "impact",
    "operations manager", "strategy", "coordinator", "partnerships",
    "philanthropy", "grants", "grantmaking", "social impact",
    "sustainability", "engagement",
]
RELEVANCE_LOW = [
    "analyst", "associate", "officer", "specialist", "advisor",
    "consultant", "researcher", "team lead",
]

# ---------------------------------------------------------------------------
# Location keywords — adjust to your target region.
# ---------------------------------------------------------------------------

LOCATION_KEYWORDS = [
    # Europe-focused defaults
    "berlin", "london", "lisbon", "europe", "european", "emea",
    "uk", "germany", "portugal", "spain", "poland", "remote",
    "amsterdam", "netherlands", "brussels", "belgium",
    "oslo", "norway", "vienna", "austria", "zurich", "switzerland",
    "rome", "italy", "paris", "france", "worldwide",
]

REGION_EUROPE = [
    "london", "berlin", "lisbon", "uk", "germany", "portugal", "spain",
    "poland", "europe", "european", "emea",
    "amsterdam", "netherlands", "brussels", "belgium",
    "oslo", "norway", "vienna", "austria", "zurich", "switzerland",
    "rome", "italy", "geneva", "paris", "munich",
]
REGION_US = [
    "san francisco", "new york", "nyc", "washington", "dc", "seattle",
    "houston", "usa", "us ", " us", "ca ", "wa ", "ny ",
]
REGION_REMOTE = ["remote"]

# ---------------------------------------------------------------------------
# Location blacklist — vacancies whose location matches any term are excluded
# from the dashboard. Checked BEFORE LOCATION_KEYWORDS so "Remote, USA" doesn't
# accidentally match "remote".
# Replace this list with locations that don't fit YOUR job search.
# ---------------------------------------------------------------------------

LOCATION_BLACKLIST = [
    # Example: a Europe-based searcher excluding US/Canada-only postings.
    # Generic country markers (matched as a substring of the location text):
    ", usa", ", united states", ", us",
    "remote, usa", "remote - usa", "remote (usa)",
    "remote, united states", "remote - united states", "remote (united states)",
    "united states, north america",
    "canada,", "canada, north america",
    # Add specific US cities here if you want to exclude them by name, e.g.:
    # "san francisco", "new york", "nyc", "boston", "seattle",
]

USA_EXCLUDE_LOCATIONS = LOCATION_BLACKLIST  # backward-compat alias

# ---------------------------------------------------------------------------
# LLM score threshold — vacancies below this are auto-archived.
# Only unseen vacancies are archived; liked/passed (user decisions) are kept.
# ---------------------------------------------------------------------------

LLM_SCORE_THRESHOLD = 20

# ---------------------------------------------------------------------------
# Global blacklist — applied to ALL sources (companies + job boards).
# Removes obviously irrelevant titles BEFORE saving to DB. Add patterns when
# you see repeated low-score matches you keep archiving manually.
#
# Each entry is a substring; if it appears in `lower(title)` between word
# boundaries, the vacancy is dropped. See GLOBAL_BLACKLIST_SUBSTR below for
# stem matching (no word boundaries).
#
# This is an EXAMPLE list — keep what's relevant for you, drop the rest.
# ---------------------------------------------------------------------------

GLOBAL_BLACKLIST = [
    # Interns and students
    "intern", "internship", "apprentice", "apprenticeship",
    "new grad", "trainee",

    # Academic
    "phd", "postdoc", "professor", "lecturer", "faculty",
    "research scientist", "research associate",

    # Volunteer
    "volunteer", "volunteer position", "volunteer opportunity",

    # Engineering / tech
    "engineer", "developer", "software engineer",
    "backend", "frontend", "devops", "sre",
    "data scientist", "machine learning",
    "software architect", "solutions architect",
    "ml researcher", "technical lead", "technical project manager",
    "it support", "qa engineer", "quality assurance",

    # Finance / legal
    "accountant", "accounting", "auditor", "bookkeeper",
    "lawyer", "legal counsel", "paralegal", "tax", "counsel",

    # Medical / clinical
    "nurse", "physician", "clinician", "therapist",
    "pharmacist", "dentist", "midwife", "psychologist", "clinical",

    # Sales / GTM
    "account executive", "account manager", "account director",
    "sales development", "sales representative", "sales manager",
    "sales director", "sales", "bdr",
    "business development representative",

    # HR / recruiting
    "recruiter", "recruiting", "recruitment", "talent acquisition",
    "people operations", "payroll", "executive assistant",
    "human resources", "people & culture", "people and culture",

    # Marketing execution
    "marketing operations", "marketing automation",
    "email marketing", "lifecycle marketing", "demand generation",
    "product marketing", "content marketing", "marketing manager",

    # Support / admin
    "helpdesk", "help desk", "receptionist", "customer support agent",
    "data entry", "office services assistant",

    # Trades / manual
    "driver", "cleaner", "janitor", "guard", "storekeeper",
    "warehouse", "mechanic", "electrician", "construction",

    # Generic non-vacancy postings
    "expression of interest", "talent pool", "general application",
    "talent community",
]

# Partial-stem blacklist — matched via substring (no word boundaries).
# Use for word stems (e.g. "nutritio" catches nutrition*, nutritionist).
GLOBAL_BLACKLIST_SUBSTR = [
    "data center", "datacenter",
    "system admin", "systems admin",
    "epidemiolog",

    # Field-agnostic non-vacancy listings sometimes scraped from boards.
    # The comma suffix avoids false positives (e.g. "funding," won't hit
    # "crowdfunding manager"; "course," won't hit "concourse").
    "list of ",     # aggregator meta-listings ("List of places to find…")
    "funding,",     # grant / funding calls ("Funding, …")
    "course,",      # course listings ("Course, Intro to …")
    "summer school",# educational programmes, not jobs
    "bootcamp",     # training programmes, not jobs
    "training on",  # training events ("Training on M&E Methods …")
    "fellowship",   # fellowships / scholarships, not staff roles

    # Examples of field-specific stems to add for YOUR search (commented out —
    # these excluded roles the original author didn't want; adjust to taste):
    # "nutritio",      # nutritionist / nutrition-coordinator roles
    # "stagiaire",     # French intern contracts
    # "alternance",    # French apprenticeship contracts
]

# Description-level kill phrases — substring-matched against full_description.
# Kept narrow (visa/citizenship boilerplate only) to avoid false positives.
GLOBAL_BLACKLIST_DESC_SUBSTR = [
    "visa sponsorship not available",
    "must be a us citizen",
    "must be us citizen",
]

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
#   weworkremotely — remote jobs via category RSS; WWR_CATEGORIES overrides
#                    the default product,management-and-finance
#   hn_whoishiring — monthly Hacker News "Who is hiring?" thread (startup /
#                    engineering heavy; 30-day TTL matches the cadence)
#
# Leave JOB_BOARDS unset to fetch only your tracked companies.
# ---------------------------------------------------------------------------

_ALL_JOB_BOARDS = {
    "80k_hours": {
        "strategy": "algolia_api",
        "name": "80,000 Hours",
        "url": "https://jobs.80000hours.org",
        # Public Algolia credentials embedded in their site search — replace
        # with your own if 80k Hours changes them.
        "algolia_app_id": "W6KM1UDIB3",
        "algolia_api_key": "d1d7f2c8696e7b36837d5ed337c4a319",
        "algolia_index": "jobs_prod_super_ranked",
        "board_blacklist": [
            "career advising", "research", "researcher", "scientist",
            "engineer", "engineering", "developer", "software",
            "machine learning", "ml ", "sre", "devops", "infrastructure",
            "junior", "entry level", "apprentice", "trainee", "fellow,",
            "student", "data analyst", "data scientist", "statistician",
            "graphic design", "ux design", "ui design",
        ],
        "ttl_days": 3,
        "tier": "B",
        "free": True,
    },
    "reliefweb": {
        "strategy": "reliefweb_api",
        "name": "ReliefWeb",
        "url": "https://reliefweb.int/jobs",
        "board_blacklist": [
            "wash", "shelter", "mine action", "demining", "camp management",
        ],
        "ttl_days": 3,
        "tier": "B",
        "free": True,
    },
    "arbeitnow": {
        "strategy": "arbeitnow_api",
        "name": "Arbeitnow",
        "url": "https://www.arbeitnow.com",
        # How many API pages to pull per run (100 jobs/page).
        "pages": 3,
        "board_blacklist": [],
        "ttl_days": 3,
        "tier": "B",
        "free": True,
    },
    "remotive": {
        "strategy": "remotive_api",
        "name": "Remotive",
        "url": "https://remotive.com",
        # Remotive asks for very few API calls (max ~4/day): one request per
        # run, or one per category when REMOTIVE_CATEGORIES is set.
        "board_blacklist": [],
        "ttl_days": 3,
        "tier": "B",
        "free": True,
    },
    "weworkremotely": {
        "strategy": "wwr_rss",
        "name": "We Work Remotely",
        "url": "https://weworkremotely.com",
        # Category slugs for the RSS feeds; override with WWR_CATEGORIES.
        "default_categories": ["product", "management-and-finance"],
        "board_blacklist": [],
        "ttl_days": 3,
        "tier": "B",
        "free": True,
    },
    "hn_whoishiring": {
        "strategy": "hn_whoishiring",
        "name": "HN Who is hiring",
        "url": "https://news.ycombinator.com/submitted?id=whoishiring",
        "board_blacklist": [],
        # The thread is monthly — a 30-day TTL stops daily refetches.
        "ttl_days": 30,
        "tier": "C",
        "free": True,
    },
}


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
