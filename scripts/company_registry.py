"""Company registry — sole owner of company data, loaded from Supabase.

Eager-loads at import time (same as old CSV-based config.py).
Breaks the circular import by using db_conn directly (zero config imports).
"""

import json
import re
import sys
from pathlib import Path

from db_conn import get_conn

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Strategy validation sets
# ---------------------------------------------------------------------------

_STRATEGY_REQUIRES_SLUG = {
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "teamtailor_rss",
    "bamboohr",
    "smartrecruiters",
}
_STRATEGY_REQUIRES_URL = {"firecrawl_scrape", "unops_widget"}

# ---------------------------------------------------------------------------
# Company registry — built from Supabase (single source of truth)
# ---------------------------------------------------------------------------

# Failure signal: distinguishes a genuinely empty company table (fresh clone)
# from a transient DB outage. COMPANIES is {} in BOTH cases, so callers that key
# destructive onboarding on `len(COMPANIES) == 0` (/jobs-new) must also check
# this flag and HARD-STOP on a load failure instead of onboarding.
# IMPORTANT: read via `registry_load_failed()`, never `from company_registry
# import REGISTRY_LOAD_FAILED` — importing the bool snapshots it at import time
# and would miss a later flip; the function reads the live global.
REGISTRY_LOAD_FAILED: bool = False
REGISTRY_LOAD_ERROR: str | None = None


def registry_load_failed() -> bool:
    """True if the registry failed to load from the DB (outage), not empty table."""
    return REGISTRY_LOAD_FAILED


def _build_companies_from_db() -> dict:
    """Build COMPANIES dict from the active backend's company table.

    Only rows with a non-empty fetch_strategy and status = 'active' become
    monitored companies. ats_config JSONB is merged into the config dict for
    complex fields (Workday tenant/board, Firecrawl url overrides).

    Works identically on both backends — SQLite (simple mode) and Postgres
    (Supabase) — because it talks to ``get_conn()`` (the db_backend
    abstraction), not to an env var. Degrades gracefully to an empty registry
    ONLY on a genuine connection/query failure (e.g. fully offline unit tests),
    so importing this module never crashes.
    """
    global REGISTRY_LOAD_FAILED, REGISTRY_LOAD_ERROR
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT canonical_name, fetch_strategy, status, tier, careers_url,
                   ats_slug, ats_config, category
            FROM company
            WHERE status = 'active'
              AND fetch_strategy IS NOT NULL
              AND fetch_strategy != ''
        """)
        rows = cur.fetchall()
        cur.close()
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — registry must never crash import
        print(f"⚠ Company registry: backend unavailable, empty registry ({exc})", file=sys.stderr)
        REGISTRY_LOAD_FAILED = True
        REGISTRY_LOAD_ERROR = str(exc)
        return {}

    REGISTRY_LOAD_FAILED = False
    REGISTRY_LOAD_ERROR = None

    companies: dict[str, dict] = {}
    for name, strategy, status, tier, careers_url, ats_slug, ats_config, category in rows:
        tier_val = tier if tier in ("S", "A", "B", "C") else None

        config: dict = {
            "strategy": strategy,
            "status": status or "active",
            "tier": tier_val,
            "careers_url": careers_url or "",
        }

        if ats_slug:
            config["slug"] = ats_slug

        if ats_config and isinstance(ats_config, dict):
            config.update(ats_config)

        # firecrawl_scrape / unops_widget need 'url'; default to careers_url
        if strategy in ("firecrawl_scrape", "unops_widget") and "url" not in config:
            config["url"] = config["careers_url"]

        companies[name] = config

    return companies


COMPANIES = _build_companies_from_db()


# ---------------------------------------------------------------------------
# Alias index — built from Supabase aliases TEXT[] column
# ---------------------------------------------------------------------------


def _build_alias_index() -> dict[str, str]:
    """Build alias→canonical_name index from DB.

    All aliases are lowercased on read to prevent case-sensitivity bugs.
    Works on both backends; returns an empty index only on a genuine backend
    failure (offline tests).
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT canonical_name, aliases
            FROM company
            WHERE aliases IS NOT NULL AND array_length(aliases, 1) > 0
        """)
        rows = cur.fetchall()
        cur.close()
        conn.commit()
    except Exception:  # noqa: BLE001
        return {}

    index: dict[str, str] = {}
    for canonical, aliases in rows:
        for alias in aliases:
            lower_alias = alias.lower()
            # Skip exact canonical name (it's handled by _COMPANIES_LOWER
            # or COMPANIES dict); keep other aliases including lowercase
            # variants of canonical for non-monitored companies
            if alias == canonical:
                continue
            index[lower_alias] = canonical
    return index


_ALIAS_INDEX = _build_alias_index()

# Pre-built case-insensitive index of COMPANIES keys
_COMPANIES_LOWER: dict[str, str] = {k.lower(): k for k in COMPANIES}


# ---------------------------------------------------------------------------
# All known company names (including inactive) — replaces _ALL_CSV_NAMES
# ---------------------------------------------------------------------------


def _load_all_known_names() -> set[str]:
    """Return ALL canonical names from DB (including inactive/candidate).

    Works on both backends; returns an empty set only on a genuine backend
    failure (offline tests).
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT canonical_name FROM company")
        names = {r[0] for r in cur.fetchall()}
        cur.close()
        conn.commit()
        return names
    except Exception:  # noqa: BLE001
        return set()


_ALL_KNOWN_NAMES = _load_all_known_names()


# ---------------------------------------------------------------------------
# Parsing artifacts (board scraper non-org strings)
# ---------------------------------------------------------------------------


def _load_parsing_artifacts() -> set[str]:
    """Known parsing artifacts from board scrapers — not real org names.

    Optional: drop a JSON file at companies/phantom-org-decisions.json with
    shape {"parsing_artifacts": ["board scraper string", ...]} to suppress
    known fake-org strings produced by aggregator parsers.
    """
    path = PROJECT_ROOT / "companies" / "phantom-org-decisions.json"
    if not path.exists():
        return set()
    data = json.load(open(path, encoding="utf-8"))
    return set(data.get("parsing_artifacts", []))


PARSING_ARTIFACTS: set[str] = _load_parsing_artifacts()


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


def _normalize_org_whitespace(name: str) -> str:
    """Strip and collapse incidental whitespace before any name matching.

    A source's raw org string can carry leading/trailing or doubled whitespace
    that another source never introduces for the same real-world company — e.g.
    fetchers/boards/algolia.py's ``company_name`` field is not stripped, unlike
    its sibling board fetchers. Left as-is, that mismatch fails every lookup
    stage below, so ``ensure_company`` forks a second company row for the same
    org and every vacancy for it duplicates (make_vacancy_id embeds the raw org
    string). Whitespace-only: no case folding or legal-suffix stripping, which
    would risk merging genuinely distinct companies instead.
    """
    return re.sub(r"\s+", " ", name or "").strip()


def resolve_canonical_name(name: str) -> str:
    """Resolve a company name to its canonical form.

    Resolution order (after whitespace normalization):
      1. Exact match in COMPANIES → return as-is
      2. Case-insensitive match in COMPANIES → return canonical key
      3. lower(name) in alias index → return canonical
      4. Passthrough (unknown names kept, whitespace-normalized)
    """
    name = _normalize_org_whitespace(name)
    if name in COMPANIES:
        return name
    lower = name.lower()
    if lower in _COMPANIES_LOWER:
        return _COMPANIES_LOWER[lower]
    if lower in _ALIAS_INDEX:
        return _ALIAS_INDEX[lower]
    return name


# ---------------------------------------------------------------------------
# Registry validation (runs on import — warnings only)
# ---------------------------------------------------------------------------


def validate_company_registry():
    """Warn about missing fields and dangling aliases."""
    warnings = []

    # 1. Companies with strategy but missing required fields
    for name, cfg in COMPANIES.items():
        strategy = cfg.get("strategy", "")
        if strategy in _STRATEGY_REQUIRES_SLUG and not cfg.get("slug"):
            warnings.append(f"  {name}: {strategy} strategy needs 'ats_slug'")
        if strategy in _STRATEGY_REQUIRES_URL and not cfg.get("url"):
            warnings.append(
                f"  {name}: {strategy} strategy needs 'url' (ats_config or careers_url)"
            )

    # 2. Alias index entries pointing to non-existent canonical names
    for alias, target in _ALIAS_INDEX.items():
        if target not in _ALL_KNOWN_NAMES:
            warnings.append(f"  alias '{alias}' → '{target}' (target not in company table)")

    if warnings:
        print(f"\u26a0 Company registry warnings ({len(warnings)}):", file=sys.stderr)
        for w in warnings:
            print(w, file=sys.stderr)


validate_company_registry()
