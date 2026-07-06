"""Company registry — sole owner of company data, loaded from Supabase.

Eager-loads at import time (same as old CSV-based config.py).
Breaks the circular import by using db_conn directly (zero config imports).
"""

import json
import re
import sys
import time
import unicodedata
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

# Ordinary DB contention (a lock or statement-timeout on the company SELECT) is
# transient: the connection is alive and a retry a moment later usually clears
# it. We retry a bounded number of times before declaring the backend
# unreachable, so a few seconds of contention no longer trips REGISTRY_LOAD_FAILED
# and hard-aborts a healthy run. A genuine outage still fails every attempt and
# sets the flag as before.
_REGISTRY_LOAD_ATTEMPTS = 3
_REGISTRY_LOAD_BACKOFF_S = 0.5


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
    rows = None
    last_exc: Exception | None = None
    conn = None
    for attempt in range(_REGISTRY_LOAD_ATTEMPTS):
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
            break
        except Exception as exc:  # noqa: BLE001 — registry must never crash import
            last_exc = exc
            # Clear the aborted transaction so the next attempt can run on the
            # same live connection; a genuinely dead connection is reconnected
            # by get_conn()'s own liveness check on the next attempt.
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            if attempt + 1 < _REGISTRY_LOAD_ATTEMPTS:
                time.sleep(_REGISTRY_LOAD_BACKOFF_S * (attempt + 1))
    else:
        print(
            f"⚠ Company registry: backend unavailable after {_REGISTRY_LOAD_ATTEMPTS} "
            f"attempts, empty registry ({last_exc})",
            file=sys.stderr,
        )
        REGISTRY_LOAD_FAILED = True
        REGISTRY_LOAD_ERROR = str(last_exc)
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
# Fuzzy company-name matching (board-variant dedup)
# ---------------------------------------------------------------------------
#
# The exact canonical_name / alias lookups above miss the board-sourced name
# VARIANTS of a company we already track — "EBRD - European Bank for
# Reconstruction and Development" for an existing "EBRD", "Save the Children
# International" for "Save the Children". Without a tolerance match those land
# as brand-new candidate rows that get re-enriched and re-WANT-scored (wasted
# Firecrawl/Exa/LLM spend). ``company_name_variants_match`` is the tolerance
# gate: the save layer (``ensure_company``) uses it to MERGE an incoming board
# name into the existing row instead of inserting a duplicate.
#
# Precision over recall on purpose: a wrong merge folds a real company into
# another and reassigns its vacancies, while a miss only re-scores a duplicate
# (today's status quo). So the rule stays tight — normalized-token equality, or
# an "ACRONYM - Full Name" containment where the single extra token is exactly
# the acronym of the shorter name — and nothing looser.

# Dropped before matching: articles/prepositions that never distinguish orgs.
_MATCH_STOPWORDS = frozenset(
    {"the", "of", "for", "and", "a", "an", "de", "la", "to", "in", "on", "at", "by", "with"}
)

# Trailing legal/org suffixes stripped so "Resolution Foundation" == "Resolution"
# and "Save the Children International" == "Save the Children". Trailing-only:
# a suffix word buried mid-name (e.g. leading "International Fund…") is kept.
_MATCH_ORG_SUFFIXES = frozenset(
    {
        "foundation",
        "international",
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "gmbh",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "ag",
        "sa",
        "ev",
        "bv",
        "nv",
        "kg",
        "group",
        "holdings",
        "worldwide",
    }
)

_MATCH_PUNCT_RE = re.compile(r"[^0-9a-z]+")


def _normalize_name_tokens(name: str) -> list[str]:
    """Lowercase, fold accents, strip punctuation, drop stopwords / trailing
    digit / trailing org suffixes, and return the remaining significant tokens
    (order preserved).

    Examples:
      "EBRD - European Bank for Reconstruction and Development"
          → [ebrd, european, bank, reconstruction, development]
      "Save the Children International" → [save, children]
      "Resolution Foundation"          → [resolution]
      "Code.X 0"                       → [code, x]
      "Médecins Sans Frontières"       → [medecins, sans, frontieres]
    """
    lowered = _normalize_org_whitespace(name).lower()
    # Fold accents (NFKD + drop combining marks) so "Médecins" == "Medecins";
    # without this the ASCII-only punctuation regex shreds accented org names
    # (common in this domain) into garbage tokens that never dedup.
    lowered = unicodedata.normalize("NFKD", lowered)
    lowered = "".join(ch for ch in lowered if not unicodedata.combining(ch))
    tokens = [t for t in _MATCH_PUNCT_RE.sub(" ", lowered).split() if t]
    tokens = [t for t in tokens if t not in _MATCH_STOPWORDS]
    # Drop a stray trailing pure-digit token ("Code.X 0" → "Code.X").
    while len(tokens) > 1 and tokens[-1].isdigit():
        tokens.pop()
    # Strip trailing org suffixes ("Resolution Foundation" → "Resolution").
    while len(tokens) > 1 and tokens[-1] in _MATCH_ORG_SUFFIXES:
        tokens.pop()
    return tokens


def company_name_variants_match(a: str, b: str) -> bool:
    """True if two raw company-name strings denote the same organisation.

    Matches on either:
      * normalized-token equality — same significant tokens after lowercase /
        punctuation / stopword / suffix normalization (word order ignored); or
      * "ACRONYM - Full Name" containment — one token set is a proper subset of
        the other and the single extra token is exactly the acronym (initials)
        of the shorter set, e.g. "IFAD - International Fund for Agricultural
        Development" ↔ "International Fund for Agricultural Development".

    Deliberately does NOT match on generic single-token containment (guards
    "Via" ↔ "[via Fast Forward]", "Apple" ↔ "Apple CSR") or on partial token
    overlap ("Henley & Partners" ↔ "Global Partners").
    """
    ta = _normalize_name_tokens(a)
    tb = _normalize_name_tokens(b)
    sa, sb = set(ta), set(tb)
    if not sa or not sb:
        return False
    if sa == sb:
        # A lone generic token ("The Foundation" → {foundation}) carries no
        # identity — refuse to merge two different orgs on it. Distinctive
        # single tokens ("Resolution" ↔ "Resolution Foundation") still match.
        if len(sa) == 1 and next(iter(sa)) in _MATCH_ORG_SUFFIXES:
            return False
        return True
    if sa < sb:
        small_tokens, big = ta, sb
    elif sb < sa:
        small_tokens, big = tb, sa
    else:
        return False
    small = set(small_tokens)
    extra = big - small
    if len(extra) != 1:
        return False
    acronym = next(iter(extra))
    # The extra token must be an acronym (2–6 letters) of the shorter name;
    # this needs ≥2 words, so single-token containment can never match here.
    if not acronym.isalpha() or not (2 <= len(acronym) <= 6):
        return False
    # In-order initials (first occurrence per token) — NOT sorted letters, so
    # an anagram like "FAI" cannot claim "Fund International Agricultural".
    seen: set[str] = set()
    initials = []
    for t in small_tokens:
        if t not in seen:
            seen.add(t)
            initials.append(t[0])
    return acronym == "".join(initials)


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
