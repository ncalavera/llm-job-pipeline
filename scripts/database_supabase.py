"""Supabase-backed data access layer for job-search-2026.

Replaces database.py (JSON/Redis) with direct Postgres via psycopg2.
Singleton connection, autocommit OFF — callers commit at logical checkpoints.
"""

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

import psycopg2  # noqa: F401 — used by callers via db_conn
from dateutil import parser as dateutil_parser
from psycopg2.extras import Json, RealDictCursor

from company_registry import (
    COMPANIES,
    _ALL_KNOWN_NAMES as _ALL_CSV_NAMES,
    resolve_canonical_name,
)
from config import (
    GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR, GLOBAL_BLACKLIST_DESC_SUBSTR,
    LLM_SCORE_THRESHOLD,
    REGION_EUROPE, REGION_REMOTE, REGION_US,
    VACANCIES_DIR,
)
from db_conn import get_conn, close_conn
from quality import clean_description

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Fetch status constants
# ---------------------------------------------------------------------------

FETCH_STATUS_OK = "ok"
FETCH_STATUS_NO_DATA = "no_data"


def is_fetch_error(status: str | None) -> bool:
    """Check if fetch_status represents an error (not ok/no_data/empty)."""
    return bool(status) and status not in (FETCH_STATUS_OK, FETCH_STATUS_NO_DATA)


# ---------------------------------------------------------------------------
# Pure functions (no DB)
# ---------------------------------------------------------------------------

# City→country mapping for location parsing
_CITY_COUNTRY = {
    "berlin": "Germany", "munich": "Germany", "hamburg": "Germany",
    "london": "United Kingdom", "manchester": "United Kingdom",
    "paris": "France", "amsterdam": "Netherlands",
    "barcelona": "Spain", "madrid": "Spain",
    "lisbon": "Portugal", "porto": "Portugal",
    "rome": "Italy", "milan": "Italy",
    "stockholm": "Sweden", "copenhagen": "Denmark",
    "oslo": "Norway", "helsinki": "Finland",
    "dublin": "Ireland", "vienna": "Austria",
    "zurich": "Switzerland", "geneva": "Switzerland",
    "brussels": "Belgium", "warsaw": "Poland",
    "prague": "Czech Republic", "budapest": "Hungary",
    "tallinn": "Estonia", "riga": "Latvia", "vilnius": "Lithuania",
    "new york": "United States", "san francisco": "United States",
    "seattle": "United States", "austin": "United States",
    "boston": "United States", "chicago": "United States",
    "toronto": "Canada", "vancouver": "Canada",
    "sydney": "Australia", "melbourne": "Australia",
    "singapore": "Singapore", "tokyo": "Japan",
    "nairobi": "Kenya", "cape town": "South Africa",
    "tel aviv": "Israel", "dubai": "UAE",
}

_REMOTE_KW = {"remote", "anywhere", "worldwide", "global", "distributed",
              "work from home", "wfh", "fully remote", "home-based"}
_HYBRID_KW = {"hybrid", "flexible"}


def classify_region(location: str) -> str | None:
    """Classify location string into region."""
    loc = location.lower()
    if any(kw in loc for kw in REGION_EUROPE):
        return "europe"
    if any(kw in loc for kw in REGION_US):
        return "americas"
    for city, country in _CITY_COUNTRY.items():
        if city in loc:
            c = country.lower()
            if any(kw in c for kw in REGION_EUROPE):
                return "europe"
            if any(kw in c for kw in REGION_US):
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


# Pre-compiled blacklist: single alternation regex, sorted by length desc to prevent
# shorter substrings matching prematurely. Compiled once at module load (~10-50x faster
# than iterating 400+ individual re.search() calls per vacancy).
_BLACKLIST_PATTERN = re.compile(
    r'\b(?:' + '|'.join(
        re.escape(kw) for kw in sorted(GLOBAL_BLACKLIST, key=len, reverse=True)
    ) + r')\b',
    re.IGNORECASE
)

def _is_blacklisted(title: str, description: str = "") -> bool:
    t = title.lower()
    if any(kw in t for kw in GLOBAL_BLACKLIST_SUBSTR):
        return True
    if _BLACKLIST_PATTERN.search(t):
        return True
    # Description-level kill phrases — narrow, conservative list (visa/citizenship
    # only). See feedback memory: full GLOBAL_BLACKLIST on descriptions causes FPs
    # ("developer" at JetBrains, "ai safety" at Anthropic). Issue #232.
    if description:
        d = description.lower()
        if any(kw in d for kw in GLOBAL_BLACKLIST_DESC_SUBSTR):
            return True
    return False


def _is_content_junk(description: str) -> str | None:
    """Detect non-vacancy content. Returns reason string or None."""
    if not description:
        return None
    d = description[:500].lower()
    if 'recaptcha' in d and len(description) < 300:
        return "recaptcha_only"
    if any(p in d for p in ['every.org', 'donate to a fund', 'make a donation']):
        return "donation_widget"
    if any(p in d for p in ['404 not found', 'page not found', 'error 404',
                             'access denied', 'cannot be displayed']):
        return "error_page"
    if len(description.strip()) < 50:
        return "navigation_snippet"
    return None


def _gate_description(job: dict) -> str | None:
    """Run job['full_description'] through the quality gate, in place.

    Strips a leading cookie banner; blanks the field when the text is pure
    boilerplate (cookie wall, error page, nav chrome). Returns the reject
    verdict ("cookie_wall"/"error_page"/"nav_junk") when boilerplate was
    dropped, else None. A merely short/empty description is left as-is — the
    existing snippet/URL fallback in _has_enough_content still applies.
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


ARCHIVE_TTL_DAYS = 90


def _is_recently_archived(cur, dedup_hash: str, include_gone: bool = True) -> bool:
    """Check if dedup_hash was archived within TTL cooldown.

    include_gone=False ignores 'gone_from_source' records: the company's own
    ATS re-listing a role is ground truth that it reopened, while lagging job
    boards (include_gone=True) must not resurrect a posting the source closed.
    """
    query = ("SELECT 1 FROM archived_hash WHERE dedup_hash = %s "
             "AND archived_at > now() - interval '90 days'")
    if not include_gone:
        query += " AND reason IS DISTINCT FROM 'gone_from_source'"
    cur.execute(query, (dedup_hash,))
    return cur.fetchone() is not None


def _sanitize_title(title: str) -> str:
    """Strip markdown/HTML artifacts from scraped titles.

    Applied before make_vacancy_id() so dedup_hash is clean.
    """
    import html as _html_mod
    title = _html_mod.unescape(title)                          # &amp; → &, &nbsp; → space
    title = re.sub(r'\*\*', '', title)                          # markdown bold
    title = re.sub(r'\s*!\[.*$', '', title)                     # markdown image refs
    title = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', title)      # [text](url) → text
    title = re.sub(r'<(?:br|strong|em|b|i|span)[^>]*>', '', title, flags=re.IGNORECASE)  # light HTML tags
    title = re.sub(r'</(?:strong|em|b|i|span)>', '', title, flags=re.IGNORECASE)
    title = re.sub(r'  +', ' ', title)                          # collapse double spaces
    return title.strip()


def _has_enough_content(job: dict, min_chars: int = 50) -> bool:
    desc = job.get("full_description", "") or ""
    snip = job.get("snippet", "") or ""
    if len(desc.strip()) >= min_chars or len(snip.strip()) >= min_chars:
        return True
    if job.get("url", "").strip():
        return True
    return False


def make_vacancy_id(org: str, title: str, location: str = "") -> str:
    key = f"{org}|{title}".lower()
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
    nums = re.findall(r'[\d,]+', comp_str)
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


def ensure_company(org_name: str, status: str = "candidate"):
    """Find or create a company. Returns UUID."""
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
# See docs/plans/2026-04-22-001-refactor-dal-egress-narrowing-plan.md (issue #225).
_VACANCY_LIGHT_COLUMNS = (
    "id", "created_at", "updated_at",
    "dedup_hash", "company_id", "title", "snippet",
    "compensation", "deadline", "locations", "department",
    "llm_score", "llm_summary", "llm_reasoning",
    "llm_hard_requirements", "llm_scored_at",
    "status", "status_updated_at",
    "first_seen", "last_seen", "triage",
)


def load_vacancies(*, company_name=None, status=None, status_exclude=None,
                   unscored_only=False, limit=None,
                   include_inactive_companies=False,
                   include_candidate_companies=False,
                   light: bool = False) -> dict[str, dict]:
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
# companies the owner hasn't reviewed yet.
CANDIDATE_SCORE_LIMIT = 15
# A candidate company's vacancy is worth scoring only if the company shows
# some promise: alignment ≥ this floor, or not yet enriched (NULL).
CANDIDATE_ALIGNMENT_FLOOR = 30


def load_candidate_vacancies_for_scoring(
    *, limit: int = CANDIDATE_SCORE_LIMIT,
    status_exclude=None,
) -> dict[str, dict]:
    """Load unscored vacancies from *candidate* companies that look promising.

    Rescues the "strong vacancy at a forgotten company" case: a candidate
    company the owner never reviewed would otherwise be invisible to scoring
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
        fields_to_update = {k: v for k, v in data.items()
                           if k not in ("id", "dedup_hash", "created_at")}
        if fields_to_update:
            set_clauses = []
            vals = []
            for k, v in fields_to_update.items():
                set_clauses.append(f"{k} = %s")
                vals.append(Json(v) if isinstance(v, (dict, list)) and k in ("locations", "triage") else v)
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
            Json(data[k]) if isinstance(data[k], (dict, list)) and k in ("locations", "triage") else data[k]
            for k in data if k not in ("id", "dedup_hash")
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
    r'(?:[Dd]eadline|[Cc]losing\s+date|[Aa]pply\s+by|[Aa]pplications?\s+close)'
    r'[:\s]+'
    r'(\d{4}-\d{2}-\d{2}|[A-Za-z0-9,\s]+\d{4})'
)


def _extract_deadline_from_description(html: str) -> str:
    """Extract application deadline from HTML description text.

    Self-contained: strips HTML tags inline (no import from fetchers).
    Returns raw date string for _safe_deadline() to parse, or empty string.
    """
    if not html:
        return ""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r'\s+', ' ', text).strip()
    m = _DEADLINE_RE.search(text)
    return m.group(1).strip() if m else ""


def _strip_nul_bytes(job: dict) -> None:
    """Postgres TEXT can't contain 0x00. Strip from all string values in place."""
    for k, v in list(job.items()):
        if isinstance(v, str) and "\x00" in v:
            job[k] = v.replace("\x00", "")


def merge_vacancies(org_name: str, tier, jobs: list[dict]) -> int:
    """Merge fetched jobs into Supabase. Returns count of new vacancies.

    Same role (org + title) at different locations → one entry with locations[].
    """
    org_name = resolve_canonical_name(org_name)
    company_id = resolve_company_id(org_name)
    if company_id is None:
        company_id = ensure_company(org_name, status="candidate")
    today = date.today().isoformat()
    new_count = 0
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

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
        if _is_blacklisted(title):
            continue
        if not _has_enough_content(job):
            continue
        junk_reason = _is_content_junk(job.get("full_description", ""))
        if junk_reason:
            skipped_junk += 1
            continue

        dedup_hash = make_vacancy_id(org_name, title)

        if _is_recently_archived(cur, dedup_hash, include_gone=False):
            skipped_archived += 1
            continue

        loc_entry = _make_location_entry(job)
        loc_key = (loc_entry.get("city") or loc_entry.get("country")
                   or loc_entry.get("work_mode") or "")

        # Check existing
        cur.execute("SELECT * FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
        existing = cur.fetchone()

        if existing:
            updates = {"last_seen": today}

            if existing.get("status") == "archived":
                updates["status"] = "unseen"
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
                extracted = _extract_deadline_from_description(
                    job.get("full_description") or "")
                if extracted:
                    parsed_dl = _safe_deadline(extracted)
                    if parsed_dl:
                        updates["deadline"] = parsed_dl
            if job.get("department") and not existing.get("department"):
                updates["department"] = job["department"]

            # Merge locations
            locs = existing.get("locations") or []
            existing_loc_keys = {
                l.get("city") or l.get("country") or l.get("work_mode") or ""
                for l in locs
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
                deadline_raw = _extract_deadline_from_description(
                    job.get("full_description") or "")
            parsed_deadline = _safe_deadline(deadline_raw) if deadline_raw else None
            cur.execute(
                """INSERT INTO vacancy (
                       dedup_hash, company_id, title, snippet,
                       full_description, compensation, deadline,
                       first_seen, last_seen, locations, department
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    dedup_hash, company_id, title,
                    job.get("snippet", ""),
                    job.get("full_description", ""),
                    job.get("compensation", ""),
                    parsed_deadline,
                    today, today,
                    Json([loc_entry]),
                    job.get("department"),
                ),
            )
            new_count += 1

    cur.close()
    if skipped_archived:
        print(f"  [{org_name}] skipped {skipped_archived} recently archived", flush=True)
    if skipped_boilerplate:
        print(f"  [{org_name}] gate dropped {skipped_boilerplate} boilerplate descriptions", flush=True)
    if skipped_junk:
        print(f"  [{org_name}] skipped {skipped_junk} junk content", flush=True)
    if resurrected:
        print(f"  [{org_name}] resurrected: {resurrected}", flush=True)
    return new_count


def merge_board_vacancies(board_cfg: dict, jobs: list[dict]) -> int:
    """Merge job board results into Supabase. Returns count of new vacancies.

    Unknown orgs → ensure_company(status='candidate'). Skips inactive companies.
    """
    today = date.today().isoformat()
    tier = board_cfg.get("tier", "C")
    board_url = board_cfg["url"]
    board_name = board_cfg.get("name", "")
    new_count = 0
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

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
        if _is_blacklisted(title):
            continue
        if not _has_enough_content(job):
            continue
        junk_reason = _is_content_junk(job.get("full_description", ""))
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
            if org not in _ALL_CSV_NAMES and not org.startswith("[via "):
                company_id = ensure_company(org, status="candidate")
            else:
                company_id = ensure_company(org, status="active")

        # Skip inactive companies (log the loss for visibility)
        cur.execute("SELECT status FROM company WHERE id = %s", (company_id,))
        comp_row = cur.fetchone()
        if comp_row and comp_row["status"] == "inactive":
            skipped_inactive[org] = skipped_inactive.get(org, 0) + 1
            continue

        dedup_hash = make_vacancy_id(org, title)

        if _is_recently_archived(cur, dedup_hash):
            skipped_archived += 1
            continue
        loc_entry = _make_location_entry(job)
        loc_key = (loc_entry.get("city") or loc_entry.get("country")
                   or loc_entry.get("work_mode") or "")

        cur.execute("SELECT * FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
        existing = cur.fetchone()

        if existing:
            updates = {"last_seen": today}
            if existing.get("status") == "archived":
                updates["status"] = "unseen"
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
                extracted = _extract_deadline_from_description(
                    job.get("full_description") or "")
                if extracted:
                    parsed_dl = _safe_deadline(extracted)
                    if parsed_dl:
                        updates["deadline"] = parsed_dl

            locs = existing.get("locations") or []
            existing_loc_keys = {
                l.get("city") or l.get("country") or l.get("work_mode") or ""
                for l in locs
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
                deadline_raw = _extract_deadline_from_description(
                    job.get("full_description") or "")
            parsed_deadline = _safe_deadline(deadline_raw) if deadline_raw else None
            cur.execute(
                """INSERT INTO vacancy (
                       dedup_hash, company_id, title, snippet,
                       full_description, compensation, deadline,
                       first_seen, last_seen, locations
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    dedup_hash, company_id, title,
                    job.get("snippet", ""),
                    job.get("full_description", ""),
                    job.get("compensation", ""),
                    parsed_deadline,
                    today, today,
                    Json([loc_entry]),
                ),
            )
            new_count += 1

    cur.close()
    if skipped_archived:
        print(f"  [{board_name}] skipped {skipped_archived} recently archived", flush=True)
    if skipped_boilerplate:
        print(f"  [{board_name}] gate dropped {skipped_boilerplate} boilerplate descriptions", flush=True)
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

def auto_review_candidates(approve_threshold=60, reject_threshold=25) -> dict:
    """Auto-approve/reject candidate companies by alignment_score threshold.

    Companies with alignment_score >= approve_threshold → active.
    Companies with alignment_score <= reject_threshold → inactive.
    Grey zone (between thresholds) → stays candidate for manual review.

    Returns summary: {"approved": [...], "rejected": [...], "pending": [...]}.
    """
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
    conn.commit()
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

    cur.execute(
        f"""UPDATE vacancy SET
               llm_score = %s, llm_reasoning = %s, llm_summary = %s,
               llm_hard_requirements = %s, llm_scored_at = now(){dl_clause}
           WHERE id = %s""",
        (
            score_data.get("llm_score"),
            score_data.get("llm_reasoning"),
            score_data.get("llm_summary"),
            json.dumps(hard_reqs),
            *dl_params,
            vacancy_uuid,
        ),
    )
    rowcount = cur.rowcount
    cur.close()
    return rowcount


# ---------------------------------------------------------------------------
# Source tracking
# ---------------------------------------------------------------------------

def update_source_tracking(org_name: str, tier, strategy: str,
                           new_count: int, fetch_status: str = FETCH_STATUS_OK):
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


def should_fetch_board(board_id: str, ttl_days: int) -> bool:
    """Return True if board has not been scraped within ttl_days."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT last_fetched FROM company WHERE canonical_name = %s",
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
    """Record now as the last fetch date for a board."""
    conn = get_conn()
    cur = conn.cursor()
    # Ensure the board company row exists
    cur.execute("SELECT id FROM company WHERE canonical_name = %s", (board_id,))
    if not cur.fetchone():
        cur.execute(
            """INSERT INTO company (canonical_name, status, aliases)
               VALUES (%s, 'active', ARRAY[%s]::text[])
               ON CONFLICT (canonical_name) DO NOTHING""",
            (board_id, board_id),
        )
    cur.execute(
        "UPDATE company SET last_fetched = now() WHERE canonical_name = %s",
        (board_id,),
    )
    cur.close()


# ---------------------------------------------------------------------------
# Archive hash dedup
# ---------------------------------------------------------------------------

def get_archived_hashes(ttl_days: int = ARCHIVE_TTL_DAYS) -> set[str]:
    """Return dedup_hashes archived within TTL. Used by filter_vacancies for reporting."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT dedup_hash FROM archived_hash WHERE archived_at > now() - interval '%s days'",
        (ttl_days,),
    )
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
    while a direct re-listing still resurrects it (see _is_recently_archived).

    Caller must guarantee the fetch succeeded and the strategy returns the
    company's complete listing — an empty list from a partial/failed fetch
    would mass-archive live vacancies.
    """
    org = resolve_canonical_name(org_name)
    company_id = resolve_company_id(org)
    if company_id is None:
        return 0
    fetched_hashes = {
        make_vacancy_id(org, _sanitize_title(j.get("title", "")))
        for j in fetched_jobs
    }
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id, dedup_hash, title FROM vacancy "
        "WHERE company_id = %s AND status = 'unseen'",
        (company_id,),
    )
    gone = [r for r in cur.fetchall() if r["dedup_hash"] not in fetched_hashes]
    if not gone:
        cur.close()
        return 0
    cur.execute(
        "UPDATE vacancy SET status = 'archived', status_updated_at = NOW() "
        "WHERE id = ANY(%s::uuid[])",
        ([str(r["id"]) for r in gone],),
    )
    cur.close()
    record_archived_hashes([(r["dedup_hash"], "gone_from_source") for r in gone])
    titles = ", ".join(sorted(r["title"] for r in gone)[:3])
    print(f"  [{org}] archived {len(gone)} gone from source: {titles}", flush=True)
    return len(gone)


# ---------------------------------------------------------------------------
# Auto-pass expired
# ---------------------------------------------------------------------------

def pass_expired_vacancies() -> int:
    """Mark expired unseen vacancies as passed. Returns count."""
    conn = get_conn()
    cur = conn.cursor()
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
        print("  Score-threshold archival paused (pure-fit scoring); thresholds "
              "need recalibration. Pass force=True to override.")
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

    # Write local archive first
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

    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump({
            "archived_at": datetime.now().isoformat(),
            "threshold": threshold,
            "count": len(archived_data),
            "vacancies": archived_data,
        }, f, indent=2, ensure_ascii=False, default=str)

    # Delete from Supabase
    uuids = [r["id"] for r in to_remove]
    cur = conn.cursor()
    cur.execute("DELETE FROM vacancy WHERE id = ANY(%s::uuid[])", (uuids,))

    # Record archived hashes for dedup (prevents re-fetch → re-score → re-archive)
    archive_hashes = [
        (r.get("dedup_hash"), "score_below_threshold")
        for r in to_remove if r.get("dedup_hash")
    ]
    if archive_hashes:
        cur.executemany(
            "INSERT INTO archived_hash (dedup_hash, reason) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            archive_hashes,
        )
    cur.close()

    print(f"  Archived {len(to_remove)} vacancies below LLM score {threshold}"
          f"\n  Archive: {archive_path.name}")
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
        "alignment_score": float(row["alignment_score"]) if row["alignment_score"] is not None else None,
        "enriched_at": row["enriched_at"].isoformat() if row["enriched_at"] else None,
    }


def calculate_company_tier(alignment_score, mpa_prestige):
    """Compute tier (S/A/B/C) + composite from strategic signals.

    Mirrors scripts/report/data_prep.py:calculate_company_tier — same formula
    so frontend display and DB `company.tier` column stay in sync.

    Returns (tier_letter, composite_score) — both None if no data.
    """
    has_align = alignment_score is not None
    has_mpa = mpa_prestige is not None
    if not has_align and not has_mpa:
        return None, None
    if has_align and has_mpa:
        composite = 0.70 * float(alignment_score) + 0.30 * float(mpa_prestige)
    elif has_align:
        composite = float(alignment_score) * 0.85
    else:
        composite = float(mpa_prestige) * 0.70
    composite = round(composite, 1)
    if composite >= 65:
        tier = "S"
    elif composite >= 50:
        tier = "A"
    elif composite >= 35:
        tier = "B"
    else:
        tier = "C"
    return tier, composite


def save_company_enrichment(org_name: str, about=None, mission_fit=None,
                            alignment_score=None):
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

    mpa = None
    if isinstance(mission_fit, dict):
        mpa = mission_fit.get("mpa_narrative_boost")
    tier, _ = calculate_company_tier(alignment_score, mpa)
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
            "alignment_score": float(row["alignment_score"]) if row["alignment_score"] is not None else None,
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

    print(f"\n{'='*50}")
    print(f"  Reconciliation Report")
    print(f"{'='*50}")
    print(f"  Companies: {active} active, {candidates} candidates")
    print(f"  Vacancies: {total_vac} total, {scored} scored")
    print(f"  Status distribution:")
    for status, cnt in status_dist:
        print(f"    {status}: {cnt}")
    print(f"{'='*50}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

VALID_STATUSES = {"unseen", "liked", "passed", "to_apply", "to_research", "to_network", "skipped", "applied", "archived"}


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
    cur.execute("SELECT dedup_hash, count(*) FROM vacancy WHERE dedup_hash IS NOT NULL GROUP BY dedup_hash HAVING count(*) > 1")
    dupes = cur.fetchall()
    if dupes:
        warnings.append(f"  {len(dupes)} duplicate dedup_hash values")

    # 4. Scored vacancy without summary
    cur.execute("SELECT count(*) FROM vacancy WHERE llm_score IS NOT NULL AND llm_summary IS NULL")
    n = cur.fetchone()[0]
    if n:
        warnings.append(f"  {n} scored vacancies missing llm_summary")

    # 5. Duplicate company canonical_name
    cur.execute("SELECT canonical_name, count(*) FROM company GROUP BY canonical_name HAVING count(*) > 1")
    dupes = cur.fetchall()
    if dupes:
        warnings.append(f"  {len(dupes)} duplicate company canonical_names: {[d[0] for d in dupes]}")

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
        warnings.append(f"  {n} descriptions match cookie-banner anchors "
                        f"(run enrich_blind_vacancies.py --clean-cookie-pages)")

    cur.close()

    if warnings:
        print(f"\n⚠ Validation: {len(warnings)} warning(s)")
        for w in warnings:
            print(w)
    else:
        print("\n✓ Validation: 0 warnings")

    return warnings
