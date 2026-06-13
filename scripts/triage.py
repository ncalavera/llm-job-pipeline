"""Triage helpers: load liked vacancies, group by company, persist decisions."""

import json
from datetime import date, datetime
from pathlib import Path

from config import (
    COMPANIES, PROJECT_ROOT,
    resolve_canonical_name,
)
from database_supabase import (
    get_conn, load_vacancies, update_vacancy_status,
    batch_update_statuses, update_vacancy_fields,
    load_company_enrichment,
)


TRIAGE_DIR = PROJECT_ROOT / "triage"
TRIAGE_DB_PATH = TRIAGE_DIR / "triage.json"

# Statuses that mean "already triaged" — no need to review again
TRIAGED_STATUSES = {"to_apply", "to_research", "to_network", "skipped", "applied"}


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

def get_liked_vacancies() -> list[dict]:
    """Fetch liked vacancies from Supabase.

    Automatically moves expired vacancies (deadline passed) to 'passed' status
    so they don't reappear in future triage sessions.
    Returns list of non-expired vacancy dicts.
    """
    liked = load_vacancies(status="liked")
    if not liked:
        return []

    today = date.today().isoformat()
    filtered = []
    expired_ids: dict[str, str] = {}
    for uid, v in liked.items():
        dl = v.get("deadline", "")
        if dl and dl < today:
            expired_ids[uid] = "passed"
        else:
            filtered.append(v)

    if expired_ids:
        batch_update_statuses(expired_ids)
        get_conn().commit()
        titles = [liked[uid].get("title", "?") for uid in expired_ids]
        print(f"  {len(expired_ids)} liked vacancies auto-passed (deadline expired):")
        for t in titles:
            print(f"    - {t}")

    return filtered


def update_status(vacancy_id: str, status: str) -> bool:
    """Update a single vacancy status in Supabase.

    Returns True on success, False on failure.
    """
    try:
        update_vacancy_status(vacancy_id, status)
        get_conn().commit()
        return True
    except Exception as e:
        print(f"  Status update failed for {vacancy_id}: {e}")
        return False


def batch_update_statuses_and_commit(updates: dict[str, str]) -> dict[str, bool]:
    """Update multiple vacancy statuses. Returns {id: success} dict."""
    try:
        batch_update_statuses(updates)
        get_conn().commit()
        return {vid: True for vid in updates}
    except Exception as e:
        print(f"  Batch status update failed: {e}")
        return {vid: False for vid in updates}


# ---------------------------------------------------------------------------
# URL auto-discovery
# ---------------------------------------------------------------------------

def find_vacancy_url(v: dict, client=None) -> str | None:
    """Return first existing location URL, or search via Firecrawl if missing."""
    for loc in v.get("locations", []):
        if loc.get("url"):
            return loc["url"]
    if not client:
        return None
    org = v.get("org", "") or v.get("organization", "")
    title = v.get("title", "")
    if not org or not title:
        return None
    try:
        results = client.search(query=f"{org} {title} job", limit=5)
        for item in (results if isinstance(results, list) else getattr(results, "data", results)):
            url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None)
            if url and "://" in url:
                return url
    except Exception:
        pass
    return None


def update_vacancies_in_db(updates: dict[str, dict]) -> int:
    """Write field updates to Supabase.

    updates = {vacancy_uuid: {"url": "..."}}
    Returns number of changed vacancies.
    """
    from psycopg2.extras import Json, RealDictCursor
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    changed = 0
    for vid, fields in updates.items():
        if "url" in fields:
            cur.execute("SELECT locations FROM vacancy WHERE id = %s", (vid,))
            row = cur.fetchone()
            if not row:
                continue
            locs = row["locations"] or []
            if locs and not locs[0].get("url"):
                locs[0]["url"] = fields["url"]
                cur.execute("UPDATE vacancy SET locations = %s WHERE id = %s",
                           (Json(locs), vid))
                changed += 1
    if changed:
        conn.commit()
    cur.close()
    return changed


# ---------------------------------------------------------------------------
# Deadline filtering
# ---------------------------------------------------------------------------

def is_deadline_passed(v: dict) -> bool:
    """True if vacancy deadline is in the past."""
    deadline = v.get("deadline", "")
    if not deadline:
        return False
    try:
        d = date.fromisoformat(deadline)
        return d < date.today()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Grouping & context
# ---------------------------------------------------------------------------

def group_by_company(vacancies: list[dict]) -> list[dict]:
    """Group vacancies by canonical org name, sorted by max llm_score DESC.

    Returns list of dicts: {org, tier, vacancies: [...], max_score}.
    """
    groups: dict[str, dict] = {}
    for v in vacancies:
        org = resolve_canonical_name(v.get("org") or v.get("organization", ""))
        if org not in groups:
            cfg = COMPANIES.get(org, {})
            groups[org] = {
                "org": org,
                "tier": cfg.get("tier") or v.get("tier") or v.get("company_tier") or None,
                "vacancies": [],
                "max_score": 0,
            }
        groups[org]["vacancies"].append(v)
        score = v.get("llm_score") or 0
        if score > groups[org]["max_score"]:
            groups[org]["max_score"] = score

    result = list(groups.values())
    # Sort companies: highest max_score first
    result.sort(key=lambda g: -g["max_score"])
    # Sort vacancies within each company: highest score first
    for g in result:
        g["vacancies"].sort(key=lambda v: -(v.get("llm_score") or 0))
    return result


def get_company_context(org: str) -> dict:
    """Load enrichment data for a company from Supabase.

    Returns dict with about, mission_fit sub-dicts or empty dict.
    """
    return load_company_enrichment(org)


# ---------------------------------------------------------------------------
# triage.json persistence
# ---------------------------------------------------------------------------

def load_triage_db() -> dict:
    """Load triage database. Creates empty structure if missing."""
    if TRIAGE_DB_PATH.exists():
        with open(TRIAGE_DB_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"sessions": []}


def save_triage_db(triage_db: dict):
    """Save triage database."""
    TRIAGE_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRIAGE_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(triage_db, f, indent=2, ensure_ascii=False)


def next_session_id(triage_db: dict) -> int:
    """Get next session ID (max existing + 1, or 1)."""
    if not triage_db.get("sessions"):
        return 1
    return max(s["session_id"] for s in triage_db["sessions"]) + 1


def create_session(triage_db: dict) -> dict:
    """Create a new triage session record."""
    session = {
        "session_id": next_session_id(triage_db),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "reviewed_count": 0,
        "verdicts": {"apply": 0, "skip": 0, "research": 0, "network": 0},
        "reviews": [],
        "company_notes": {},
        "calibration": {
            "scoring_adjustments": [],
            "blacklist_additions": [],
            "new_sources": [],
            "strategy_insights": [],
            "applied": False,
        },
    }
    triage_db["sessions"].append(session)
    return session


def add_review(session: dict, review: dict):
    """Add a vacancy review to the session."""
    session["reviews"].append(review)
    session["reviewed_count"] = len(session["reviews"])
    verdict = review.get("verdict", "")
    if verdict in session["verdicts"]:
        session["verdicts"][verdict] += 1


# ---------------------------------------------------------------------------
# Vacancy formatting for CLI briefing
# ---------------------------------------------------------------------------

def format_vacancy_briefing(v: dict, enrichment: dict) -> str:
    """Format a vacancy for CLI display during triage interview."""
    lines = []
    lines.append(f"  Position: {v.get('title', '?')}")

    score = v.get("llm_score")
    if score is not None:
        lines.append(f"  LLM Score: {score}")

    # Locations
    locs = v.get("locations", [])
    if locs:
        loc_strs = [l.get("location", "?") for l in locs]
        lines.append(f"  Locations: {', '.join(loc_strs)}")

    # Compensation
    comp = v.get("compensation", "")
    if not comp and locs:
        comp = next((l.get("compensation", "") for l in locs if l.get("compensation")), "")
    if comp:
        lines.append(f"  Compensation: {comp}")

    # Deadline
    if v.get("deadline"):
        lines.append(f"  Deadline: {v['deadline']}")

    # URL (first found in locations)
    url = find_vacancy_url(v)
    if url:
        lines.append(f"  URL: {url}")

    # LLM summary
    if v.get("llm_summary"):
        lines.append(f"\n  Summary: {v['llm_summary']}")

    # LLM tags
    if v.get("llm_tags"):
        lines.append(f"  Tags: {', '.join(v['llm_tags'])}")

    # LLM reasoning (gap analysis) — support both field name variants
    reasoning = v.get("llm_reasoning") or v.get("llm_rationale", "")
    if reasoning:
        lines.append(f"\n  Analysis: {reasoning}")

    return "\n".join(lines)


def format_company_briefing(org: str, tier, enrichment: dict) -> str:
    """Format company context for CLI display."""
    tier_display = tier if tier in ("S", "A", "B", "C") else "?"
    lines = [f"  Company: {org} (Tier {tier_display})"]

    about = enrichment.get("about", {})
    mission = enrichment.get("mission_fit", {})

    if about.get("description"):
        desc = about["description"][:200]
        lines.append(f"  About: {desc}")

    if about.get("hq_location"):
        lines.append(f"  HQ: {about['hq_location']}")

    if about.get("employee_count"):
        lines.append(f"  Employees: {about['employee_count']}")

    if mission.get("alignment_score") is not None:
        lines.append(f"  Alignment: {mission['alignment_score']}/100 ({mission.get('alignment_label', '')})")

    if mission.get("mission_verdict"):
        lines.append(f"  Verdict: {mission['mission_verdict']}")

    if mission.get("strengths"):
        lines.append(f"  Strengths: {'; '.join(mission['strengths'][:3])}")

    if mission.get("risks"):
        lines.append(f"  Risks: {'; '.join(mission['risks'][:2])}")

    return "\n".join(lines)
