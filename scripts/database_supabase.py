"""Supabase-backed data access layer.

Direct Postgres via psycopg2. Singleton connection, autocommit OFF — callers
commit at logical checkpoints.
"""

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

from dateutil import parser as dateutil_parser

from company_registry import (
    COMPANIES,
    _ALL_KNOWN_NAMES,
    _ALL_KNOWN_NAMES as _ALL_CSV_NAMES,
    resolve_canonical_name,
)
import settings
from config import (
    LLM_SCORE_THRESHOLD,
    PROTECT_SCORE,
    VACANCIES_DIR,
    GEO_BANNED_COUNTRIES,
    GEO_BANNED_REGIONS,
    GEO_KEEP_COUNTRIES_SET,
    GEO_BAN_US_ONLY,
)
from geo import country_banned, is_remote_mode

# Json / RealDictCursor come from db_backend so they work under both the
# Supabase (psycopg2) and the local SQLite backend without importing psycopg2.
from db_backend import IS_SQLITE, Json, RealDictCursor
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
    boilerplate (cookie wall, error page, nav chrome). Returns the reject
    verdict ("cookie_wall"/"error_page"/"nav_junk") when boilerplate was
    dropped, else None. A merely short/empty description is left as-is — the
    snippet/URL fallback in filters.has_enough_content still applies.
    """
    raw = job.get("full_description") or ""
    if not raw.strip():
        return None
    cleaned, verdict = clean_description(raw)
    if verdict in ("cookie_wall", "error_page", "nav_junk"):
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


#: In simple mode (SQLite) the board/ATS auto-discovery path creates companies
#: ACTIVE, not 'candidate' — there is no dashboard review step to approve them,
#: so the candidate gate would blackhole every board company (filter/score/
#: dashboard only count active ones → "Ready to score: 0", empty dashboard). Full
#: mode (Supabase) keeps the candidate→review gate. See save_vacancies / the
#: board ingestion path.
AUTO_DISCOVERED_STATUS = "active" if IS_SQLITE else "candidate"


def ensure_company(org_name: str, status: str = "candidate"):
    """Find or create a company. Returns UUID.

    ``status`` is honoured verbatim — callers that want a candidate get one. The
    board/ATS auto-discovery path passes ``AUTO_DISCOVERED_STATUS`` so it lands
    active in simple mode (see save_vacancies).
    """
    cid = resolve_company_id(org_name)
    if cid is not None:
        return cid

    canonical = resolve_canonical_name(org_name)
    conn = get_conn()
    cur = conn.cursor()
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


def load_vacancies(
    *,
    company_name=None,
    status=None,
    status_exclude=None,
    unscored_only=False,
    limit=None,
    include_inactive_companies=False,
    include_candidate_companies=False,
    light: bool = False,
) -> dict[str, dict]:
    """Load vacancies from Supabase. Returns {uuid_str: vacancy_dict}.

    By default shows only vacancies from active (approved) companies.
    include_inactive_companies=True → no company status filter (all statuses).
    include_candidate_companies=True → also include candidate companies.
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
            conditions.append("c.status != 'inactive'")
        else:
            conditions.append("c.status = 'active'")

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
        conditions.append("v.llm_score IS NULL")
        conditions.append("v.status != 'archived'")

    where = " AND ".join(conditions) if conditions else "TRUE"

    if light:
        vacancy_cols = ", ".join(f"v.{c}" for c in _VACANCY_LIGHT_COLUMNS)
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
        "v.llm_score IS NULL",
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


def save_vacancies(org_name: str, tier, jobs: list[dict]) -> int:
    """Save fetched jobs into the DB. Returns count of new vacancies.

    Same role (org + title) at different locations → one entry with locations[].
    """
    org_name = resolve_canonical_name(org_name)
    company_id = resolve_company_id(org_name)
    if company_id is None:
        company_id = ensure_company(org_name, status=AUTO_DISCOVERED_STATUS)
    today = date.today().isoformat()
    new_count = 0
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Direct ATS path: exclude 'gone_from_source' tombstones so the company's
    # own re-listing resurrects the role. Loaded once (not per row).
    archived_hashes = get_archived_hashes(include_gone=False)

    skipped_archived = 0
    skipped_junk = 0
    skipped_boilerplate = 0
    resurrected = 0

    for job in jobs:
        _strip_nul_bytes(job)
        # Quality gate BEFORE any length comparison: strip cookie banners,
        # blank pure boilerplate so it never overwrites a real description.
        if _gate_description(job):
            skipped_boilerplate += 1
        title = _sanitize_title(job.get("title", ""))
        if filters.title_words_blacklisted(title):
            continue
        if not filters.has_enough_content(job):
            continue
        junk_reason = filters.is_content_junk(job.get("full_description", ""))
        if junk_reason:
            skipped_junk += 1
            continue

        dedup_hash = make_vacancy_id(org_name, title)

        if filters.is_recently_archived(archived_hashes, dedup_hash):
            skipped_archived += 1
            continue

        loc_entry = _make_location_entry(job)
        loc_key = (
            loc_entry.get("city") or loc_entry.get("country") or loc_entry.get("work_mode") or ""
        )

        # Check existing
        cur.execute("SELECT * FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
        existing = cur.fetchone()

        if existing:
            updates = {"last_seen": today}

            # A re-listed role is alive again: resurrect a row we had archived
            # (gone from source) OR protected as 'expiring' back to 'unseen'.
            if existing.get("status") in ("archived", "expiring"):
                updates["status"] = "unseen"
                # Reset the expiring alert so a future expiry re-alerts.
                updates["expiring_alerted_at"] = None
                resurrected += 1

            if job.get("snippet") and not existing.get("snippet"):
                updates["snippet"] = job["snippet"]
            new_desc = job.get("full_description") or ""
            old_desc = existing.get("full_description") or ""
            if new_desc and len(new_desc) > len(old_desc) + 100:
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
            existing_loc_keys = {
                l.get("city") or l.get("country") or l.get("work_mode") or "" for l in locs
            }
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
            # Resolve deadline: fetcher-provided or fallback regex from description
            deadline_raw = job.get("deadline") or ""
            if not deadline_raw:
                deadline_raw = _extract_deadline_from_description(job.get("full_description") or "")
            parsed_deadline = _safe_deadline(deadline_raw) if deadline_raw else None
            cur.execute(
                """INSERT INTO vacancy (
                       dedup_hash, company_id, title, snippet,
                       full_description, compensation, deadline,
                       first_seen, last_seen, locations, department
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    dedup_hash,
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
    if skipped_archived:
        print(f"  [{org_name}] skipped {skipped_archived} recently archived", flush=True)
    if skipped_boilerplate:
        print(
            f"  [{org_name}] gate dropped {skipped_boilerplate} boilerplate descriptions",
            flush=True,
        )
    if skipped_junk:
        print(f"  [{org_name}] skipped {skipped_junk} junk content", flush=True)
    if resurrected:
        print(f"  [{org_name}] resurrected: {resurrected}", flush=True)
    return new_count


def save_board_vacancies(board_cfg: dict, jobs: list[dict]) -> int:
    """Save job board results into the DB. Returns count of new vacancies.

    Unknown orgs → ensure_company(status='candidate'). Skips inactive companies.
    """
    today = date.today().isoformat()
    tier = board_cfg.get("tier", "C")
    board_url = board_cfg["url"]
    board_name = board_cfg.get("name", "")
    new_count = 0
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Board path: full archived set (include_gone=True) so a lagging feed cannot
    # resurrect a posting the source already closed. Loaded once (not per row).
    archived_hashes = get_archived_hashes(include_gone=True)

    seen_ext_ids: set[str] = set()

    skipped_archived = 0
    skipped_junk = 0
    skipped_boilerplate = 0
    resurrected = 0
    skipped_inactive: dict[str, int] = {}

    for job in jobs:
        _strip_nul_bytes(job)
        # Quality gate BEFORE any length comparison: strip cookie banners,
        # blank pure boilerplate so it never overwrites a real description.
        if _gate_description(job):
            skipped_boilerplate += 1
        title = _sanitize_title(job.get("title", ""))
        if filters.title_words_blacklisted(title):
            continue
        if not filters.has_enough_content(job):
            continue
        junk_reason = filters.is_content_junk(job.get("full_description", ""))
        if junk_reason:
            skipped_junk += 1
            continue

        ext_id = job.get("external_id", "")
        if ext_id:
            dedup_key = f"{board_name}|{ext_id}"
            if dedup_key in seen_ext_ids:
                continue
            seen_ext_ids.add(dedup_key)

        raw_org = job.get("org_override") or board_cfg["name"]
        org = resolve_canonical_name(raw_org)

        # Resolve or create company
        company_id = resolve_company_id(org)
        if company_id is None:
            if org not in _ALL_KNOWN_NAMES and not org.startswith("[via "):
                company_id = ensure_company(org, status=AUTO_DISCOVERED_STATUS)
            else:
                company_id = ensure_company(org, status="active")

        # Skip inactive companies (log the loss for visibility)
        cur.execute("SELECT status FROM company WHERE id = %s", (company_id,))
        comp_row = cur.fetchone()
        if comp_row and comp_row["status"] == "inactive":
            skipped_inactive[org] = skipped_inactive.get(org, 0) + 1
            continue

        dedup_hash = make_vacancy_id(org, title)

        if filters.is_recently_archived(archived_hashes, dedup_hash):
            skipped_archived += 1
            continue
        loc_entry = _make_location_entry(job)
        loc_key = (
            loc_entry.get("city") or loc_entry.get("country") or loc_entry.get("work_mode") or ""
        )

        cur.execute("SELECT * FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
        existing = cur.fetchone()

        if existing:
            updates = {"last_seen": today}
            if existing.get("status") in ("archived", "expiring"):
                updates["status"] = "unseen"
                updates["expiring_alerted_at"] = None
                resurrected += 1
            for field in ("snippet", "full_description"):
                if job.get(field) and not existing.get(field):
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
            existing_loc_keys = {
                l.get("city") or l.get("country") or l.get("work_mode") or "" for l in locs
            }
            if loc_key not in existing_loc_keys:
                locs.append(loc_entry)
                updates["locations"] = Json(locs)

            set_parts = [f"{k} = %s" for k in updates]
            vals = list(updates.values()) + [existing["id"]]
            cur.execute(f"UPDATE vacancy SET {', '.join(set_parts)} WHERE id = %s", vals)
        else:
            # Resolve deadline: fetcher-provided or fallback regex from description
            deadline_raw = job.get("deadline") or ""
            if not deadline_raw:
                deadline_raw = _extract_deadline_from_description(job.get("full_description") or "")
            parsed_deadline = _safe_deadline(deadline_raw) if deadline_raw else None
            cur.execute(
                """INSERT INTO vacancy (
                       dedup_hash, company_id, title, snippet,
                       full_description, compensation, deadline,
                       first_seen, last_seen, locations
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    dedup_hash,
                    company_id,
                    title,
                    job.get("snippet", ""),
                    job.get("full_description", ""),
                    job.get("compensation", ""),
                    parsed_deadline,
                    today,
                    today,
                    Json([loc_entry]),
                ),
            )
            new_count += 1

    cur.close()
    if skipped_archived:
        print(f"  [{board_name}] skipped {skipped_archived} recently archived", flush=True)
    if skipped_boilerplate:
        print(
            f"  [{board_name}] gate dropped {skipped_boilerplate} boilerplate descriptions",
            flush=True,
        )
    if skipped_junk:
        print(f"  [{board_name}] skipped {skipped_junk} junk content", flush=True)
    if resurrected:
        print(f"  [{board_name}] resurrected: {resurrected}", flush=True)
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
    """Update LLM score fields for a vacancy."""
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

    cur.execute(
        f"""UPDATE vacancy SET
               llm_score = %s, llm_reasoning = %s, llm_summary = %s,
               llm_hard_requirements = %s, llm_scored_at = now(){dl_clause}{elig_clause}
           WHERE id = %s""",
        (
            score_data.get("llm_score"),
            score_data.get("llm_reasoning"),
            score_data.get("llm_summary"),
            json.dumps(hard_reqs),
            *dl_params,
            *elig_params,
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


def mark_board_fetched(board_id: str):
    """Record now as the last fetch date for a board.

    Inserts a bare row if the board is not yet in the catalog (a fetch can run
    before sync_boards in edge cases); sync_boards backfills the metadata.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO board (id, name, last_fetched)
           VALUES (%s, %s, now())
           ON CONFLICT (id) DO UPDATE SET
               last_fetched = now(), updated_at = now()""",
        (board_id, board_id),
    )
    cur.close()


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


def record_archived_hashes(entries: list[tuple[str, str]]):
    """Bulk-insert archived hashes. entries = [(dedup_hash, reason), ...]."""
    if not entries:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO archived_hash (dedup_hash, reason) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        entries,
    )
    cur.close()


# ---------------------------------------------------------------------------
# Gone-from-source detection
# ---------------------------------------------------------------------------


def archive_gone_vacancies(org_name: str, fetched_jobs: list[dict]) -> int:
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
        return 0
    fetched_hashes = {
        make_vacancy_id(org, _sanitize_title(j.get("title", ""))) for j in fetched_jobs
    }
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id, dedup_hash, title, llm_score FROM vacancy "
        "WHERE company_id = %s AND status = 'unseen'",
        (company_id,),
    )
    gone = [r for r in cur.fetchall() if r["dedup_hash"] not in fetched_hashes]
    if not gone:
        cur.close()
        return 0

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
        record_archived_hashes([(r["dedup_hash"], "gone_from_source") for r in to_archive])
        titles = ", ".join(sorted(r["title"] for r in to_archive)[:3])
        print(f"  [{org}] archived {len(to_archive)} gone from source: {titles}", flush=True)
    if protected:
        ptitles = ", ".join(sorted(r["title"] for r in protected)[:3])
        print(
            f"  [{org}] PROTECTED {len(protected)} high-fit gone from source → expiring: {ptitles}",
            flush=True,
        )
    return len(to_archive)


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


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


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
    # disagree about what was removed. So we (1) build the archive
    # payload in memory, (2) DELETE + record tombstones and COMMIT, then (3)
    # write the JSON — only AFTER the commit succeeds. Ordering rationale:
    #   * JSON-before-DELETE (the old flow) could leave a file claiming a
    #     removal that a later rollback undid — a divergence.
    #   * COMMIT-before-JSON makes the committed DELETE the single source of
    #     truth; the file can only ever describe rows that are genuinely gone.
    # archive_vacancies OWNS its transaction (it does NOT leave the commit to
    # the caller like the other DAL writes) precisely because that disk artifact
    # must be tied to a durable delete. This is the one intentional exception to
    # the "callers commit" rule; see AGENTS.md.
    archive_dir = VACANCIES_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"archived_{datetime.now().strftime('%Y%m%d_%H%M')}.json"

    archived_data = {}
    for r in to_remove:
        uid = str(r["id"])
        vac = dict(r)
        for df in ("first_seen", "last_seen", "deadline"):
            if isinstance(vac.get(df), date):
                vac[df] = vac[df].isoformat()
        for df in ("status_updated_at", "created_at", "updated_at"):
            if isinstance(vac.get(df), datetime):
                vac[df] = vac[df].isoformat()
        vac["id"] = uid
        vac["company_id"] = str(vac["company_id"])
        archived_data[uid] = vac

    # Delete from the DB and record dedup tombstones, then COMMIT — the durable
    # source of truth for what was archived.
    uuids = [r["id"] for r in to_remove]
    cur = conn.cursor()
    cur.execute("DELETE FROM vacancy WHERE id = ANY(%s::uuid[])", (uuids,))

    # Record archived hashes for dedup (prevents re-fetch → re-score → re-archive)
    archive_hashes = [
        (r.get("dedup_hash"), "score_below_threshold") for r in to_remove if r.get("dedup_hash")
    ]
    if archive_hashes:
        cur.executemany(
            "INSERT INTO archived_hash (dedup_hash, reason) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            archive_hashes,
        )
    cur.close()
    conn.commit()

    # Write the on-disk archive ONLY after the DELETE committed, so the file and
    # the database can never disagree about what was removed.
    with open(archive_path, "w", encoding="utf-8") as f:
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
