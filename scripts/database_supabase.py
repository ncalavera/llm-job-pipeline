"""Supabase-backed data access layer.

Direct Postgres via psycopg2. Singleton connection, autocommit OFF — callers
commit at logical checkpoints.
"""

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from dateutil import parser as dateutil_parser

from company_registry import (
    COMPANIES,
    company_name_variants_match,
    resolve_canonical_name,
)
import settings
from config import (
    LLM_SCORE_THRESHOLD,
    BOARD_STALE_DAYS,
    PROTECT_SCORE,
    VACANCIES_DIR,
    GEO_BANNED_COUNTRIES,
    GEO_BANNED_REGIONS,
    GEO_KEEP_COUNTRIES_SET,
    GEO_BAN_US_ONLY,
    DASHBOARD_TZ,
)
from geo import country_banned, is_remote_mode

# Json / RealDictCursor come from db_backend so they work under both the
# Supabase (psycopg2) and the local SQLite backend without importing psycopg2.
from db_backend import Json, RealDictCursor
from db_conn import get_conn, close_conn
import filters

# filters caches the blacklist (a compiled alternation pattern) from config at
# its own import time. When the pipeline reloads config under a new profile it
# also reloads this module; mirror that here so the cached pattern rebinds to
# the fresh blacklist (matches the pre-refactor behaviour, when the pattern
# lived in this module and was rebuilt on every reload).
import importlib as _importlib

_importlib.reload(filters)
from quality import clean_description

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Fetch status constants
# ---------------------------------------------------------------------------

FETCH_STATUS_OK = "ok"
FETCH_STATUS_NO_DATA = "no_data"
# Reason codes (U9 / WS6) that disambiguate the old, overloaded no_data.
# render_ok_zero = a real successful fetch that is genuinely empty (healthy);
# credit_exhausted / js_required stay in the "broken" bucket below.
FETCH_STATUS_RENDER_OK_ZERO = "render_ok_zero"
FETCH_STATUS_CREDIT_EXHAUSTED = "credit_exhausted"

# Statuses that are NOT operational errors (a genuinely-empty listing is fine).
_FETCH_STATUS_NON_ERROR = (
    FETCH_STATUS_OK,
    FETCH_STATUS_NO_DATA,
    FETCH_STATUS_RENDER_OK_ZERO,
)


def is_fetch_error(status: str | None) -> bool:
    """Check if fetch_status represents an error (not ok/no_data/render_ok_zero)."""
    return bool(status) and status not in _FETCH_STATUS_NON_ERROR


# ---------------------------------------------------------------------------
# Pure functions (no DB)
# ---------------------------------------------------------------------------

# Location-parsing DATA from config/defaults.toml ([geo.city_country],
# [geo.work_mode]). The parse LOGIC lives here; only the maps live in TOML.
_CITY_COUNTRY = settings.geo_city_country()

_WORK_MODE = settings.geo_work_mode()
_REMOTE_KW = _WORK_MODE["remote"]
_HYBRID_KW = _WORK_MODE["hybrid"]


def classify_region(location: str) -> str | None:
    """Classify a location string into a coarse region label.

    Structural classification via geo.py's country/city → bucket mapping — no
    owner keyword lists, no region treated as "preferred". The stored `region`
    is display-only legacy; geo_bucket() is the authoritative on-the-fly geo.

    Returns "europe" | "americas" | None, matching the legacy field contract
    (callers stamp vacancy.region).
    """
    if not location:
        return None
    from geo import bucket_for_country

    bucket = bucket_for_country(location)
    if bucket in ("uk", "germany", "europe"):
        return "europe"
    if bucket == "us":
        return "americas"
    return None


def parse_location(location_str: str) -> dict:
    """Parse free-text location into {work_mode, region, country, city}."""
    if not location_str or not location_str.strip():
        return {"work_mode": None, "region": None, "country": None, "city": None}

    loc = location_str.strip()
    loc_lower = loc.lower()

    work_mode = "onsite"
    if any(kw in loc_lower for kw in _REMOTE_KW):
        work_mode = "remote"
    elif any(kw in loc_lower for kw in _HYBRID_KW):
        work_mode = "hybrid"

    city = None
    country = None
    for known_city, known_country in _CITY_COUNTRY.items():
        if known_city in loc_lower:
            city = known_city.title()
            country = known_country
            break

    region = classify_region(location_str)

    return {"work_mode": work_mode, "region": region, "country": country, "city": city}


# ---------------------------------------------------------------------------
# Content filters live in scripts/filters.py — the save_* paths call
# filters.title_words_blacklisted / is_content_junk / has_enough_content
# directly (single home, no DAL-local aliases).
# ---------------------------------------------------------------------------

ARCHIVE_TTL_DAYS = 90


def _gate_description(job: dict) -> str | None:
    """Run job['full_description'] through the quality gate, in place.

    Delegates the cleaning logic to quality.clean_description (single home); this
    DAL-local helper only applies the verdict to the job dict so both save_*
    paths share one mutator instead of inlining the same three lines twice.

    Strips a leading cookie banner; blanks the field when the text is pure
    boilerplate (cookie wall, error page, nav chrome, marketing/homepage dump).
    Returns the reject verdict ("cookie_wall"/"error_page"/"nav_junk"/
    "marketing_page") when boilerplate was dropped, else None. A merely
    short/empty description is left as-is — the snippet/URL fallback in
    filters.has_enough_content still applies.
    """
    raw = job.get("full_description") or ""
    if not raw.strip():
        return None
    cleaned, verdict = clean_description(raw)
    if verdict in ("cookie_wall", "error_page", "nav_junk", "marketing_page"):
        job["full_description"] = ""
        return verdict
    if cleaned is not None:
        job["full_description"] = cleaned
    return None


def _sanitize_title(title: str) -> str:
    """Strip markdown/HTML artifacts from scraped titles.

    Applied before make_vacancy_id() so dedup_hash is clean.
    """
    import html as _html_mod

    # A fetcher may hand back a row whose title key is present but None
    # (dict.get(k, "") returns None, not the default, when k exists as None).
    # Coerce to "" so a single malformed row can't crash the whole batch —
    # _gate_job then skips it as empty_title. Covers every call site at once
    # (the pre-loop batch_hashes precompute included, which runs before the
    # per-row gate could catch it).
    title = title or ""
    title = _html_mod.unescape(title)  # &amp; → &, &nbsp; → space
    title = re.sub(r"\*\*", "", title)  # markdown bold
    title = re.sub(r"\s*!\[.*$", "", title)  # markdown image refs
    title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)  # [text](url) → text
    title = re.sub(
        r"<(?:br|strong|em|b|i|span)[^>]*>", "", title, flags=re.IGNORECASE
    )  # light HTML tags
    title = re.sub(r"</(?:strong|em|b|i|span)>", "", title, flags=re.IGNORECASE)
    title = re.sub(r"  +", " ", title)  # collapse double spaces
    return title.strip()


# Trailing parenthetical that only restates work-mode / location, e.g.
# "(Remote)", "(Remote, United States)", "(Hybrid)", "(On-site)". Boards like
# Getro append these to the title, which would otherwise fork the dedup hash
# from the same role posted without the suffix. Only mode/location qualifiers
# are stripped — "(Spanish)", "(Maternity cover)" etc. stay, so genuinely
# distinct roles are not merged.
_TITLE_GEO_SUFFIX = re.compile(
    r"\s*\((?:remote|hybrid|on[\s-]?site|onsite|wfh|hq)\b[^)]*\)\s*$", re.I
)


def _normalize_title_for_dedup(title: str) -> str:
    """Collapse whitespace and drop trailing work-mode/location parentheticals."""
    t = title or ""
    prev = None
    while prev != t:
        prev = t
        t = _TITLE_GEO_SUFFIX.sub("", t).strip()
    return re.sub(r"\s+", " ", t).strip()


def make_vacancy_id(org: str, title: str, location: str = "") -> str:
    key = f"{org}|{_normalize_title_for_dedup(title)}".lower()
    return hashlib.md5(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Cross-variant dedup: catch the SAME role re-listed under a renamed title (a
# seniority word added/removed), a re-punctuated title, or as a same-company
# duplicate in another language. This is an ADDITIVE layer — make_vacancy_id()'s
# exact dedup_hash formula is unchanged, so every already-stored row and every
# archived tombstone keeps matching by the OLD hash (no mass resurrection of
# buried roles, no self-duplication of live rows on the next fetch). The
# normalized key and the description fingerprint are computed on the fly and
# checked ALONGSIDE the exact hash, never instead of it.
# ---------------------------------------------------------------------------

# Seniority / level qualifiers that do not change role identity. Removed as
# whole words so "Senior Product Manager" and "Product Manager" collapse to one
# role. Deliberately small — only unambiguous *modifiers*. Words that NAME a
# role rather than grade it are excluded on purpose: "staff", "head of" and
# "chief" would corrupt distinct titles — "Staff Engineer"→"Engineer", "Head of
# Product"→"Product", "Chief of Staff"→"of" — collapsing genuinely different
# roles. Trade-off: "Lead" is kept as a modifier, so "Lead Generation Manager"
# also loses "Lead"; the risk is bounded because matching is per-company and
# that exact pair coexisting is rare.
_LEVEL_WORDS = (
    "senior",
    "sr",
    "snr",
    "junior",
    "jr",
    "jnr",
    "lead",
    "principal",
)
_LEVEL_RE = re.compile(r"\b(?:" + "|".join(_LEVEL_WORDS) + r")\b")
# Any non-word, non-space char is a separator, so dash vs comma vs slash vs
# double space vs case never fork the key ("Innovation - Generative" ==
# "Innovation, Generative"). \w keeps accented letters and digits.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

#: A normalized description shorter than this is too boilerplate-prone to trust
#: as a same-company language-duplicate signal; description_fingerprint()
#: returns None below it. Set high on purpose: a real full job description is
#: long AND role-specific, so two distinct roles almost never share an identical
#: body this size — whereas a short "about us / how to apply" blurb reused
#: across several postings would. The guard trades a few missed medium-length
#: language dupes for never collapsing two genuinely different roles that happen
#: to share a boilerplate stub.
_MIN_DESC_FP_CHARS = 1000

# Statuses that carry a user decision — a renamed/language variant must inherit
# one of these rather than resurface as 'unseen'.
_DECIDED_STATUSES = frozenset(
    {"applied", "liked", "to_apply", "to_research", "to_network", "passed", "skipped"}
)

# Common title abbreviations expanded to their long form so a spelled-out role
# and an abbreviated one collapse to ONE dedup key ("Office of the CEO" ==
# "Office of the Chief Executive Officer"). Deliberately small and unambiguous:
# only acronyms whose expansion never renames a genuinely different role.
_TITLE_ABBREVIATIONS = {
    "ceo": "chief executive officer",
    "cfo": "chief financial officer",
    "coo": "chief operating officer",
    "cto": "chief technology officer",
    "cmo": "chief marketing officer",
    "cpo": "chief product officer",
    "cio": "chief information officer",
    "ciso": "chief information security officer",
    "vp": "vice president",
    "svp": "senior vice president",
    "evp": "executive vice president",
    "hr": "human resources",
    "comms": "communications",
    "ops": "operations",
    "mgr": "manager",
    "exec": "executive",
}
_ABBREV_RE = re.compile(r"\b(" + "|".join(_TITLE_ABBREVIATIONS) + r")\b")

# Parenthetical NOISE that annotates a posting's count/status, never the role
# identity: "(3 Openings)", "(closed)", "(Reopened)", "(Multiple positions)".
# Distinguishing parentheticals — "(Spanish)", "(Maternity Cover)" — are left
# intact so genuinely different roles are never merged. Bare "new"/"updated"
# are deliberately NOT noise words: "(New York)", "(New Delhi)", "(New Grad)"
# must keep their distinguishing key ("(3 new openings)" still strips via the
# count branch / "openings").
_NOISE_PAREN_RE = re.compile(
    r"\s*\((?:\s*\d[\d\s,]*\s*(?:openings?|positions?|roles?|vacancies|posts?)?\s*"
    r"|[^)]*\b(?:openings?|positions?|vacancies|closed|reopened|re-opened|filled|"
    r"on hold|urgent|multiple)\b[^)]*)\)",
    re.I,
)

# Requisition-id noise: "#12345", "#REQ-2024-01" — a '#' followed by a token
# that contains at least one digit.
_REQ_ID_RE = re.compile(r"#\s*[A-Za-z0-9-]*\d[A-Za-z0-9-]*")


def _strip_title_noise(text: str) -> str:
    """Drop count/status parentheticals and req-id tokens from a lowercased title."""
    text = _NOISE_PAREN_RE.sub(" ", text)
    text = _REQ_ID_RE.sub(" ", text)
    return text


def _normalize_title_core(title: str) -> str:
    """Shared dedup normalization, seniority KEPT.

    On top of _normalize_title_for_dedup (geo-suffix strip + whitespace
    collapse): lowercase, strip count/req-id noise, expand common abbreviations
    (CEO -> chief executive officer), fold '&'->'and', drop punctuation, and
    collapse whitespace. Seniority words survive here — _normalize_title_strong
    layers their removal on top (a level rename over time is one role)."""
    base = _normalize_title_for_dedup(title).lower()
    base = _strip_title_noise(base)
    base = _ABBREV_RE.sub(lambda m: _TITLE_ABBREVIATIONS[m.group(1)], base)
    base = base.replace("&", " and ")
    base = _PUNCT_RE.sub(" ", base).replace("_", " ")
    return re.sub(r"\s+", " ", base).strip()


# Trailing title segments (after a comma, pipe, mid-dot, or spaced dash) that
# name a place or work mode, not the role: "Head of X, London", "Y - Remote".
# Matching is vocabulary-driven — the segment must equal a KNOWN city/country
# from geo's curated maps (or a work-mode word below) — so a distinguishing
# qualifier like "Program Officer, Climate" is never stripped. Same-time pairs
# of genuinely distinct regional roles stay protected by the batch-alive and
# URL guards in _find_existing_vacancy.
_WORKMODE_SEGMENTS = frozenset(
    {"remote", "hybrid", "onsite", "on site", "global", "worldwide", "flexible"}
)
# Continents / macro-regions boards append the same way ("Director of MEAL,
# Africa"). Curated like the work-mode set: only unambiguous place-words that
# never name a portfolio. Same-time distinct regional roles ("Director, Europe"
# + "Director, Asia" both live) stay protected by the batch-alive + URL guards.
_CONTINENT_SEGMENTS = frozenset(
    {
        "africa",
        "asia",
        "americas",
        "north america",
        "south america",
        "latin america",
        "central america",
        "europe",
        "oceania",
        "middle east",
        "east africa",
        "west africa",
        "southern africa",
        "sub saharan africa",
        "sub-saharan africa",
        "south asia",
        "southeast asia",
        "central asia",
        "apac",
        "emea",
        "mena",
        "lac",
    }
)
_TITLE_SEG_SPLIT = re.compile(r"\s*(?:,|\||·|\s[–—-]\s)\s*")

_GEO_SEGMENTS: "frozenset[str] | None" = None


def _geo_segments() -> "frozenset[str]":
    """Lowercased known city/country names + work-mode words, built lazily.

    geo (via settings) is imported on first use, mirroring the lazy
    filter_vacancies import in description_fingerprint(), to keep module import
    order untangled.
    """
    global _GEO_SEGMENTS
    if _GEO_SEGMENTS is None:
        import geo

        terms = set(_WORKMODE_SEGMENTS) | set(_CONTINENT_SEGMENTS)
        for bucket in geo._CITY_MAP.values():
            terms.update(t.lower() for t in bucket)
        for bucket in geo._COUNTRY_MAP.values():
            terms.update(t.lower() for t in bucket)
        _GEO_SEGMENTS = frozenset(terms)
    return _GEO_SEGMENTS


def _strip_geo_segments(title: str) -> str:
    """Drop trailing comma/dash segments that name a known place or work mode.

    "Head of Community Engagement, London" -> "Head of Community Engagement";
    "Program Officer, Climate" is untouched ("climate" is not in the geo
    vocabulary). Never strips down to nothing.
    """
    parts = _TITLE_SEG_SPLIT.split(title)
    geo_terms = _geo_segments()
    while len(parts) > 1 and parts[-1].strip().lower() in geo_terms:
        parts.pop()
    return ", ".join(p for p in parts if p.strip())


def _singularize_token(word: str) -> str:
    """Deterministic plural fold for dedup keys ("heads" == "head").

    Cheap suffix rules, applied identically to both sides of a comparison —
    occasional over-stemming ("sales" -> "sale") is harmless because the result
    is only ever a hash key, never displayed. Words ending in -ss/-us/-is
    (business, status, analysis) are left alone.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _normalize_title_strong(title: str) -> str:
    """Aggressively normalize a title for cross-variant dedup.

    Trailing geo/work-mode segment strip, then core normalization (see
    _normalize_title_core), then removal of standalone seniority words and a
    plural fold — so "Heads of Community Engagement" and "Head of Community
    Engagement, London" produce ONE key. An empty result (the title was only
    level words) falls back to the core form so unrelated stub titles don't all
    collapse together.
    """
    base = _normalize_title_core(_strip_geo_segments(title))
    stripped = re.sub(r"\s+", " ", _LEVEL_RE.sub(" ", base)).strip()
    stripped = " ".join(_singularize_token(w) for w in stripped.split())
    return stripped or base


def make_normalized_id(org: str, title: str) -> str:
    """Cross-variant dedup key: md5(org | strongly-normalized title).

    Additive companion to make_vacancy_id(): same 16-char md5 shape and same
    org scoping (so two companies with the same role never collide in the
    global archived-hash set), but a title normalized hard enough that renamed
    or re-punctuated variants of one role produce ONE key.
    """
    key = f"{org}|{_normalize_title_strong(title)}".lower()
    return hashlib.md5(key.encode()).hexdigest()[:16]


def make_sibling_vacancy_id(canonical_hash: str, desc_fp: str) -> str:
    """Disambiguated dedup_hash for a distinct role that shares org+title with an
    already-stored role but has its OWN, different job description.

    dedup_hash is UNIQUE and make_vacancy_id() hashes only org+title, so two
    genuinely different roles with the same title cannot both hold the canonical
    hash. The first-seen role keeps that canonical hash (backward compatible); a
    later sibling whose description body differs is salted with its description
    fingerprint so both coexist as separate rows and each re-matches its OWN row
    on the next fetch (stable: same body -> same fingerprint -> same salt). Only
    reached when both rows carry a trustworthy fingerprint (a full, role-specific
    body >= _MIN_DESC_FP_CHARS) that differs — never for a short/absent body.
    """
    key = f"{canonical_hash}|{desc_fp}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def description_fingerprint(description: str | None) -> str | None:
    """Stable fingerprint of a description body, or None when too short.

    Cheap and deterministic (no LLM, no network): strip HTML, unescape
    entities, lowercase, drop punctuation, collapse whitespace, md5. Two
    same-company postings whose bodies match — e.g. an English and a French
    listing that carry the same description — share a fingerprint and dedup to
    one role. Short/boilerplate bodies return None so unrelated roles are never
    merged on a stub.

    HTML stripping reuses filter_vacancies._strip_html (it also removes the
    *contents* of <script>/<style> blocks, which a bare tag-strip would leave
    behind and skew the fingerprint). Imported lazily: filter_vacancies imports
    this module at load time, so a top-level import would be circular.
    """
    if not description:
        return None
    from filter_vacancies import _strip_html

    text = _strip_html(description).lower()
    text = _PUNCT_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < _MIN_DESC_FP_CHARS:
        return None
    return hashlib.md5(text.encode()).hexdigest()[:16]


def _index_row(
    index: dict, org: str, row_id, dedup_hash: str, title: str, description, status, locations=None
):
    """Register one row's normalized-title, description, and URL keys into `index`.

    The stored entry carries the row's own exact dedup_hash so the caller can
    tell whether that row's exact title is also live in the current fetch (the
    batch-alive guard in _find_existing_vacancy). URL entries also carry the
    row's title: a same-URL hit only merges when one normalized title CONTAINS
    the other (see _find_existing_vacancy), so orgs whose fetcher stamps one
    generic careers URL on every role never collapse together.
    """
    entry = {"id": row_id, "status": status, "dedup_hash": dedup_hash}
    index["norm"].setdefault(make_normalized_id(org, title or ""), entry)
    fp = description_fingerprint(description)
    if fp:
        index["desc"].setdefault(fp, entry)
    for loc in locations or []:
        u = (loc.get("url") or "").strip()
        if u:
            index["url"].setdefault(u, {**entry, "title": title or ""})


def _build_dedup_index(cur, org: str, company_id) -> dict:
    """Index a company's EXISTING rows by normalized title and description body.

    Returns {"norm": {hash: entry}, "desc": {fp: entry}, "url": {url: entry+title}}.
    When several rows share a key (a pre-existing duplicate), the one carrying a
    user decision wins so a later variant inherits it. The index reflects the
    pre-batch state only: rows inserted during the save loop are deliberately
    NOT added, so two variants that appear together in one fetch stay two rows
    (a same-time level pair is not a rename — see _find_existing_vacancy).
    """
    cur.execute(
        "SELECT id, dedup_hash, title, full_description, status, locations"
        " FROM vacancy WHERE company_id = %s",
        (company_id,),
    )
    rows = cur.fetchall()
    # Decided-status rows first so setdefault keeps them as the canonical target.
    rows.sort(key=lambda r: (0 if r.get("status") in _DECIDED_STATUSES else 1, str(r.get("id"))))
    index: dict = {"norm": {}, "desc": {}, "url": {}}
    for r in rows:
        _index_row(
            index,
            org,
            r["id"],
            r.get("dedup_hash") or "",
            r.get("title") or "",
            r.get("full_description"),
            r.get("status"),
            r.get("locations"),
        )
    return index


def _consume_index_entry(index: dict, row_id) -> None:
    """Drop every key pointing at row_id after it is claimed by a rename match.

    Prevents a SECOND batch variant of the same vanished title from collapsing
    onto the same existing row: e.g. an old "Program Officer" is gone and this
    fetch lists both "Senior Program Officer" and "Junior Program Officer" — the
    first claims the old row (rename); the second must fork its own row, not
    overwrite the first.
    """
    for bucket in ("norm", "desc", "url"):
        for key in [k for k, entry in index[bucket].items() if entry["id"] == row_id]:
            del index[bucket][key]


def _row_apply_urls(row) -> set:
    """Non-empty apply/job URLs recorded on an existing row's locations[]."""
    urls = set()
    for loc in row.get("locations") or []:
        u = (loc.get("url") or "").strip()
        if u:
            urls.add(u)
    return urls


def _row_already_settled(row) -> bool:
    """True when a row already carries a user decision OR a real LLM score.

    Re-importing a (company, normalized-title) that maps onto such a row must
    NOT create a re-scoreable copy (per-facet dup fix): fold onto the settled row instead of
    forking / inserting a parallel one that scoring would pay to re-evaluate.
    A negative sentinel score (awaiting scoring) does not count as settled.
    """
    if row.get("status") in _DECIDED_STATUSES:
        return True
    score = row.get("llm_score")
    return score is not None and score >= 0


def _find_existing_vacancy(
    cur,
    index: dict,
    batch_hashes: set,
    dedup_hash: str,
    norm_hash: str,
    desc_fp,
    candidate_url,
    batch_claimed: set,
    candidate_desc_len: int = 0,
    candidate_title: str = "",
):
    """Return (existing row, match_kind, insert_hash) for this signature.

    ``existing`` is the row to merge onto, or None to insert a new row.
    ``match_kind`` is "exact", "norm", "desc", "fork" or None. ``insert_hash`` is
    the dedup_hash the caller MUST use when it inserts (existing is None):
    normally the canonical ``dedup_hash``, but a body-salted hash when this is a
    distinct sibling colliding with the canonical row (match_kind == "fork").

    The exact dedup_hash is checked first, but it is NOT unconditionally
    authoritative: make_vacancy_id() hashes only org+title, so two GENUINELY
    different roles that share a title collide on it (e.g. two open reqs listed
    at once, one via the direct ATS and one via a board). The description body
    disambiguates them —

      * body guard (exact) — if the exact-hash row and the candidate both carry
        a trustworthy description fingerprint (a full, role-specific body, see
        _MIN_DESC_FP_CHARS) and the two DIFFER, they are two distinct roles. Do
        not merge onto the canonical row; look for THIS role's own salted row (a
        prior run's sibling) and, failing that, signal a fork so the caller
        inserts it under a body-salted hash (dedup_hash is UNIQUE, so the
        canonical hash is already taken by the other sibling). A shared body (a
        true two-source duplicate or a same-source re-fetch) or a short/absent
        body (no trustworthy signal) still merges onto the single canonical row.
        The apply URL is NOT used here: the board path deliberately folds one
        role's several location-specific URLs onto one row (multi-location
        posting), so a differing URL is not a distinct-role signal.

        TWO exceptions collapse instead of forking (per-facet dup fix):
          - the canonical hash was already CLAIMED earlier in THIS batch
            (``batch_claimed``): the two are per-facet variants of one posting
            listed once per country in a single fetch (an ATS can post a
            remote role 8×, one per country, each with a country-specific body).
          - the canonical row is already SETTLED (scored / decided): re-importing
            a variant of a role we already scored or acted on must fold onto it,
            never spawn a re-scoreable copy.

    The additive normalized-title / description keys model a rename OVER TIME
    (the old title vanished, a new one appeared), so they only fire when that
    story holds:

      * batch-alive guard — if the matched existing row's OWN exact title is
        also present in this fetch (its hash is in batch_hashes), both roles are
        live right now, so this is a same-time level pair, not a rename: skip.
        (A fund can list "Program Officer" and "Senior Program Officer" at once
        — losing one would drop a genuinely open role.)
      * URL guard — if both the candidate and the existing row carry non-empty
        apply URLs and none overlap, they are two distinct reqs: skip — UNLESS
        the matched row is already settled (scored/decided), in which case a
        differing URL is just a re-posted variant of a role we already scored
        and it folds rather than spawning a re-scoreable copy (per-facet dup fix).

    A claimed inexact match is consumed from the index so a second batch variant
    of the same vanished title forks its own row instead of collapsing again.
    """
    candidate_url = (candidate_url or "").strip()
    cur.execute("SELECT * FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
    row = cur.fetchone()
    if row is not None:
        row_fp = description_fingerprint(row.get("full_description"))
        row_desc_len = len(row.get("full_description") or "")
        # Scrape-depth guard: two sources scraping the SAME posting can ship
        # wildly different bodies (a full JD vs a board's summary stub), and
        # their fingerprints then differ for reasons that say nothing about
        # role identity. When one body is under a third of the other's length
        # the fp comparison is not trustworthy — fold onto the canonical row
        # instead of forking a sibling. Two genuinely distinct roles both carry
        # full, comparably-sized JDs.
        comparable_bodies = (
            not candidate_desc_len
            or not row_desc_len
            or min(candidate_desc_len, row_desc_len) * 3 >= max(candidate_desc_len, row_desc_len)
        )
        if desc_fp and row_fp and desc_fp != row_fp and comparable_bodies:
            # Same org+title, DIFFERENT full description. Normally two distinct
            # roles colliding on the canonical hash → fork. But fold instead when
            # this is a per-facet variant of a posting already claimed in THIS
            # batch, or the canonical row is already settled (scored/decided):
            # both cases must not spawn a re-scoreable parallel row (per-facet dup fix).
            if dedup_hash in batch_claimed or _row_already_settled(row):
                return row, "exact", dedup_hash
            # Re-match this role's own salted row, else fork so the live sibling
            # is not merged away.
            salted = make_sibling_vacancy_id(dedup_hash, desc_fp)
            cur.execute("SELECT * FROM vacancy WHERE dedup_hash = %s", (salted,))
            sibling = cur.fetchone()
            if sibling is not None:
                return sibling, "exact", salted
            return None, "fork", salted
        return row, "exact", dedup_hash
    # Same-company, same apply URL: an apply URL identifies one requisition, so
    # a retitled re-listing of it ("Director of MEAL" -> "Director of MEAL,
    # Africa"; "Program Manager, X" -> "Program Manager, X - Deal Operations")
    # is the SAME role even when the normalized titles no longer match. Two
    # guards keep this from over-merging: one normalized title must CONTAIN the
    # other (orgs whose fetcher stamps one generic careers URL on every role
    # never pass this), and the batch-alive rule below still applies (both
    # spellings live in ONE fetch stay two rows — e.g. a level pair sharing a
    # landing URL).
    if candidate_url and candidate_title:
        hit = index["url"].get(candidate_url)
        if hit is not None and hit["dedup_hash"] not in batch_hashes:
            a = _normalize_title_strong(candidate_title)
            b = _normalize_title_strong(hit.get("title", ""))
            if a and b and (a in b or b in a):
                cur.execute("SELECT * FROM vacancy WHERE id = %s", (hit["id"],))
                cand = cur.fetchone()
                if cand is not None:
                    _consume_index_entry(index, hit["id"])
                    return cand, "norm", dedup_hash

    for kind, key in (("norm", norm_hash), ("desc", desc_fp)):
        if not key:
            continue
        hit = index[kind].get(key)
        if hit is None:
            continue
        # Both live in the current fetch → a same-time pair, not a rename.
        if hit["dedup_hash"] in batch_hashes:
            continue
        cur.execute("SELECT * FROM vacancy WHERE id = %s", (hit["id"],))
        cand = cur.fetchone()
        if cand is None:
            continue
        existing_urls = _row_apply_urls(cand)
        if candidate_url and existing_urls and candidate_url not in existing_urls:
            # Two distinct reqs — do not merge — unless the matched row is
            # already scored/decided: then this is a re-titled variant of a role
            # we already settled and folding it prevents a re-scoreable copy.
            if not _row_already_settled(cand):
                continue
        _consume_index_entry(index, hit["id"])
        return cand, kind, dedup_hash
    return None, None, dedup_hash


def _make_location_entry(job: dict) -> dict:
    """Build a v2 location entry from a fetcher job dict."""
    loc_str = job.get("location", "")
    parsed = parse_location(loc_str)
    return {
        "work_mode": parsed["work_mode"],
        "region": parsed["region"],
        "country": parsed["country"],
        "city": parsed["city"],
        "compensation": job.get("compensation", "") or None,
        "url": job.get("url", "") or None,
    }


def _parse_comp_value(comp_str: str) -> float:
    if not comp_str:
        return 0.0
    nums = re.findall(r"[\d,]+", comp_str)
    if not nums:
        return 0.0
    val = max(float(n.replace(",", "")) for n in nums)
    if "RSD" in comp_str:
        val = val / 117
    elif "GEL" in comp_str:
        val = val / 2.9
    elif "$" in comp_str:
        val = val * 0.92
    elif "\u00a3" in comp_str:
        val = val * 1.17
    return val


# ---------------------------------------------------------------------------
# Company resolution
# ---------------------------------------------------------------------------


def resolve_company_id(org_name: str):
    """Resolve org name to company UUID.

    Priority: config.py alias → canonical_name lookup → DB GIN aliases → None.
    """
    canonical = resolve_canonical_name(org_name)
    conn = get_conn()
    cur = conn.cursor()

    # 1. Exact canonical_name match
    cur.execute("SELECT id FROM company WHERE canonical_name = %s", (canonical,))
    row = cur.fetchone()
    if row:
        cur.close()
        return row[0]

    # 2. GIN alias search (case-insensitive via lower)
    cur.execute(
        "SELECT id FROM company WHERE aliases @> ARRAY[%s]::text[]",
        (org_name,),
    )
    row = cur.fetchone()
    if row:
        cur.close()
        return row[0]

    cur.close()
    return None


def _auto_discovery_status() -> str:
    """Status assigned to a brand-new, auto-discovered company.

    Same rule on BOTH backends (STRATEGY guardrail 2: product behaviour never
    branches on IS_SQLITE) — the board/ATS auto-discovery path in
    ``save_vacancies`` / ``save_board_vacancies`` calls this, including for a
    name already known to the static registry: nothing skips the review gate
    just because the name looks familiar.

    Configured by ``config/defaults.toml`` ``[thresholds] auto_discovery_status``
    (env ``AUTO_DISCOVERY_STATUS`` overrides). The default "candidate" sends
    every auto-discovered company through the ``company_scoring`` driver stage
    (drop junk → find a site → collect evidence → WANT-score) before it can
    activate — activation needs either an explicit approve in ``/jobs-review``
    or, if opted in, ``auto_review_candidates()`` crossing the approve
    threshold (STRATEGY guardrail 8: nothing activates without an explicit
    yes). Only "active" is honoured as an explicit opt-out; any other value
    (including a typo) falls back to the safe default.
    """
    import os

    raw = os.environ.get("AUTO_DISCOVERY_STATUS")
    if raw is None:
        raw = settings.thresholds()["auto_discovery_status"]
    return "active" if str(raw).strip().lower() == "active" else "candidate"


_STATUS_MERGE_RANK = {"active": 0, "inactive": 1, "candidate": 2}


def _find_mergeable_company(cur, org_name: str):
    """Find an existing company an incoming board name is a VARIANT of.

    Exact canonical_name / alias lookups (``resolve_company_id``) already ran
    and missed; this is the tolerance pass that catches board name variants
    ("EBRD - European Bank for Reconstruction and Development" for an existing
    "EBRD") so they MERGE into the tracked row instead of forking a duplicate
    candidate that gets re-enriched and re-WANT-scored. Matches against BOTH
    canonical_name and every alias, across ANY status; when several rows match,
    the most-established status wins (active > inactive > candidate) so a
    duplicate can never shadow the already-scored row.

    Returns ``(id, canonical_name, aliases_list)`` or ``None``.
    """
    cur.execute("SELECT id, canonical_name, aliases, status FROM company")
    best = None  # (rank, id, canonical, aliases)
    for cid, canonical, aliases, status in cur.fetchall():
        names = [canonical, *(aliases or [])]
        if any(company_name_variants_match(org_name, n) for n in names):
            rank = _STATUS_MERGE_RANK.get(status, 3)
            if best is None or rank < best[0]:
                best = (rank, cid, canonical, list(aliases or []))
    if best is None:
        return None
    return best[1], best[2], best[3]


def ensure_company(org_name: str, status: str = "candidate"):
    """Find or create a company. Returns UUID.

    ``status`` is honoured verbatim — callers that want a candidate get one.
    The board/ATS auto-discovery path passes ``_auto_discovery_status()`` so
    the same, configurable status lands on both backends (see save_vacancies).

    Before inserting, an incoming board name that is a VARIANT of a company we
    already track (fuzzy match on canonical_name + aliases, any status) is
    MERGED into that row — the variant is folded into its ``aliases`` and its
    id returned — instead of forking a new candidate. The existing row keeps
    its status and WANT score, so a duplicate never reaches the enrichment or
    scoring gates (board-variant merge).
    """
    cid = resolve_company_id(org_name)
    if cid is not None:
        return cid

    conn = get_conn()
    cur = conn.cursor()

    match = _find_mergeable_company(cur, org_name)
    if match is not None:
        existing_id, canonical, aliases = match
        variant = resolve_canonical_name(org_name)
        known = {canonical.lower(), *(a.lower() for a in aliases)}
        if variant.lower() not in known:
            cur.execute(
                "UPDATE company SET aliases = %s WHERE id = %s",
                (aliases + [variant], existing_id),
            )
        cur.close()
        return existing_id

    canonical = resolve_canonical_name(org_name)
    cur.execute(
        """INSERT INTO company (canonical_name, status, aliases)
           VALUES (%s, %s, ARRAY[%s]::text[])
           ON CONFLICT (canonical_name) DO UPDATE SET canonical_name = EXCLUDED.canonical_name
           RETURNING id""",
        (canonical, status, canonical),
    )
    cid = cur.fetchone()[0]
    cur.close()
    return cid


# ---------------------------------------------------------------------------
# Vacancy CRUD
# ---------------------------------------------------------------------------

# Vacancy columns loaded when light=True. Intentionally excludes full_description
# (~8 KB p90) to cut Supabase egress for callers that only need metadata.
_VACANCY_LIGHT_COLUMNS = (
    "id",
    "created_at",
    "updated_at",
    "dedup_hash",
    "company_id",
    "title",
    "snippet",
    "compensation",
    "deadline",
    "locations",
    "department",
    "llm_score",
    "llm_summary",
    "llm_reasoning",
    "llm_hard_requirements",
    "llm_scored_at",
    "us_eligibility",
    "status",
    "status_updated_at",
    "first_seen",
    "last_seen",
    "triage",
)

# ``scored_by`` (migration 0009) is deliberately NOT in the tuple above: unlike
# the baseline-folded columns it's migration-only (see sql/migrations/README.md
# — new migrations must be self-sufficient, not rely on the frozen baseline
# already carrying the column). An install that hasn't run migrate.py yet must
# not crash on every score write/read, so its presence is detected once per
# process and the column is included only when it actually exists — mirrors
# learning.table_ready() for the learning_log table.
_scored_by_supported_cache: bool | None = None


def _scored_by_supported() -> bool:
    """True once ``vacancy.scored_by`` exists (migration 0009 has run).

    False before that — callers degrade to skipping provenance rather than
    raising "no such column" / "column does not exist".
    """
    global _scored_by_supported_cache
    if _scored_by_supported_cache is not None:
        return _scored_by_supported_cache
    from db_backend import IS_SQLITE

    conn = get_conn()
    cur = conn.cursor()
    try:
        if IS_SQLITE:
            cur.execute("PRAGMA table_info(vacancy)")
            cols = {row[1] for row in cur.fetchall()}
        else:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'vacancy'"
            )
            cols = {row[0] for row in cur.fetchall()}
        _scored_by_supported_cache = "scored_by" in cols
    except Exception:
        _scored_by_supported_cache = False
    finally:
        cur.close()
    return _scored_by_supported_cache


def _table_has_column(table: str, col: str) -> bool:
    """True when ``table`` has ``col`` on the active backend.

    Lets a write degrade gracefully on a pre-migration schema (a fresh
    simple-mode SQLite DB is baseline-only until ``migrate.py`` runs) instead of
    raising "no such column". Same pattern as the migration-0013 source_board
    gate; also used for the 0015 fetch-health telemetry columns."""
    from db_backend import IS_SQLITE

    conn = get_conn()
    cur = conn.cursor()
    try:
        if IS_SQLITE:
            cur.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in cur.fetchall()}
        else:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            )
            cols = {row[0] for row in cur.fetchall()}
        return col in cols
    except Exception:
        return False
    finally:
        cur.close()


def _vacancy_has_column(col: str) -> bool:
    """True when ``vacancy`` has ``col`` — thin wrapper over _table_has_column
    kept for the existing source_board (migration 0013) call site."""
    return _table_has_column("vacancy", col)


def load_vacancies(
    *,
    company_name=None,
    status=None,
    status_exclude=None,
    unscored_only=False,
    limit=None,
    include_inactive_companies=False,
    include_candidate_companies=False,
    score_floor_any_company=None,
    light: bool = False,
) -> dict[str, dict]:
    """Load vacancies from Supabase. Returns {uuid_str: vacancy_dict}.

    By default shows only vacancies from active (approved) companies.
    include_inactive_companies=True → no company status filter (all statuses).
    include_candidate_companies=True → also include candidate companies.
    score_floor_any_company=N → a role scoring > N is shown no matter its
    company's status (active/candidate/inactive), so a strong match never hides
    just because its company isn't approved yet. Only widens the set; it never
    hides a role the company-status filter would have shown.
    status_exclude=[...] → filter out vacancies whose status is in the list
    (e.g. ['passed', 'skipped'] for /score to skip already-decided rows).
    light=True → drop full_description from the SELECT (saves ~8 KB/row).
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    conditions = []
    params = []

    if not include_inactive_companies:
        if include_candidate_companies:
            company_cond = "c.status != 'inactive'"
        else:
            company_cond = "c.status = 'active'"
        if score_floor_any_company is not None:
            conditions.append(f"({company_cond} OR v.llm_score > %s)")
            params.append(score_floor_any_company)
        else:
            conditions.append(company_cond)

    if company_name:
        canonical = resolve_canonical_name(company_name)
        conditions.append("c.canonical_name = %s")
        params.append(canonical)

    if status:
        conditions.append("v.status = %s")
        params.append(status)

    if status_exclude:
        placeholders = ", ".join(["%s"] * len(status_exclude))
        conditions.append(f"v.status NOT IN ({placeholders})")
        params.extend(status_exclude)

    if unscored_only:
        # "Unscored" must mean the same thing here as in the count the dashboard
        # shows (report.data_prep._count_unscored) and the in-memory re-score
        # gate (score_vacancies.py) — all treat a negative sentinel score as
        # awaiting scoring. Loading only IS NULL stranded a -1 row: counted as
        # awaiting scoring but never offered to the scorer.
        conditions.append("(v.llm_score IS NULL OR v.llm_score < 0)")
        conditions.append("v.status != 'archived'")

    where = " AND ".join(conditions) if conditions else "TRUE"

    if light:
        light_cols = _VACANCY_LIGHT_COLUMNS + (("scored_by",) if _scored_by_supported() else ())
        vacancy_cols = ", ".join(f"v.{c}" for c in light_cols)
    else:
        vacancy_cols = "v.*"

    query = f"""
        SELECT {vacancy_cols}, c.canonical_name AS org, c.careers_url AS org_url,
               c.tier AS company_tier
        FROM vacancy v
        JOIN company c ON v.company_id = c.id
        WHERE {where}
        ORDER BY v.created_at DESC
    """
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    result = {}
    for row in rows:
        vac = _row_to_vacancy(row)
        result[vac["id"]] = vac
    return result


def _row_to_vacancy(row) -> dict:
    """Convert a RealDictCursor vacancy row (joined with company) into the
    plain dict consumers expect: UUIDs as strings, tier normalized, dates ISO."""
    uid = str(row["id"])
    vac = dict(row)
    vac["id"] = uid
    vac["dedup_hash"] = row.get("dedup_hash", "")
    vac["company_id"] = str(row["company_id"])
    # Normalize tier key: SQL alias is company_tier, consumers expect "tier"
    vac["tier"] = vac.get("company_tier")
    # Ensure locations is a list (JSONB comes back as list already)
    if vac.get("locations") is None:
        vac["locations"] = []
    # Convert dates to ISO strings for compatibility
    for df in ("first_seen", "last_seen", "deadline"):
        if isinstance(vac.get(df), date):
            vac[df] = vac[df].isoformat()
    for df in ("status_updated_at", "created_at", "updated_at", "llm_scored_at"):
        if isinstance(vac.get(df), datetime):
            vac[df] = vac[df].isoformat()
    return vac


# Default cap on candidate-company vacancies pulled into a single scoring run —
# keeps subagent/LLM budget bounded while still rescuing strong roles from
# companies that have not been reviewed yet.
CANDIDATE_SCORE_LIMIT = 15
# A candidate company's vacancy is worth scoring only if the company shows
# some promise: alignment ≥ this floor, or not yet enriched (NULL).
CANDIDATE_ALIGNMENT_FLOOR = 30


def load_candidate_vacancies_for_scoring(
    *,
    limit: int = CANDIDATE_SCORE_LIMIT,
    status_exclude=None,
) -> dict[str, dict]:
    """Load unscored vacancies from *candidate* companies that look promising.

    Rescues the "strong vacancy at a forgotten company" case: a candidate
    company nobody reviewed would otherwise be invisible to scoring
    (load_vacancies filters to active companies). We let in candidate companies
    whose alignment_score is >= CANDIDATE_ALIGNMENT_FLOOR or NULL (not yet
    enriched), newest first, capped at `limit` to bound LLM spend.

    Returns {uuid_str: vacancy_dict} in the same shape as load_vacancies().
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    conditions = [
        "c.status = 'candidate'",
        # Same "unscored" definition as the main gate: a negative sentinel score
        # is awaiting scoring, so the candidate rescue must offer it too, not
        # strand it.
        "(v.llm_score IS NULL OR v.llm_score < 0)",
        "v.status != 'archived'",
        "(c.alignment_score >= %s OR c.alignment_score IS NULL)",
    ]
    params: list = [CANDIDATE_ALIGNMENT_FLOOR]

    if status_exclude:
        placeholders = ", ".join(["%s"] * len(status_exclude))
        conditions.append(f"v.status NOT IN ({placeholders})")
        params.extend(status_exclude)

    where = " AND ".join(conditions)
    query = f"""
        SELECT v.*, c.canonical_name AS org, c.careers_url AS org_url,
               c.tier AS company_tier
        FROM vacancy v
        JOIN company c ON v.company_id = c.id
        WHERE {where}
        ORDER BY v.created_at DESC
        LIMIT %s
    """
    params.append(limit)
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    result = {}
    for row in rows:
        vac = _row_to_vacancy(row)
        result[vac["id"]] = vac
    return result


def upsert_vacancy(dedup_hash: str, data: dict):
    """Insert or update vacancy by dedup_hash. Returns UUID string."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
    existing = cur.fetchone()

    if existing:
        uuid_val = existing[0]
        fields_to_update = {
            k: v for k, v in data.items() if k not in ("id", "dedup_hash", "created_at")
        }
        if fields_to_update:
            set_clauses = []
            vals = []
            for k, v in fields_to_update.items():
                set_clauses.append(f"{k} = %s")
                vals.append(
                    Json(v) if isinstance(v, (dict, list)) and k in ("locations", "triage") else v
                )
            vals.append(uuid_val)
            cur.execute(
                f"UPDATE vacancy SET {', '.join(set_clauses)} WHERE id = %s",
                vals,
            )
        cur.close()
        return str(uuid_val)
    else:
        # Insert
        cols = ["dedup_hash"] + [k for k in data if k not in ("id", "dedup_hash")]
        vals = [dedup_hash] + [
            Json(data[k])
            if isinstance(data[k], (dict, list)) and k in ("locations", "triage")
            else data[k]
            for k in data
            if k not in ("id", "dedup_hash")
        ]
        placeholders = ", ".join(["%s"] * len(cols))
        cur.execute(
            f"INSERT INTO vacancy ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
            vals,
        )
        uuid_val = cur.fetchone()[0]
        cur.close()
        return str(uuid_val)


def update_vacancy_fields(vacancy_uuid: str, **fields):
    """Update specific fields on a vacancy by UUID."""
    if not fields:
        return
    conn = get_conn()
    cur = conn.cursor()
    set_clauses = []
    vals = []
    for k, v in fields.items():
        set_clauses.append(f"{k} = %s")
        if isinstance(v, (dict, list)) and k in ("locations", "triage"):
            vals.append(Json(v))
        else:
            vals.append(v)
    vals.append(vacancy_uuid)
    cur.execute(
        f"UPDATE vacancy SET {', '.join(set_clauses)} WHERE id = %s",
        vals,
    )
    cur.close()


def delete_vacancies(vacancy_uuids: list[str]) -> int:
    """Delete vacancies by UUID list. Returns count deleted."""
    if not vacancy_uuids:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM vacancy WHERE id = ANY(%s::uuid[])", (vacancy_uuids,))
    count = cur.rowcount
    cur.close()
    return count


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------


def _safe_deadline(raw: str | None) -> str | None:
    """Parse free-text deadline into ISO date, return None on failure."""
    if not raw:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        # An ISO-SHAPED string can still be a calendar-invalid date
        # ("2026-02-30", month 13, day 00). SQLite would store it verbatim as
        # text, but the canonical Postgres DATE column rejects it and aborts the
        # run — so validate the calendar here, backend-agnostically (no
        # IS_SQLITE branch), and drop an impossible date rather than persist it.
        try:
            date.fromisoformat(raw)
        except ValueError:
            print(f"  dropped calendar-invalid deadline: {raw!r}", flush=True)
            return None
        return raw
    try:
        return dateutil_parser.parse(raw, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


_DEADLINE_RE = re.compile(
    r"(?:[Dd]eadline|[Cc]losing\s+date|[Aa]pply\s+by|[Aa]pplications?\s+close)"
    r"[:\s]+"
    r"(\d{4}-\d{2}-\d{2}|[A-Za-z0-9,\s]+\d{4})"
)


def _extract_deadline_from_description(html: str) -> str:
    """Extract application deadline from HTML description text.

    Self-contained: strips HTML tags inline (no import from fetchers).
    Returns raw date string for _safe_deadline() to parse, or empty string.
    """
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()
    m = _DEADLINE_RE.search(text)
    return m.group(1).strip() if m else ""


def _strip_nul_bytes(job: dict) -> None:
    """Postgres TEXT can't contain 0x00. Strip from all string values in place."""
    for k, v in list(job.items()):
        if isinstance(v, str) and "\x00" in v:
            job[k] = v.replace("\x00", "")


# ---------------------------------------------------------------------------
# Shared core for the two save paths (save_vacancies / save_board_vacancies).
# The per-job quality gate, the location key, the new-row deadline resolution
# and the merge summary are identical across both; the divergent bits (the
# existing-row update policy and the INSERT column set) stay inline in each.
# ---------------------------------------------------------------------------


def _gate_job(job: dict) -> tuple[str, str | None, bool]:
    """Run the shared pre-insert quality gate on a fetched job, in place.

    Returns ``(title, skip_reason, boilerplate_gated)``:
      * ``title`` — the sanitized title.
      * ``skip_reason`` — ``None`` keeps the job; ``"junk"`` drops it AND counts
        as skipped_junk; ``"empty_title"`` drops a null/blank-title row (logged
        by the caller); ``"blacklist"`` / ``"thin"`` drop it silently.
      * ``boilerplate_gated`` — True when _gate_description blanked a cookie-wall
        description (count skipped_boilerplate; the row is still kept).

    Same order both save paths ran inline: strip NULs → _gate_description →
    sanitize title → title blacklist → has_enough_content → is_content_junk.
    """
    _strip_nul_bytes(job)
    boilerplate_gated = bool(_gate_description(job))
    title = _sanitize_title(job.get("title", ""))
    # A row with no usable title (null/blank from the source) is junk: skip it
    # rather than insert a blank-title vacancy — and, crucially, never let it
    # abort the org's whole save. Checked first so no downstream helper runs on
    # an empty title.
    if not title.strip():
        return title, "empty_title", boilerplate_gated
    if filters.title_words_blacklisted(title):
        return title, "blacklist", boilerplate_gated
    if not filters.has_enough_content(job):
        return title, "thin", boilerplate_gated
    if filters.is_content_junk(job.get("full_description", "")):
        return title, "junk", boilerplate_gated
    return title, None, boilerplate_gated


def _refresh_gated_last_seen(cur, org: str, title: str, today: str) -> bool:
    """Refresh last_seen on an ALREADY-KNOWN role that the source still lists but
    the import gate drops (a junk/blacklisted/thin title — e.g. a "talent pool"
    posting the user already liked).

    The quality gate exists to stop us IMPORTING such a posting as a NEW row, not
    to declare an existing one closed. When a gated job's exact org+title still
    matches a stored row, the source is plainly still listing that role, so bump
    its last_seen — otherwise a role we keep filtering out drifts stale and shows
    as "expired" in triage while it is openly live at source. Deliberately narrow:
    matches only the exact dedup_hash and never resurrects, re-imports or
    rescores. A title we don't already track updates zero rows (the gate still
    blocks a brand-new junk import); an archived tombstone is left untouched.
    Returns True when a row was refreshed.
    """
    if not title.strip():
        return False
    cur.execute(
        "UPDATE vacancy SET last_seen = %s WHERE dedup_hash = %s AND status != 'archived'",
        (today, make_vacancy_id(org, title)),
    )
    return cur.rowcount > 0


def refresh_unchanged_company_last_seen(org_name: str, today: str | None = None) -> int:
    """Bump last_seen on a firecrawl-scraped company's own live rows when its
    careers page is byte-identical to the last scrape (Firecrawl change-tracking
    ``changeStatus == "same"``).

    An unchanged page means every role it listed last time is STILL listed, yet
    change-tracking hands the scraper nothing to diff, so it returns an empty
    ``UnchangedListing`` and ``save_vacancies`` touches no row. Left alone the
    company's whole roster freezes and falsely ages into Triage's "Expired"
    column (derived from ``last_seen >= STALE_SOURCE_DAYS``). This is the
    company-level analogue of ``_refresh_gated_last_seen``: a narrow "still
    listed at source" touch that never resurrects a tombstone, never rescores and
    never inserts.

    Provenance: the vacancy table has no per-row source column, so ``company_id``
    is the only provenance signal available — this refreshes every non-archived
    row of the firecrawl company, exactly the set a changed-page scrape would
    have re-touched via ``save_vacancies``. (A board-sourced row for the SAME
    tracked employer is indistinguishable and thus also bumped; such a row is
    independently refreshed by its own board run, so the touch is at worst
    redundant and never resurrects — see the PR body.)

    Does NOT commit — mirrors the other DAL writes; the fetch driver commits
    per-company. Returns the number of rows refreshed.
    """
    company_id = resolve_company_id(org_name)
    if company_id is None:
        return 0
    if today is None:
        today = datetime.now(DASHBOARD_TZ).date().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE vacancy SET last_seen = %s WHERE company_id = %s AND status != 'archived'",
        (today, company_id),
    )
    refreshed = cur.rowcount
    cur.close()
    return refreshed


def _loc_key(loc: dict) -> str:
    """Stable key for a location entry: city, else country, else work_mode."""
    return loc.get("city") or loc.get("country") or loc.get("work_mode") or ""


def _resolve_new_deadline(job: dict) -> str | None:
    """Deadline for a brand-new row: fetcher-provided, else a regex fallback from
    the description. Returns a parsed date string or None."""
    deadline_raw = job.get("deadline") or ""
    if not deadline_raw:
        deadline_raw = _extract_deadline_from_description(job.get("full_description") or "")
    return _safe_deadline(deadline_raw) if deadline_raw else None


def _print_merge_summary(
    label: str,
    *,
    skipped_archived: int,
    skipped_boilerplate: int,
    skipped_junk: int,
    resurrected: int,
    refreshed_gated: int = 0,
) -> None:
    """Print the per-source merge counters shared by both save paths."""
    if skipped_archived:
        print(f"  [{label}] skipped {skipped_archived} recently archived", flush=True)
    if skipped_boilerplate:
        print(
            f"  [{label}] gate dropped {skipped_boilerplate} boilerplate descriptions",
            flush=True,
        )
    if skipped_junk:
        print(f"  [{label}] skipped {skipped_junk} junk content", flush=True)
    if resurrected:
        print(f"  [{label}] resurrected: {resurrected}", flush=True)
    if refreshed_gated:
        print(
            f"  [{label}] refreshed last_seen on {refreshed_gated} still-listed gated role(s)",
            flush=True,
        )


def save_vacancies(org_name: str, tier, jobs: list[dict]) -> int:
    """Save fetched jobs into the DB. Returns count of new vacancies.

    Same role (org + title) at different locations → one entry with locations[].
    """
    org_name = resolve_canonical_name(org_name)
    company_id = resolve_company_id(org_name)
    if company_id is None:
        company_id = ensure_company(org_name, status=_auto_discovery_status())
    today = datetime.now(DASHBOARD_TZ).date().isoformat()
    new_count = 0
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Direct ATS path: exclude ONLY 'gone_from_source' tombstones so the
    # company's own re-listing resurrects a role the source had merely dropped.
    # Every OTHER tombstone reason — crucially 'score_below_threshold' — STAYS in
    # the set, so a role we buried for a low score is NOT re-imported / re-scored /
    # re-archived each run when the ATS still lists it. Loaded
    # once (not per row).
    archived_hashes = get_archived_hashes(include_gone=False)
    # Resurrects must clear a machine-archival reason (board_stale /
    # board_disabled) so it never sits on a live row. Guarded once (0014).
    has_status_reason = _vacancy_has_column("status_reason")
    # Index this company's existing rows once so a renamed / re-punctuated /
    # language variant merges onto the live row instead of forking a new one.
    dedup_index = _build_dedup_index(cur, org_name, company_id)
    # Exact hashes of every title in THIS fetch. If an existing row's own exact
    # title is in here it is still live, so a variant of it is a same-time pair
    # (keep both), not a rename (see _find_existing_vacancy's batch-alive guard).
    batch_hashes = {make_vacancy_id(org_name, _sanitize_title(j.get("title", ""))) for j in jobs}
    # Canonical dedup_hashes this batch has already inserted/merged onto — a
    # later same-title job with a per-facet body folds onto that row instead of
    # forking a parallel per-country copy (per-facet dup fix). Populated AFTER each job so a
    # role's FIRST appearance still uses the normal distinct-sibling fork.
    batch_claimed: set = set()

    skipped_archived = 0
    skipped_junk = 0
    skipped_boilerplate = 0
    resurrected = 0
    refreshed_gated = 0

    for job in jobs:
        title, skip_reason, boilerplate_gated = _gate_job(job)
        if boilerplate_gated:
            skipped_boilerplate += 1
        if skip_reason == "junk":
            skipped_junk += 1
        elif skip_reason == "empty_title":
            print(f"  [{org_name}] skipped a fetched row with a missing/empty title", flush=True)
        if skip_reason is not None:
            # A gated title we ALREADY track is still being listed by the source:
            # refresh its last_seen so it isn't shown stale/expired while live.
            if skip_reason != "empty_title" and _refresh_gated_last_seen(
                cur, org_name, title, today
            ):
                refreshed_gated += 1
            continue

        dedup_hash = make_vacancy_id(org_name, title)
        norm_hash = make_normalized_id(org_name, title)
        desc_fp = description_fingerprint(job.get("full_description"))

        if filters.is_recently_archived(
            archived_hashes, dedup_hash
        ) or filters.is_recently_archived(archived_hashes, norm_hash):
            skipped_archived += 1
            continue

        loc_entry = _make_location_entry(job)
        loc_key = _loc_key(loc_entry)

        # Check existing: exact hash first, then a same-company renamed/language variant.
        existing, match_kind, insert_hash = _find_existing_vacancy(
            cur,
            dedup_index,
            batch_hashes,
            dedup_hash,
            norm_hash,
            desc_fp,
            job.get("url"),
            batch_claimed,
            candidate_desc_len=len(job.get("full_description") or ""),
            candidate_title=title,
        )
        batch_claimed.add(dedup_hash)

        if existing:
            updates = {"last_seen": today}
            is_rename = match_kind in ("norm", "desc")
            decided = existing.get("status") in _DECIDED_STATUSES

            # A re-listed role is alive again: resurrect a row we had archived
            # (gone from source) OR protected as 'expiring' back to 'unseen'.
            if existing.get("status") in ("archived", "expiring"):
                updates["status"] = "unseen"
                # Reset the expiring alert so a future expiry re-alerts.
                updates["expiring_alerted_at"] = None
                if has_status_reason:
                    # Re-listed = alive: a stale archival reason must not
                    # linger on the now-live row.
                    updates["status_reason"] = None
                resurrected += 1

            # A true rename over time: point the surviving row at the new title
            # (and its hash) so a later exact fetch matches directly and
            # archive_gone keeps it live. Status is inherited (same row).
            if is_rename:
                updates["title"] = title
                updates["dedup_hash"] = dedup_hash

            if job.get("snippet") and not existing.get("snippet"):
                updates["snippet"] = job["snippet"]
            new_desc = job.get("full_description") or ""
            old_desc = existing.get("full_description") or ""
            # Never overwrite a DECIDED row's description on an inexact match: the
            # rename/language match could be a false positive and we'd corrupt a
            # role the user already acted on.
            if new_desc and len(new_desc) > len(old_desc) + 100 and not (is_rename and decided):
                updates["full_description"] = new_desc
            if job.get("deadline") and not existing.get("deadline"):
                parsed_dl = _safe_deadline(job["deadline"])
                if parsed_dl:
                    updates["deadline"] = parsed_dl
            # Fallback: extract deadline from description if still missing
            if not existing.get("deadline") and "deadline" not in updates:
                extracted = _extract_deadline_from_description(job.get("full_description") or "")
                if extracted:
                    parsed_dl = _safe_deadline(extracted)
                    if parsed_dl:
                        updates["deadline"] = parsed_dl
            if job.get("department") and not existing.get("department"):
                updates["department"] = job["department"]

            # Merge locations
            locs = existing.get("locations") or []
            existing_loc_keys = {_loc_key(l) for l in locs}
            if loc_key not in existing_loc_keys:
                locs.append(loc_entry)
                updates["locations"] = Json(locs)
            elif loc_entry.get("url"):
                for loc in locs:
                    lk = loc.get("city") or loc.get("country") or loc.get("work_mode") or ""
                    if lk == loc_key:
                        loc["url"] = loc_entry["url"]
                        break
                updates["locations"] = Json(locs)

            set_parts = [f"{k} = %s" for k in updates]
            vals = list(updates.values()) + [existing["id"]]
            cur.execute(f"UPDATE vacancy SET {', '.join(set_parts)} WHERE id = %s", vals)
        else:
            parsed_deadline = _resolve_new_deadline(job)
            cur.execute(
                """INSERT INTO vacancy (
                       dedup_hash, company_id, title, snippet,
                       full_description, compensation, deadline,
                       first_seen, last_seen, locations, department
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    insert_hash,
                    company_id,
                    title,
                    job.get("snippet", ""),
                    job.get("full_description", ""),
                    job.get("compensation", ""),
                    parsed_deadline,
                    today,
                    today,
                    Json([loc_entry]),
                    job.get("department"),
                ),
            )
            new_count += 1

    cur.close()
    _print_merge_summary(
        org_name,
        skipped_archived=skipped_archived,
        skipped_boilerplate=skipped_boilerplate,
        skipped_junk=skipped_junk,
        resurrected=resurrected,
        refreshed_gated=refreshed_gated,
    )
    return new_count


def save_board_vacancies(board_cfg: dict, jobs: list[dict]) -> int:
    """Save job board results into the DB. Returns count of new vacancies.

    Unknown orgs → ensure_company(status=_auto_discovery_status()), "candidate"
    by default (see that function). Skips inactive companies.
    """
    today = datetime.now(DASHBOARD_TZ).date().isoformat()
    tier = board_cfg.get("tier", "C")
    board_url = board_cfg["url"]
    board_name = board_cfg.get("name", "")
    new_count = 0
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # Board provenance: stamp which board a vacancy came from (board-yield analytics).
    # Only the direct-ATS path (save_vacancies) leaves it empty. Guarded once so
    # a pre-migration install (no source_board column) still saves, unstamped.
    write_source_board = bool(board_name) and _vacancy_has_column("source_board")
    # Resurrects must clear a machine-archival reason (board_stale /
    # board_disabled) so it never sits on a live row. Guarded once (0014).
    has_status_reason = _vacancy_has_column("status_reason")

    # Board path: full archived set (include_gone=True) so a lagging feed cannot
    # resurrect a posting the source already closed. Loaded once (not per row).
    archived_hashes = get_archived_hashes(include_gone=True)
    # Per-company dedup index, built lazily (a board batch spans many orgs) so a
    # renamed / re-punctuated / language variant merges onto the live row.
    dedup_index_cache: dict = {}
    # Exact hashes present in THIS fetch, per canonical org — the batch-alive
    # guard: a variant of a title that is itself live in the fetch is a
    # same-time pair (keep both), not a rename.
    batch_hashes_by_org: dict[str, set] = {}
    for j in jobs:
        o = resolve_canonical_name(j.get("org_override") or board_cfg["name"])
        batch_hashes_by_org.setdefault(o, set()).add(
            make_vacancy_id(o, _sanitize_title(j.get("title", "")))
        )
    # Canonical dedup_hashes already inserted/merged in THIS board fetch — a
    # later same-title job with a per-facet body folds rather than forking a
    # per-country copy (per-facet dup fix). Populated after each job (see save_vacancies).
    batch_claimed: set = set()

    seen_ext_ids: set[str] = set()

    skipped_archived = 0
    skipped_junk = 0
    skipped_boilerplate = 0
    resurrected = 0
    refreshed_gated = 0
    skipped_inactive: dict[str, int] = {}

    for job in jobs:
        title, skip_reason, boilerplate_gated = _gate_job(job)
        if boilerplate_gated:
            skipped_boilerplate += 1
        if skip_reason == "junk":
            skipped_junk += 1
        elif skip_reason == "empty_title":
            print(f"  [{board_name}] skipped a board row with a missing/empty title", flush=True)
        if skip_reason is not None:
            # A gated title we ALREADY track is still being listed by the board:
            # refresh its last_seen so it isn't shown stale/expired while live.
            if skip_reason != "empty_title" and _refresh_gated_last_seen(
                cur,
                resolve_canonical_name(job.get("org_override") or board_cfg["name"]),
                title,
                today,
            ):
                refreshed_gated += 1
            continue

        ext_id = job.get("external_id", "")
        if ext_id:
            dedup_key = f"{board_name}|{ext_id}"
            if dedup_key in seen_ext_ids:
                continue
            seen_ext_ids.add(dedup_key)

        raw_org = job.get("org_override") or board_cfg["name"]
        org = resolve_canonical_name(raw_org)

        # Resolve or create company. Every unresolved org lands at the SAME
        # configurable status — including a name already known to the static
        # registry and a '[via BoardName]' aggregator placeholder. A "known
        # name" is not a fast lane around the review gate (STRATEGY guardrail
        # 2); junk placeholders get pruned as candidates by
        # filter_companies.py's aggregator check instead. See
        # _auto_discovery_status().
        company_id = resolve_company_id(org)
        if company_id is None:
            company_id = ensure_company(org, status=_auto_discovery_status())

        # Skip inactive companies (log the loss for visibility)
        cur.execute("SELECT status FROM company WHERE id = %s", (company_id,))
        comp_row = cur.fetchone()
        if comp_row and comp_row["status"] == "inactive":
            skipped_inactive[org] = skipped_inactive.get(org, 0) + 1
            continue

        dedup_hash = make_vacancy_id(org, title)
        norm_hash = make_normalized_id(org, title)
        desc_fp = description_fingerprint(job.get("full_description"))

        if filters.is_recently_archived(
            archived_hashes, dedup_hash
        ) or filters.is_recently_archived(archived_hashes, norm_hash):
            skipped_archived += 1
            continue

        dedup_index = dedup_index_cache.get(company_id)
        if dedup_index is None:
            dedup_index = _build_dedup_index(cur, org, company_id)
            dedup_index_cache[company_id] = dedup_index

        loc_entry = _make_location_entry(job)
        loc_key = _loc_key(loc_entry)

        # Check existing: exact hash first, then a same-company renamed/language variant.
        existing, match_kind, insert_hash = _find_existing_vacancy(
            cur,
            dedup_index,
            batch_hashes_by_org.get(org, set()),
            dedup_hash,
            norm_hash,
            desc_fp,
            job.get("url"),
            batch_claimed,
            candidate_desc_len=len(job.get("full_description") or ""),
            candidate_title=title,
        )
        batch_claimed.add(dedup_hash)

        if existing:
            updates = {"last_seen": today}
            is_rename = match_kind in ("norm", "desc")
            decided = existing.get("status") in _DECIDED_STATUSES
            if existing.get("status") in ("archived", "expiring"):
                updates["status"] = "unseen"
                updates["expiring_alerted_at"] = None
                if has_status_reason:
                    # Re-listed = alive: a stale archival reason must not
                    # linger on the now-live row.
                    updates["status_reason"] = None
                resurrected += 1
            # A true rename over time: repoint the surviving row at the new title.
            if is_rename:
                updates["title"] = title
                updates["dedup_hash"] = dedup_hash
            for field in ("snippet", "full_description"):
                if job.get(field) and not existing.get(field):
                    # Don't fill a decided row's description from an inexact match.
                    if field == "full_description" and is_rename and decided:
                        continue
                    updates[field] = job[field]
            if job.get("deadline") and not existing.get("deadline"):
                parsed_dl = _safe_deadline(job["deadline"])
                if parsed_dl:
                    updates["deadline"] = parsed_dl
            # Fallback: extract deadline from description if still missing
            if not existing.get("deadline") and "deadline" not in updates:
                extracted = _extract_deadline_from_description(job.get("full_description") or "")
                if extracted:
                    parsed_dl = _safe_deadline(extracted)
                    if parsed_dl:
                        updates["deadline"] = parsed_dl

            locs = existing.get("locations") or []
            existing_loc_keys = {_loc_key(l) for l in locs}
            if loc_key not in existing_loc_keys:
                locs.append(loc_entry)
                updates["locations"] = Json(locs)
            elif loc_entry.get("url"):
                # Same loc_key but a fresh apply URL — keep it (mirrors
                # save_vacancies). Without this, a same-title same-location
                # facet folded onto this row would lose its apply URL entirely.
                for loc in locs:
                    lk = loc.get("city") or loc.get("country") or loc.get("work_mode") or ""
                    if lk == loc_key:
                        loc["url"] = loc_entry["url"]
                        break
                updates["locations"] = Json(locs)

            # Backfill board provenance on a row that predates the column / was
            # first saved by another source but is now confirmed on this board.
            if write_source_board and not (existing.get("source_board") or ""):
                updates["source_board"] = board_name

            set_parts = [f"{k} = %s" for k in updates]
            vals = list(updates.values()) + [existing["id"]]
            cur.execute(f"UPDATE vacancy SET {', '.join(set_parts)} WHERE id = %s", vals)
        else:
            parsed_deadline = _resolve_new_deadline(job)
            cols = [
                "dedup_hash",
                "company_id",
                "title",
                "snippet",
                "full_description",
                "compensation",
                "deadline",
                "first_seen",
                "last_seen",
                "locations",
            ]
            vals = [
                insert_hash,
                company_id,
                title,
                job.get("snippet", ""),
                job.get("full_description", ""),
                job.get("compensation", ""),
                parsed_deadline,
                today,
                today,
                Json([loc_entry]),
            ]
            if write_source_board:
                cols.append("source_board")
                vals.append(board_name)
            placeholders = ", ".join(["%s"] * len(cols))
            cur.execute(
                f"INSERT INTO vacancy ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            new_count += 1

    cur.close()
    _print_merge_summary(
        board_name,
        skipped_archived=skipped_archived,
        skipped_boilerplate=skipped_boilerplate,
        skipped_junk=skipped_junk,
        resurrected=resurrected,
        refreshed_gated=refreshed_gated,
    )
    if skipped_inactive:
        total_skipped = sum(skipped_inactive.values())
        top3 = sorted(skipped_inactive.items(), key=lambda x: -x[1])[:3]
        top3_str = ", ".join(f"{name} ({n})" for name, n in top3)
        print(
            f"  [{board_name}] ⚠ {total_skipped} vacancies from {len(skipped_inactive)}"
            f" inactive companies skipped: {top3_str}",
            flush=True,
        )
    return new_count


# ---------------------------------------------------------------------------
# Company fitness (for filter gate)
# ---------------------------------------------------------------------------


def auto_review_candidates(approve_threshold=None, reject_threshold=None, enabled=None) -> dict:
    """Auto-approve/reject candidate companies by alignment_score threshold.

    OPT-IN: this mutates company status with no human in the loop, so it does
    nothing unless explicitly enabled. Enable by passing ``enabled=True`` or by
    setting env ``AUTO_REVIEW_CANDIDATES=1``. When disabled, every candidate is
    left untouched and returned under "pending".

    Thresholds default to env ``AUTO_REVIEW_APPROVE`` / ``AUTO_REVIEW_REJECT``
    (falling back to 60 / 25). Pass explicit numbers to override.

    Companies with alignment_score >= approve_threshold → active.
    Companies with alignment_score <= reject_threshold → inactive.
    Grey zone (between thresholds) → stays candidate for manual review.

    Returns summary: {"approved": [...], "rejected": [...], "pending": [...]}.

    COMMIT CONTRACT: like every other write in this module, this stages its
    UPDATEs on the shared connection and leaves the commit to the caller (see
    the module docstring). Call ``get_conn().commit()`` after it or the
    approve/reject changes are silently rolled back at connection close.
    """
    import os

    if enabled is None:
        enabled = os.environ.get("AUTO_REVIEW_CANDIDATES", "").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    if approve_threshold is None:
        approve_threshold = int(os.environ.get("AUTO_REVIEW_APPROVE", "60"))
    if reject_threshold is None:
        reject_threshold = int(os.environ.get("AUTO_REVIEW_REJECT", "25"))

    if not enabled:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT canonical_name FROM company "
            "WHERE status = 'candidate' AND alignment_score IS NOT NULL"
        )
        pending = [r[0] for r in cur.fetchall()]
        cur.close()
        return {"approved": [], "rejected": [], "pending": pending}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, canonical_name, alignment_score
        FROM company
        WHERE status = 'candidate'
          AND alignment_score IS NOT NULL
    """)
    rows = cur.fetchall()

    approved = []
    rejected = []
    pending = []

    for cid, name, score in rows:
        if score >= approve_threshold:
            cur.execute(
                "UPDATE company SET status = 'active', status_reason = %s WHERE id = %s",
                (f"auto-approved: alignment={score}", cid),
            )
            approved.append(name)
        elif score <= reject_threshold:
            cur.execute(
                "UPDATE company SET status = 'inactive', status_reason = %s WHERE id = %s",
                (f"auto-rejected: alignment={score}", cid),
            )
            rejected.append(name)
        else:
            pending.append(name)

    cur.close()
    # No commit here — the caller owns the transaction (see docstring).
    return {"approved": approved, "rejected": rejected, "pending": pending}


def save_company_evidence(
    company_id: str,
    source: str,
    *,
    url: str = "",
    content: str = "",
    meta: "dict | None" = None,
) -> None:
    """Save one piece of company research into ``company_evidence`` and commit.

    Pre-application research lands in the SAME table that feeds WANT-scoring, so
    what you learn while preparing an application also enriches the company's
    desirability signal and is visible in the company profile — instead of
    rotting in a folder. Idempotent by (company_id, source, url): re-saving the
    same page refreshes it in place rather than duplicating.

    ``source`` is free-form; note ``score_companies`` only feeds a fixed set of
    primary sources to the scorer (website/careers/manual_url/exa/...), so use
    ``manual_url`` for research you want the scorer to weigh, or a custom label
    (e.g. ``application_research``) for profile-only notes. The DAL leaves that
    choice to the caller and never auto-commits elsewhere — this write does.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM company_evidence WHERE company_id = %s AND source = %s AND url = %s",
        (company_id, source, url or ""),
    )
    cur.execute(
        """INSERT INTO company_evidence (company_id, source, url, content, meta)
           VALUES (%s, %s, %s, %s, %s)""",
        (
            company_id,
            source,
            url or "",
            content or "",
            json.dumps(meta) if meta is not None else None,
        ),
    )
    cur.close()
    conn.commit()


def load_company_evidence_summary() -> dict[str, list[dict]]:
    """Return {company_id_str: [{source, url, fetched_at}]} for every company.

    Metadata only (no ``content`` — the raw scraped text can be large): enough
    for the company profile to show what research exists and link out to it.
    Ordered newest-first. Empty when the table is absent (fresh DB, pre-migration
    on the Postgres baseline)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT company_id, source, url, fetched_at FROM company_evidence "
            "ORDER BY fetched_at DESC"
        )
        rows = cur.fetchall()
    except Exception:
        cur.close()
        return {}
    cur.close()
    out: dict[str, list[dict]] = {}
    for company_id_val, source, url, fetched_at in rows:
        out.setdefault(str(company_id_val), []).append(
            {
                "source": source or "",
                "url": url or "",
                "fetched_at": fetched_at.isoformat()
                if hasattr(fetched_at, "isoformat")
                else (str(fetched_at) if fetched_at else ""),
            }
        )
    return out


def get_company_fitness_map() -> dict[str, dict]:
    """Return {canonical_name: {alignment_score, status}} for all companies."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT canonical_name, alignment_score, status FROM company")
    result = {}
    for name, alignment, status in cur.fetchall():
        result[name] = {"alignment_score": alignment, "status": status}
    cur.close()
    return result


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def get_vacancy_statuses() -> dict[str, str]:
    """Return {uuid_str: status} for all vacancies."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, status FROM vacancy")
    result = {str(r[0]): r[1] for r in cur.fetchall()}
    cur.close()
    return result


def update_vacancy_status(vacancy_uuid: str, status: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE vacancy SET status = %s, status_updated_at = now() WHERE id = %s",
        (status, vacancy_uuid),
    )
    cur.close()


def batch_update_statuses(updates: dict[str, str]):
    """Batch update vacancy statuses. {uuid: status}."""
    if not updates:
        return
    conn = get_conn()
    cur = conn.cursor()
    for uid, status in updates.items():
        cur.execute(
            "UPDATE vacancy SET status = %s, status_updated_at = now() WHERE id = %s",
            (status, uid),
        )
    cur.close()


def get_protected_ids() -> set[str]:
    """Return UUIDs of vacancies with non-unseen status (protected from archival)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM vacancy WHERE status != 'unseen'")
    result = {str(r[0]) for r in cur.fetchall()}
    cur.close()
    return result


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# Accepted us_eligibility verdicts; the last one names roles that cannot be
# worked from abroad and is auto-archived below. Kept as data (a tuple + a
# named member) rather than inline literals so the eligibility value never
# appears as a branch literal in the logic.
_ELIG_VERDICTS = ("outside_us_ok", "us_only", "unclear")
_ELIG_ARCHIVE = _ELIG_VERDICTS[1]


def _geo_hard_banned(country: str, work_mode: str) -> bool:
    """True if a scored role's country is hard-banned by the profile geo policy.

    Save-time safety net for roles the pre-score filter could not resolve but the
    scorer's extracted `country` can. Remote roles are never banned (reachable
    from anywhere). Delegates the ban decision to the shared geo.country_banned
    predicate over config's pre-built (canonicalised) policy caches.
    """
    if is_remote_mode(work_mode):
        return False
    return country_banned(
        country,
        banned_regions=GEO_BANNED_REGIONS,
        keep_countries=GEO_KEEP_COUNTRIES_SET,
        banned_countries=GEO_BANNED_COUNTRIES,
    )


def update_llm_score(vacancy_uuid: str, score_data: dict):
    """Update LLM score fields for a vacancy.

    ``score_data["scored_by"]`` (optional) records which model tier produced
    this score — the two-pass driver's screen pass writes the cheap model's
    name here, and an escalation overwrites it with the strong model's name
    on re-score. Omitted/absent writes NULL (unchanged behaviour for callers
    that predate two-pass scoring).
    """
    conn = get_conn()
    cur = conn.cursor()
    hard_reqs = score_data.get("llm_hard_requirements", [])
    if not isinstance(hard_reqs, list):
        hard_reqs = []

    # LLM-extracted deadline: only fill if vacancy has no deadline yet
    llm_dl = score_data.get("llm_deadline")
    dl_clause = ""
    dl_params: list = []
    if llm_dl:
        parsed = _safe_deadline(llm_dl)
        if parsed:
            dl_clause = ", deadline = COALESCE(deadline, %s)"
            dl_params = [parsed]

    # US work-eligibility (orthogonal to fit score). Only written when supplied.
    elig = score_data.get("us_eligibility")
    elig_clause = ""
    elig_params: list = []
    if elig in _ELIG_VERDICTS:
        elig_clause = ", us_eligibility = %s"
        elig_params = [elig]

    # Provenance (two-pass scoring): the model tier that produced THIS score.
    # Only written once migration 0009 has actually run — an install that
    # hasn't migrated yet must keep scoring, just without provenance, rather
    # than crash on "no such column" (see _scored_by_supported()). NULL when
    # the caller doesn't report a model (a caller that pre-dates two-pass
    # scoring) — the dashboard treats a NULL scored_by as "no badge", never a
    # crash either way.
    sb_clause = ""
    sb_params: list = []
    if _scored_by_supported():
        sb_clause = ", scored_by = %s"
        sb_params = [score_data.get("scored_by")]

    cur.execute(
        f"""UPDATE vacancy SET
               llm_score = %s, llm_reasoning = %s, llm_summary = %s,
               llm_hard_requirements = %s, llm_scored_at = now()
               {dl_clause}{elig_clause}{sb_clause}
           WHERE id = %s""",
        (
            score_data.get("llm_score"),
            score_data.get("llm_reasoning"),
            score_data.get("llm_summary"),
            json.dumps(hard_reqs),
            *dl_params,
            *elig_params,
            *sb_params,
            vacancy_uuid,
        ),
    )
    rowcount = cur.rowcount

    # Drop roles that geography makes unreachable: US/Canada-bound (us_only, only
    # when the profile opts in via ban_us_only) or a banned-region country the
    # pre-score filter missed. Kept out of the active view, never deleted so the
    # verdict is auditable. Only unseen rows — never override a user decision.
    geo_ban = (elig == _ELIG_ARCHIVE and GEO_BAN_US_ONLY) or _geo_hard_banned(
        score_data.get("country"), score_data.get("work_mode")
    )
    if geo_ban:
        cur.execute(
            """UPDATE vacancy SET status = 'archived', status_updated_at = now()
               WHERE id = %s AND status = 'unseen'""",
            (vacancy_uuid,),
        )

    cur.close()
    return rowcount


def reset_llm_scores(member_ids: list[str]) -> int:
    """Null the ``llm_score`` for a set of vacancies so they read as unscored.

    Used by the two-pass driver at the screen->escalate handshake: a
    finalist's cheap SCREEN score is cleared so the strong pass re-scores it and
    the driver can reuse the same ``llm_score IS NULL`` idempotency for BOTH
    passes. The strong pass overwrites each with its own score; an interrupted
    escalate simply leaves the finalists unscored, and a later run re-screens and
    re-escalates them — never a silent stale cheap score.

    Like the other DAL writers this does NOT commit — the caller owns the
    transaction (see AGENTS.md). Returns the number of rows nulled.
    """
    if not member_ids:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE vacancy SET llm_score = NULL WHERE id = ANY(%s::uuid[])",
        ([str(m) for m in member_ids],),
    )
    rowcount = cur.rowcount
    cur.close()
    return rowcount


# ---------------------------------------------------------------------------
# Source tracking
# ---------------------------------------------------------------------------


def update_source_tracking(
    org_name: str, tier, strategy: str, new_count: int, fetch_status: str = FETCH_STATUS_OK
):
    """Update company source tracking metadata.

    If fetch succeeded (status=ok) but vacancy_count=0, status becomes no_data.
    """
    canonical = resolve_canonical_name(org_name)
    conn = get_conn()
    cur = conn.cursor()

    # Count current vacancies for this company
    cur.execute(
        """SELECT count(*) FROM vacancy v
           JOIN company c ON v.company_id = c.id
           WHERE c.canonical_name = %s""",
        (canonical,),
    )
    vacancy_count = cur.fetchone()[0]

    # Refine status: ok with 0 vacancies → no_data
    if fetch_status == FETCH_STATUS_OK and vacancy_count == 0:
        fetch_status = FETCH_STATUS_NO_DATA

    # Health telemetry (migration 0015). last_fetched = last ATTEMPT; last_success
    # = last time it actually worked (ok / genuinely-empty). consecutive_failures
    # counts only real errors so a monitor can alert on a persistent break, not a
    # one-off blip. is_fetch_error() treats no_data / render_ok_zero as healthy.
    # Degrades on a pre-0015 schema (fresh simple-mode DB) to the base columns.
    if _table_has_column("company", "last_success"):
        if is_fetch_error(fetch_status):
            cur.execute(
                """UPDATE company SET
                       last_fetched = now(), vacancy_count = %s,
                       fetch_status = %s, fetch_error = %s,
                       consecutive_failures = COALESCE(consecutive_failures, 0) + 1
                   WHERE canonical_name = %s""",
                (vacancy_count, fetch_status, fetch_status, canonical),
            )
        else:
            cur.execute(
                """UPDATE company SET
                       last_fetched = now(), vacancy_count = %s,
                       fetch_status = %s, fetch_error = NULL,
                       last_success = now(), consecutive_failures = 0
                   WHERE canonical_name = %s""",
                (vacancy_count, fetch_status, canonical),
            )
    else:
        cur.execute(
            """UPDATE company SET
                   last_fetched = now(), vacancy_count = %s,
                   fetch_status = %s
               WHERE canonical_name = %s""",
            (vacancy_count, fetch_status, canonical),
        )
    cur.close()


def sync_boards(boards: dict) -> None:
    """Upsert the board catalog (id → cfg) into the `board` table.

    Keeps name/strategy/tier/ttl_days/url in sync with config (JOB_BOARDS) so
    the dashboard's Boards tab and the board-statuses API have a single source
    of truth. last_fetched is left untouched (owned by mark_board_fetched).
    Called at the start of a fetch run.
    """
    if not boards:
        return
    conn = get_conn()
    cur = conn.cursor()
    for board_id, cfg in boards.items():
        cur.execute(
            """INSERT INTO board (id, name, strategy, tier, ttl_days, url)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   name = EXCLUDED.name,
                   strategy = EXCLUDED.strategy,
                   tier = EXCLUDED.tier,
                   ttl_days = EXCLUDED.ttl_days,
                   url = EXCLUDED.url,
                   updated_at = now()""",
            (
                board_id,
                cfg.get("name", board_id),
                cfg.get("strategy"),
                cfg.get("tier"),
                cfg.get("ttl_days"),
                cfg.get("url"),
            ),
        )
    cur.close()


def should_fetch_board(board_id: str, ttl_days: int) -> bool:
    """Return True if board has not been scraped within ttl_days."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT last_fetched FROM board WHERE id = %s",
        (board_id,),
    )
    row = cur.fetchone()
    cur.close()
    if not row or not row[0]:
        return True
    last_dt = row[0]
    if isinstance(last_dt, datetime):
        return (datetime.now(last_dt.tzinfo) - last_dt).days >= ttl_days
    return True


def mark_board_fetched(
    board_id: str,
    jobs_returned: int | None = None,
    fetch_status: str = FETCH_STATUS_OK,
):
    """Record a board fetch outcome + health telemetry (migration 0015).

    Inserts a bare row if the board is not yet in the catalog (a fetch can run
    before sync_boards in edge cases); sync_boards backfills the metadata.

    ``jobs_returned`` is the RAW count the fetcher returned this run (not the
    post-dedup "new" count) — the honest signal for "is this board alive". A
    board that returns 200 rows of which 0 are new is healthy; one that returns
    0 is the problem. ``fetch_status`` is "ok" / a genuinely-empty reason /
    an error string; is_fetch_error() decides which columns move.
    """
    conn = get_conn()
    cur = conn.cursor()

    # Pre-0015 schema (fresh simple-mode DB): board has only last_fetched.
    # Degrade to the original bare mark rather than raising "no such column".
    if not _table_has_column("board", "vacancy_count"):
        cur.execute(
            """INSERT INTO board (id, name, last_fetched)
               VALUES (%s, %s, now())
               ON CONFLICT (id) DO UPDATE SET
                   last_fetched = now(), updated_at = now()""",
            (board_id, board_id),
        )
        cur.close()
        return

    # Streak: read the prior value and compute in Python so the UPSERT stays
    # portable (referencing the existing row inside ON CONFLICT differs between
    # Postgres and SQLite). is_fetch_error() treats a genuinely-empty board as
    # healthy, so only real errors bump the streak.
    if is_fetch_error(fetch_status):
        cur.execute("SELECT consecutive_failures FROM board WHERE id = %s", (board_id,))
        row = cur.fetchone()
        streak = (row[0] or 0) + 1 if row else 1
        cur.execute(
            """INSERT INTO board (id, name, last_fetched, vacancy_count,
                   fetch_status, last_error, consecutive_failures)
               VALUES (%s, %s, now(), %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   last_fetched = now(), updated_at = now(),
                   vacancy_count = EXCLUDED.vacancy_count,
                   fetch_status = EXCLUDED.fetch_status,
                   last_error = EXCLUDED.last_error,
                   consecutive_failures = EXCLUDED.consecutive_failures""",
            (board_id, board_id, jobs_returned, fetch_status, fetch_status, streak),
        )
    else:
        cur.execute(
            """INSERT INTO board (id, name, last_fetched, vacancy_count,
                   fetch_status, last_error, last_success, consecutive_failures)
               VALUES (%s, %s, now(), %s, %s, NULL, now(), 0)
               ON CONFLICT (id) DO UPDATE SET
                   last_fetched = now(), updated_at = now(),
                   vacancy_count = EXCLUDED.vacancy_count,
                   fetch_status = EXCLUDED.fetch_status,
                   last_error = NULL,
                   last_success = now(),
                   consecutive_failures = 0""",
            (board_id, board_id, jobs_returned, fetch_status),
        )
    cur.close()


class BoardPersistenceUnavailable(RuntimeError):
    """The schema predates board persistence (no board table / enabled column).

    Raised instead of the backend's raw column/table error so callers can tell
    "not migrated yet" (expected on a fresh clone; run_daily degrades to the
    manual override) apart from a real DB failure, and so the user-facing
    message says what to run instead of a traceback."""


def _board_schema_missing(exc: Exception) -> bool:
    """True when the error means migration 0011 hasn't been applied (either
    dialect), as opposed to a genuine query/connection failure."""
    msg = str(exc).lower()
    return (
        "no such table: board" in msg
        or "has no column named enabled" in msg
        or "no such column: enabled" in msg
        or ("does not exist" in msg and ("enabled" in msg or 'relation "board"' in msg))
    )


_MIGRATE_HINT = (
    "board persistence is not set up in this database yet "
    "(missing board.enabled, added by migration 0011) — run: python3 scripts/migrate.py"
)


def set_board_enabled(board_id: str, enabled: bool = True) -> None:
    """Persist whether a job board participates in future runs.

    Enabled boards are unioned into every run's board set by run_daily.py, so an
    enabled board keeps fetching with no env var and no reminder; JOB_BOARDS /
    --boards stays a manual override applied ON TOP. Upserts a bare catalog row
    when the board has never been synced (sync_boards backfills name/strategy/
    ttl on the next fetch), mirroring mark_board_fetched.

    Commits: the status writers in this module deliberately do NOT commit and
    lean on a caller commit, but this is a discrete user action ("enable this
    board") whose whole point is to survive the process -- a forgotten caller
    commit would silently lose it, so the persistence is committed here.

    Raises BoardPersistenceUnavailable (with the migrate command) on a schema
    that predates migration 0011."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO board (id, name, enabled)
               VALUES (%s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   enabled = EXCLUDED.enabled, updated_at = now()""",
            (board_id, board_id, bool(enabled)),
        )
    except Exception as exc:
        conn.rollback()
        if _board_schema_missing(exc):
            raise BoardPersistenceUnavailable(_MIGRATE_HINT) from exc
        raise
    cur.close()
    conn.commit()


def set_board_hidden(board_id: str, hidden: bool = True) -> None:
    """Persist whether a board is hidden from the dashboard's Boards tab.

    Hidden != disabled: disabling stops a board fetching (set_board_enabled),
    hiding only removes a board from the dashboard render (api/board-statuses.js
    surfaces the flag, public/modules/boards.js filters on it) so a curated view
    isn't cluttered by boards kept off. A board can be disabled AND hidden (the
    curation set), or hidden while still enabled. The row is never deleted.

    Mirrors set_board_enabled: upserts a bare catalog row when the board has
    never been synced, and COMMITS here (a discrete user action whose effect
    must survive the process, not wait on a caller commit).

    The `hidden` column arrives in a later migration than 0011, so it may be
    absent on a partially-migrated DB. Guarded with _table_has_column so the
    write raises the run-migrate hint (BoardPersistenceUnavailable) rather than
    a raw "no such column" traceback."""
    if not _table_has_column("board", "hidden"):
        raise BoardPersistenceUnavailable(
            "board.hidden column is missing (added by the board-hidden migration) — "
            "run: python3 scripts/migrate.py"
        )
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO board (id, name, hidden)
               VALUES (%s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   hidden = EXCLUDED.hidden, updated_at = now()""",
            (board_id, board_id, bool(hidden)),
        )
    except Exception as exc:
        conn.rollback()
        if _board_schema_missing(exc):
            raise BoardPersistenceUnavailable(_MIGRATE_HINT) from exc
        raise
    cur.close()
    conn.commit()


def get_enabled_boards() -> list[str]:
    """Board ids persisted as enabled -- they participate in every run until
    disabled (see set_board_enabled). Sorted for stable, diffable output.

    Raises BoardPersistenceUnavailable (with the migrate command) on a schema
    that predates migration 0011."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM board WHERE enabled ORDER BY id")
    except Exception as exc:
        conn.rollback()
        if _board_schema_missing(exc):
            raise BoardPersistenceUnavailable(_MIGRATE_HINT) from exc
        raise
    ids = [r[0] for r in cur.fetchall()]
    cur.close()
    return ids


def archive_board_vacancies(board_id: str) -> int:
    """Archive a disabled board's still-unseen vacancies so they stop
    lingering forever. Returns the archived count.

    A disabled board stops fetching, but nothing else ever revisits its old
    rows: gone-from-source detection (archive_gone_vacancies) only runs on a
    direct-ATS re-fetch, never on a board. Without this, every 'unseen' row a
    disabled board ever produced sits stuck in the pipeline permanently.
    Mirrors archive_gone_vacancies's in-place UPDATE (status -> 'archived',
    never a delete) and records status_reason = 'board_disabled' the same way
    company.status_reason already records why a company's status changed.
    Only status = 'unseen' rows are touched -- liked/to_apply/passed/applied/
    expiring/already-archived rows (any real decision or prior scoring) are
    never revisited.

    vacancy.source_board stores the board's display NAME (stamped by
    save_board_vacancies), not its config id, so board_id is resolved to a
    name via the board catalog first -- this also lets a since-removed board
    id (config no longer ships it, matching disable-board's own tolerance for
    unknown ids) still match its historical rows. A board never synced to the
    catalog (no row, or no name) has nothing to resolve against and archives
    nothing. Rows whose source_board IS NULL (imported before the stamping
    change, or via direct ATS) are unreachable by this function -- permanently,
    not just until some later run: they carry no board provenance to ever
    match on.

    CAVEAT: board.name carries no UNIQUE constraint, so two catalog rows could
    in principle share one display name -- and a name-keyed archive would then
    sweep the OTHER board's unseen rows too. Guarded by a reverse lookup: when
    the resolved name maps back to more than one board id, the archive aborts
    with a RuntimeError instead of proceeding.

    Re-enabling the board later does NOT resurrect these rows: a fresh fetch
    brings back whatever is still live on its own merits, exactly like any
    other archived vacancy.

    Commits internally, matching set_board_enabled: disabling a board is a
    discrete user action whose effect must survive the process, not wait on a
    caller's commit. Any failure rolls the staged UPDATE back (mirroring
    set_board_enabled's rollback-then-raise) so the connection is left clean
    and no partial archive is ever committed; cmd_disable reports the error
    and exits non-zero.

    Degrades to a no-op (returns 0) on a schema that predates migration 0013
    -- no source_board column means no board provenance to resolve against,
    the same guard save_board_vacancies already uses for writing it.
    """
    if not _vacancy_has_column("source_board"):
        return 0

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM board WHERE id = %s", (board_id,))
        row = cur.fetchone()
        board_name = row[0] if row else None
        if not board_name:
            return 0

        # Reverse check: board.name is not UNIQUE, so refuse to archive when
        # the display name is shared -- a name-keyed UPDATE would hit the
        # other board's rows too.
        cur.execute("SELECT id FROM board WHERE name = %s", (board_name,))
        sharing_ids = sorted(r[0] for r in cur.fetchall())
        if len(sharing_ids) > 1:
            raise RuntimeError(
                f"board name '{board_name}' is shared by boards "
                f"{', '.join(sharing_ids)} -- refusing to archive by name; "
                "rename the duplicate catalog rows first"
            )

        cur.execute(
            "SELECT id FROM vacancy WHERE source_board = %s AND status = 'unseen'",
            (board_name,),
        )
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            return 0

        placeholders = ", ".join(["%s"] * len(ids))
        if _vacancy_has_column("status_reason"):
            cur.execute(
                "UPDATE vacancy SET status = 'archived', status_reason = 'board_disabled', "
                f"status_updated_at = now() WHERE id IN ({placeholders})",
                ids,
            )
        else:
            cur.execute(
                "UPDATE vacancy SET status = 'archived', status_updated_at = now() "
                f"WHERE id IN ({placeholders})",
                ids,
            )
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    conn.commit()
    return len(ids)


# ---------------------------------------------------------------------------
# Archive hash dedup
# ---------------------------------------------------------------------------


def get_archived_hashes(ttl_days: int = ARCHIVE_TTL_DAYS, *, include_gone: bool = True) -> set[str]:
    """Return dedup_hashes archived within TTL.

    include_gone=False excludes 'gone_from_source' tombstones, matching the
    direct-ATS resurrection rule (the company's own ATS re-listing is ground
    truth that a role reopened, so a gone-from-source archive must not block
    it). Boards pass include_gone=True (full set) so lagging feeds cannot
    resurrect a posting the source closed.
    """
    conn = get_conn()
    cur = conn.cursor()
    query = "SELECT dedup_hash FROM archived_hash WHERE archived_at > now() - interval '%s days'"
    if not include_gone:
        query += " AND reason IS DISTINCT FROM 'gone_from_source'"
    cur.execute(query, (ttl_days,))
    result = {r[0] for r in cur.fetchall()}
    cur.close()
    return result


def record_archived_hashes(entries: list[tuple]):
    """Bulk-insert archived tombstones. entries = [(dedup_hash, reason[, norm_hash]), ...].

    Each entry may carry an optional third element — the role's normalized dedup
    key. When present (and distinct), it is tombstoned as a SECOND row under the
    same reason, so a renamed / re-punctuated variant of a buried role stays
    buried on the next fetch. The archived-hash set is keyed only by the hash
    column, so a normalized key lives there as its own row rather than needing a
    schema change — backward-compatible with every existing tombstone and with
    2-tuple callers.
    """
    if not entries:
        return
    rows: list[tuple[str, str]] = []
    for entry in entries:
        dedup_hash, reason = entry[0], entry[1]
        norm_hash = entry[2] if len(entry) > 2 else None
        if dedup_hash:
            rows.append((dedup_hash, reason))
        if norm_hash and norm_hash != dedup_hash:
            rows.append((norm_hash, reason))
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO archived_hash (dedup_hash, reason) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        rows,
    )
    cur.close()


# ---------------------------------------------------------------------------
# Gone-from-source detection
# ---------------------------------------------------------------------------


class ArchivedCount(int):
    """Archived-as-gone count that also carries the protected-expiring count.

    Behaves as a plain ``int`` (comparisons, arithmetic, ``int(x or 0)``) so
    existing callers that treat the return value as an archived count keep
    working unchanged. New callers can read ``.protected`` for the count of
    high-fit roles flipped to 'expiring' instead of archived — those still
    vanished from the source and belong in "gone" telemetry.
    """

    def __new__(cls, archived: int, protected: int = 0):
        obj = super().__new__(cls, archived)
        obj.protected = protected
        return obj


def archive_gone_vacancies(org_name: str, fetched_jobs: list[dict]) -> "ArchivedCount":
    """Archive unseen vacancies absent from a fresh direct-ATS listing.

    The company's own ATS is ground truth: an unseen vacancy whose dedup_hash
    is not in the fresh fetch was closed at the source. Decided statuses
    (liked, to_apply, applied, ...) are never touched. Hashes are recorded as
    'gone_from_source' so lagging job boards can't re-import the dead posting,
    while a direct re-listing still resurrects it (the save_* paths filter via
    filters.is_recently_archived against get_archived_hashes(include_gone=...)).

    Caller must guarantee the fetch succeeded and the strategy returns the
    company's complete listing — an empty list from a partial/failed fetch
    would mass-archive live vacancies.
    """
    org = resolve_canonical_name(org_name)
    company_id = resolve_company_id(org)
    if company_id is None:
        return ArchivedCount(0)
    fetched_titles = [_sanitize_title(j.get("title", "")) for j in fetched_jobs]
    fetched_hashes = {make_vacancy_id(org, t) for t in fetched_titles}
    fetched_norm = {make_normalized_id(org, t) for t in fetched_titles}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id, dedup_hash, title, llm_score FROM vacancy "
        "WHERE company_id = %s AND status = 'unseen'",
        (company_id,),
    )
    # A row is gone only if NEITHER its exact hash NOR its normalized key is in
    # the fresh listing — so a role re-listed under a renamed / re-punctuated
    # title (already merged onto this row by save_vacancies) is kept, not
    # archived.
    gone = [
        r
        for r in cur.fetchall()
        if r["dedup_hash"] not in fetched_hashes
        and make_normalized_id(org, r["title"] or "") not in fetched_norm
    ]
    if not gone:
        cur.close()
        return ArchivedCount(0)

    # Latency protection (KTD1/KTD2): a high-fit role that vanished from its
    # source is exactly the scarce decision we must not lose silently. Flip it
    # to 'expiring' (kept visible, alerted in telegram_digest) instead of
    # archiving, and do NOT tombstone it — so a re-listing resurrects it freely.
    protected = [r for r in gone if (r["llm_score"] or 0) >= PROTECT_SCORE]
    to_archive = [r for r in gone if (r["llm_score"] or 0) < PROTECT_SCORE]

    if protected:
        cur.execute(
            "UPDATE vacancy SET status = 'expiring', status_updated_at = NOW() "
            "WHERE id = ANY(%s::uuid[])",
            ([str(r["id"]) for r in protected],),
        )
    if to_archive:
        cur.execute(
            "UPDATE vacancy SET status = 'archived', status_updated_at = NOW() "
            "WHERE id = ANY(%s::uuid[])",
            ([str(r["id"]) for r in to_archive],),
        )
    cur.close()
    if to_archive:
        record_archived_hashes(
            [
                (r["dedup_hash"], "gone_from_source", make_normalized_id(org, r["title"] or ""))
                for r in to_archive
            ]
        )
        titles = ", ".join(sorted(r["title"] for r in to_archive)[:3])
        print(f"  [{org}] archived {len(to_archive)} gone from source: {titles}", flush=True)
    if protected:
        ptitles = ", ".join(sorted(r["title"] for r in protected)[:3])
        print(
            f"  [{org}] PROTECTED {len(protected)} high-fit gone from source → expiring: {ptitles}",
            flush=True,
        )
    return ArchivedCount(len(to_archive), len(protected))


# ---------------------------------------------------------------------------
# Auto-pass expired
# ---------------------------------------------------------------------------


def pass_expired_vacancies() -> int:
    """Auto-resolve expired unseen vacancies past their deadline.

    High-fit roles (llm_score >= PROTECT_SCORE) are NOT silently passed — they
    flip to 'expiring' so the scarce decision stays visible and alerted
    (KTD1/KTD2). Everything below the threshold is passed as before. Returns the
    count auto-passed (the protected→expiring count is logged separately).
    """
    conn = get_conn()
    cur = conn.cursor()
    # Protect first so the rows leave 'unseen' before the pass sweep runs.
    cur.execute(
        """
        UPDATE vacancy SET status = 'expiring', status_updated_at = NOW()
        WHERE deadline IS NOT NULL
          AND deadline < CURRENT_DATE
          AND status = 'unseen'
          AND COALESCE(llm_score, 0) >= %s
    """,
        (PROTECT_SCORE,),
    )
    protected = cur.rowcount
    cur.execute("""
        UPDATE vacancy SET status = 'passed', status_updated_at = NOW()
        WHERE deadline IS NOT NULL
          AND deadline < CURRENT_DATE
          AND status = 'unseen'
    """)
    count = cur.rowcount
    cur.close()
    if count:
        print(f"  Auto-passed {count} expired unseen vacancies", flush=True)
    if protected:
        print(f"  PROTECTED {protected} expired high-fit roles → expiring", flush=True)
    return count


def archive_stale_board_vacancies(
    fetched_boards, stale_days: int | None = None
) -> "ArchivedCount":
    """Auto-archive board-sourced vacancies not re-seen by any source in a while.

    Gone-from-source reconciliation (archive_gone_vacancies) only runs on a
    direct-ATS re-fetch, so it never covers board rows: a board-sourced vacancy
    that drops off its board is never revisited and lingers 'unseen' forever,
    accumulating zombies that waste LLM scoring budget. This is the board-row
    analogue: a row whose board WAS successfully fetched this run yet whose
    last_seen (bumped every run the role is still listed by ANY source) is older
    than ``stale_days`` days is treated as gone and archived in place
    (status -> 'archived', never a delete), recording status_reason =
    'board_stale' -- a bare machine token, mirroring archive_board_vacancies's
    'board_disabled'.

    ``fetched_boards`` is the POSITIVE-EVIDENCE precondition: the set of board
    display names (as stamped into vacancy.source_board) successfully fetched in
    THIS run. Only rows from those boards are eligible -- wall-clock staleness
    alone is never enough, otherwise a --no-boards run, a TTL-skipped board
    (hn_whoishiring's ttl_days=30 exceeds the default window by construction)
    or a silently broken fetcher would convert fetch absence into mass archival
    of live roles. Empty/None set -> no-op.

    Latency protection (KTD1/KTD2), mirroring both sibling sweeps: rows with
    llm_score >= PROTECT_SCORE are NOT silently archived -- they flip to
    'expiring' (kept visible, alerted) and are never tombstoned. Archived rows
    ARE tombstoned as 'gone_from_source' via record_archived_hashes, exactly
    like archive_gone_vacancies, so a stale board snapshot cannot resurrect the
    closed posting while a direct-ATS re-listing still can.

    Only status = 'unseen' rows are touched -- decided statuses (liked/to_apply/
    passed/applied/expiring) and company-fetched rows (source_board IS NULL,
    reconciled against their own ATS) are never revisited.

    ``stale_days`` defaults to BOARD_STALE_DAYS (resolved at call time, so
    patching the module global works); 0 disables the sweep entirely, matching
    the llm_score_threshold convention. Does NOT commit -- mirrors
    pass_expired_vacancies; the fetch driver commits. Degrades to a no-op on a
    schema predating migration 0013/0014 (no source_board / status_reason
    column). Returns ArchivedCount (archived, .protected).
    """
    if stale_days is None:
        stale_days = BOARD_STALE_DAYS
    if stale_days <= 0:  # 0 = disabled, like llm_score_threshold
        return ArchivedCount(0)
    if not fetched_boards:  # no board successfully fetched -> no absence evidence
        return ArchivedCount(0)
    if not (_vacancy_has_column("source_board") and _vacancy_has_column("status_reason")):
        return ArchivedCount(0)

    # last_seen is stored as an ISO 'YYYY-MM-DD' date string (see save_* /
    # refresh_* paths), so a lexicographic compare against a cutoff string works
    # on both backends without INTERVAL / julianday branching.
    cutoff = (datetime.now(DASHBOARD_TZ).date() - timedelta(days=stale_days)).isoformat()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    board_ph = ", ".join(["%s"] * len(fetched_boards))
    cur.execute(
        "SELECT v.id, v.dedup_hash, v.title, v.llm_score, c.canonical_name AS org "
        "FROM vacancy v LEFT JOIN company c ON v.company_id = c.id "
        f"WHERE v.status = 'unseen' AND v.source_board IN ({board_ph}) "
        "AND v.last_seen < %s",
        (*sorted(fetched_boards), cutoff),
    )
    rows = cur.fetchall()
    protected = [r for r in rows if (r["llm_score"] or 0) >= PROTECT_SCORE]
    to_archive = [r for r in rows if (r["llm_score"] or 0) < PROTECT_SCORE]

    if protected:
        ids = [r["id"] for r in protected]
        ph = ", ".join(["%s"] * len(ids))
        cur.execute(
            "UPDATE vacancy SET status = 'expiring', status_updated_at = NOW() "
            f"WHERE id IN ({ph})",
            ids,
        )
    if to_archive:
        ids = [r["id"] for r in to_archive]
        ph = ", ".join(["%s"] * len(ids))
        cur.execute(
            "UPDATE vacancy SET status = 'archived', status_updated_at = NOW(), "
            f"status_reason = 'board_stale' WHERE id IN ({ph})",
            ids,
        )
    cur.close()
    if to_archive:
        record_archived_hashes(
            [
                (
                    r["dedup_hash"],
                    "gone_from_source",
                    make_normalized_id(r["org"] or "", r["title"] or ""),
                )
                for r in to_archive
            ]
        )
        print(
            f"  Auto-archived {len(to_archive)} board-sourced vacancies "
            f"not re-seen for {stale_days} days",
            flush=True,
        )
    if protected:
        print(
            f"  PROTECTED {len(protected)} stale board-sourced high-fit roles → expiring",
            flush=True,
        )
    return ArchivedCount(len(to_archive), len(protected))


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def serialize_vacancy_rows(rows) -> dict:
    """Map vacancy rows to a JSON-safe {id: {...}} archive payload.

    Shared by archive_vacancies and scripts/dedup_sweep.py so both write the
    same on-disk record. Dates/datetimes become ISO strings; id and company_id
    become strings (UUIDs on Postgres, ints on SQLite).
    """
    out: dict = {}
    for r in rows:
        uid = str(r["id"])
        vac = dict(r)
        for df in ("first_seen", "last_seen", "deadline"):
            if isinstance(vac.get(df), date):
                vac[df] = vac[df].isoformat()
        for df in ("status_updated_at", "created_at", "updated_at"):
            if isinstance(vac.get(df), datetime):
                vac[df] = vac[df].isoformat()
        vac["id"] = uid
        if vac.get("company_id") is not None:
            vac["company_id"] = str(vac["company_id"])
        out[uid] = vac
    return out


def archive_vacancies(threshold: int = LLM_SCORE_THRESHOLD, force: bool = False) -> list[str]:
    """Archive low-scoring unseen vacancies.

    Writes local JSON archive BEFORE deleting from Supabase.
    Returns list of archived UUID strings.
    """
    # Score-threshold auto-archive is paused: scoring is now pure-fit and no
    # longer encodes geography, so the old <30 cap would wrongly delete
    # eligible roles. Archival stays opt-in via force=True until thresholds
    # are recalibrated for the pure-fit scale.
    if not force:
        print(
            "  Score-threshold archival paused (pure-fit scoring); thresholds "
            "need recalibration. Pass force=True to override."
        )
        return []

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Get protected IDs (non-unseen status)
    protected = get_protected_ids()

    # Find candidates for archival
    cur.execute(
        """SELECT v.*, c.canonical_name AS org
           FROM vacancy v JOIN company c ON v.company_id = c.id
           WHERE v.llm_score IS NOT NULL AND v.llm_score < %s
             AND v.status = 'unseen'""",
        (threshold,),
    )
    candidates = cur.fetchall()
    cur.close()

    to_remove = [r for r in candidates if str(r["id"]) not in protected]
    if not to_remove:
        return []

    # Atomicity contract: the DB DELETE and the on-disk JSON archive must never
    # disagree about what was removed — and neither side may lose data when the
    # other fails. Order:
    #   (1) build the archive payload in memory;
    #   (2) serialize it to a TEMP file in the archive dir — a full-disk /
    #       permissions / encoding failure raises HERE, before anything is
    #       staged on the connection, so nothing is deleted and there is no
    #       staged DELETE for a later unrelated commit to sweep up;
    #   (3) DELETE + record tombstones, then COMMIT — the durable source of
    #       truth for what was archived;
    #   (4) atomically os.replace() the temp file onto the final name (same
    #       directory → same filesystem). The final artifact appears only after
    #       the commit, so it can never describe a removal a rollback undid,
    #       and a kill mid-write can never leave a truncated file under the
    #       final name. If the rename itself fails, the committed payload is
    #       preserved in the temp file; its path + the UUIDs go to stderr.
    # archive_vacancies OWNS its transaction (it does NOT leave the commit to
    # the caller like the other DAL writes) precisely because that disk artifact
    # must be tied to a durable delete. This is the one intentional exception to
    # the "callers commit" rule; see AGENTS.md.
    import os
    import sys

    archive_dir = VACANCIES_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"archived_{datetime.now().strftime('%Y%m%d_%H%M')}.json"

    archived_data = serialize_vacancy_rows(to_remove)

    # (2) Serialize to a temp file BEFORE touching the DB (see contract above):
    # if this raises, the vacancies are still in the database, untouched.
    tmp_path = archive_path.with_name(archive_path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "archived_at": datetime.now().isoformat(),
                    "threshold": threshold,
                    "count": len(archived_data),
                    "vacancies": archived_data,
                },
                f,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
    except BaseException:
        # Nothing is staged on the connection yet — drop the partial temp file
        # and bail; the DB keeps every row.
        tmp_path.unlink(missing_ok=True)
        raise

    # (3) Delete from the DB and record dedup tombstones, then COMMIT — the
    # durable source of truth for what was archived.
    uuids = [r["id"] for r in to_remove]
    cur = conn.cursor()
    cur.execute("DELETE FROM vacancy WHERE id = ANY(%s::uuid[])", (uuids,))
    cur.close()

    # Record archived hashes for dedup (prevents re-fetch → re-score → re-archive).
    # record_archived_hashes tombstones the normalized key too (3-tuple) so a
    # renamed/re-punctuated variant of the buried role stays buried; it shares
    # this connection and does NOT commit, so the tombstones land in the same
    # transaction as the DELETE above (r carries c.canonical_name AS org).
    record_archived_hashes(
        [
            (
                r["dedup_hash"],
                "score_below_threshold",
                make_normalized_id(r.get("org", "") or "", r.get("title", "") or ""),
            )
            for r in to_remove
            if r.get("dedup_hash")
        ]
    )
    conn.commit()

    # (4) Atomically publish the already-written archive under its final name.
    # Same directory → same filesystem → os.replace is an atomic rename.
    try:
        os.replace(tmp_path, archive_path)
    except OSError:
        print(
            f"  ERROR: archive rename failed AFTER the delete was committed.\n"
            f"  The archived payload is preserved at: {tmp_path}\n"
            f"  Archived UUIDs: {', '.join(str(u) for u in uuids)}",
            file=sys.stderr,
        )
        raise

    print(
        f"  Archived {len(to_remove)} vacancies below LLM score {threshold}"
        f"\n  Archive: {archive_path.name}"
    )
    return [str(u) for u in uuids]


# ---------------------------------------------------------------------------
# Enrichment (replaces enriched.json)
# ---------------------------------------------------------------------------


def load_company_enrichment(org_name: str) -> dict:
    """Load enrichment data for a single company."""
    canonical = resolve_canonical_name(org_name)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT about, mission_fit, alignment_score, enriched_at FROM company WHERE canonical_name = %s",
        (canonical,),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return {}
    return {
        "about": row["about"] or {},
        "mission_fit": row["mission_fit"] or {},
        "alignment_score": float(row["alignment_score"])
        if row["alignment_score"] is not None
        else None,
        "enriched_at": row["enriched_at"].isoformat() if row["enriched_at"] else None,
    }


def calculate_company_tier(alignment_score, custom_boost=None):
    """Compute tier (S/A/B/C) + composite from the company alignment score.

    Tiering is alignment-only by default — no owner prestige axis. ``custom_boost``
    is an OPTIONAL user-defined signal (0..100) that ships unset; when a user's
    profile supplies one it nudges the composite, otherwise it has no effect.

    Mirrors scripts/report/data_prep.py:calculate_company_tier so frontend
    display and the DB `company.tier` column stay in sync.

    Returns (tier_letter, composite_score) — both None if no alignment data.
    """
    if alignment_score is None:
        return None, None
    th = settings.thresholds()
    composite = float(alignment_score)
    if custom_boost is not None:
        composite = th["composite_alignment_weight"] * composite + th[
            "composite_boost_weight"
        ] * float(custom_boost)
    composite = round(composite, 1)
    if composite >= th["tier_s"]:
        tier = "S"
    elif composite >= th["tier_a"]:
        tier = "A"
    elif composite >= th["tier_b"]:
        tier = "B"
    else:
        tier = "C"
    return tier, composite


def save_company_enrichment(org_name: str, about=None, mission_fit=None, alignment_score=None):
    """Save enrichment data for a company."""
    canonical = resolve_canonical_name(org_name)
    conn = get_conn()
    cur = conn.cursor()
    updates = ["enriched_at = now()"]
    vals = []
    if about is not None:
        updates.append("about = %s")
        vals.append(Json(about))
    if mission_fit is not None:
        updates.append("mission_fit = %s")
        vals.append(Json(mission_fit))
    if alignment_score is not None:
        updates.append("alignment_score = %s")
        vals.append(alignment_score)

    tier, _ = calculate_company_tier(alignment_score)
    if tier is not None:
        updates.append("tier = %s")
        vals.append(tier)

    vals.append(canonical)
    cur.execute(
        f"UPDATE company SET {', '.join(updates)} WHERE canonical_name = %s",
        vals,
    )
    cur.close()


def load_all_enrichment() -> dict[str, dict]:
    """Load enrichment for all companies. Returns {canonical_name: enrichment_dict}."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT canonical_name, about, mission_fit, alignment_score, enriched_at FROM company"
    )
    result = {}
    for row in cur.fetchall():
        result[row["canonical_name"]] = {
            "about": row["about"] or {},
            "mission_fit": row["mission_fit"] or {},
            "alignment_score": float(row["alignment_score"])
            if row["alignment_score"] is not None
            else None,
            "enriched_at": row["enriched_at"].isoformat() if row["enriched_at"] else None,
        }
    cur.close()
    return result


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def print_reconciliation_report():
    """Post-run summary: candidates, status distribution, errors."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM company WHERE status = 'candidate'")
    candidates = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM company WHERE status = 'active'")
    active = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM vacancy")
    total_vac = cur.fetchone()[0]

    cur.execute("SELECT status, count(*) FROM vacancy GROUP BY status ORDER BY count(*) DESC")
    status_dist = cur.fetchall()

    cur.execute("SELECT count(*) FROM vacancy WHERE llm_score IS NOT NULL")
    scored = cur.fetchone()[0]

    cur.close()

    print(f"\n{'=' * 50}")
    print("  Reconciliation Report")
    print(f"{'=' * 50}")
    print(f"  Companies: {active} active, {candidates} candidates")
    print(f"  Vacancies: {total_vac} total, {scored} scored")
    print("  Status distribution:")
    for status, cnt in status_dist:
        print(f"    {status}: {cnt}")
    print(f"{'=' * 50}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

VALID_STATUSES = {
    "unseen",
    "liked",
    "passed",
    "to_apply",
    "to_research",
    "to_network",
    "skipped",
    "applied",
    "expiring",
    "archived",
}


def validate_db() -> list[str]:
    """Run lightweight integrity checks. Returns list of warning strings."""
    conn = get_conn()
    cur = conn.cursor()
    warnings = []

    # 1. Vacancies with NULL company_id
    cur.execute("SELECT count(*) FROM vacancy WHERE company_id IS NULL")
    n = cur.fetchone()[0]
    if n:
        warnings.append(f"  {n} vacancies with NULL company_id")

    # 2. Invalid status values
    cur.execute("SELECT DISTINCT status FROM vacancy")
    for (s,) in cur.fetchall():
        if s not in VALID_STATUSES:
            warnings.append(f"  Invalid vacancy status: '{s}'")

    # 3. Duplicate dedup_hash
    cur.execute(
        "SELECT dedup_hash, count(*) FROM vacancy WHERE dedup_hash IS NOT NULL GROUP BY dedup_hash HAVING count(*) > 1"
    )
    dupes = cur.fetchall()
    if dupes:
        warnings.append(f"  {len(dupes)} duplicate dedup_hash values")

    # 4. Scored vacancy without summary
    cur.execute("SELECT count(*) FROM vacancy WHERE llm_score IS NOT NULL AND llm_summary IS NULL")
    n = cur.fetchone()[0]
    if n:
        warnings.append(f"  {n} scored vacancies missing llm_summary")

    # 5. Duplicate company canonical_name
    cur.execute(
        "SELECT canonical_name, count(*) FROM company GROUP BY canonical_name HAVING count(*) > 1"
    )
    dupes = cur.fetchall()
    if dupes:
        warnings.append(
            f"  {len(dupes)} duplicate company canonical_names: {[d[0] for d in dupes]}"
        )

    # 6. Descriptions polluted by a cookie/consent banner (second line of
    #    defence behind the merge-time gate). One ILIKE over the main anchors.
    cur.execute("""
        SELECT count(*) FROM vacancy
        WHERE full_description ILIKE '%we use cookies%'
           OR full_description ILIKE '%we value your privacy%'
           OR full_description ILIKE '%accept all cookies%'
           OR full_description ILIKE '%this website uses cookies%'
    """)
    n = cur.fetchone()[0]
    if n:
        warnings.append(
            f"  {n} descriptions match cookie-banner anchors "
            f"(run enrich_blind_vacancies.py --clean-cookie-pages)"
        )

    cur.close()

    if warnings:
        print(f"\n⚠ Validation: {len(warnings)} warning(s)")
        for w in warnings:
            print(w)
    else:
        print("\n✓ Validation: 0 warnings")

    return warnings
