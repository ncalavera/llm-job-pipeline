"""Company registry — sole owner of company data, loaded from Supabase.

Eager-loads at import time (same as old CSV-based config.py).
Breaks the circular import by using db_conn directly (zero config imports).
"""

import json
import sys
from pathlib import Path

from db_conn import get_conn

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Strategy validation sets
# ---------------------------------------------------------------------------

_STRATEGY_REQUIRES_SLUG = {"greenhouse", "lever", "ashby", "workable", "recruitee", "teamtailor_rss", "bamboohr"}
_STRATEGY_REQUIRES_URL = {"firecrawl_scrape", "unops_widget"}

# ---------------------------------------------------------------------------
# Company registry — built from Supabase (single source of truth)
# ---------------------------------------------------------------------------


def _build_companies_from_db() -> dict:
    """Build COMPANIES dict from Supabase company table.

    Only rows with a non-empty fetch_strategy and status != 'inactive'
    become monitored companies. ats_config JSONB is merged into the config
    dict for complex fields (Workday tenant/board, Firecrawl url overrides).

    Degrades gracefully when no database is configured (e.g. offline unit
    tests): returns an empty registry instead of exiting, so importing this
    module never requires a live connection.
    """
    import os
    if not (os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_DIRECT_URL")):
        return {}

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

    companies: dict[str, dict] = {}
    for (name, strategy, status, tier, careers_url,
         ats_slug, ats_config, category) in rows:

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
    Returns an empty index when no database is configured (offline tests).
    """
    import os
    if not (os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_DIRECT_URL")):
        return {}

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

    Returns an empty set when no database is configured (offline tests).
    """
    import os
    if not (os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_DIRECT_URL")):
        return set()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT canonical_name FROM company")
    names = {r[0] for r in cur.fetchall()}
    cur.close()
    conn.commit()
    return names


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


def resolve_canonical_name(name: str) -> str:
    """Resolve a company name to its canonical form.

    Resolution order:
      1. Exact match in COMPANIES → return as-is
      2. Case-insensitive match in COMPANIES → return canonical key
      3. lower(name) in alias index → return canonical
      4. Passthrough (unknown names kept as-is)
    """
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
            warnings.append(f"  {name}: {strategy} strategy needs 'url' (ats_config or careers_url)")

    # 2. Alias index entries pointing to non-existent canonical names
    for alias, target in _ALIAS_INDEX.items():
        if target not in _ALL_KNOWN_NAMES:
            warnings.append(f"  alias '{alias}' → '{target}' (target not in company table)")

    if warnings:
        print(f"\u26a0 Company registry warnings ({len(warnings)}):", file=sys.stderr)
        for w in warnings:
            print(w, file=sys.stderr)


validate_company_registry()
