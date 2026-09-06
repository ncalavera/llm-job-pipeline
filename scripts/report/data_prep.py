"""Data preparation for vacancy dashboard: grouping and stats."""

import json
import re
from datetime import date, datetime

from config import (
    COMPANIES,
    PROJECT_ROOT,
    APPLYABLE_SCORE,
    CATALOG_MIN_SCORE,
    DASHBOARD_TZ,
    resolve_canonical_name,
)
from company_registry import PARSING_ARTIFACTS

#: Statuses that mean "this role is live work" — the user picked it up, or an
#: application is running, or it closed with the employer's own answer. These
#: survive the dashboard score floor at any score. Everything else (unseen below
#: the floor, passed, skipped) is either noise or history.
_ACTIVE_STATUSES = frozenset(
    {"liked", "to_apply", "to_research", "to_network", "applied", "interview", "declined"}
)
from database_supabase import load_vacancies, load_all_enrichment
from db_conn import get_conn

# Per-company vacancy NUMBERS (vacancy_count, applyable_count, avg score and the
# hot-vacancy signal) are no longer baked here — they derive in the browser from
# the shipped roles (KISS derivation, phase 2; public/modules/derive.js
# `companyRollup`). The payload ships only the raw role-id list per company; the
# browser owns the math.


# ---------------------------------------------------------------------------
# Latency / SLA health metrics (U11)
# ---------------------------------------------------------------------------

# Active-but-undecided statuses an SLA-tracked role can stall in.
_SLA_ACTIVE_STATUSES = (
    "unseen",
    "liked",
    "expiring",
    "to_research",
    "to_network",
    "to_apply",
)


def _as_date(val):
    """Best-effort parse a date/timestamp (Postgres datetime or SQLite text)."""
    from datetime import datetime as _dt

    if not val:
        return None
    if isinstance(val, _dt):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val)[:10])
    except (ValueError, TypeError):
        return None


def compute_latency_metrics(conn=None) -> dict:
    """Decision-SLA health, computed at snapshot time (KTD1 / U11).

    - **stuck**: roles scoring >= SLA_SCORE in an active-but-undecided status
      that haven't moved in > SLA_DAYS (age from status_updated_at, falling back
      to first_seen so a never-touched unseen role still counts).
    - **leakage_count**: roles scoring >= SLA_SCORE that landed in archived/passed
      within the last SLA-week. Post-U2 the system no longer silently archives or
      passes protected roles, so this is a watchdog that should trend to ~0; it
      cannot perfectly separate a user's deliberate pass from a system leak, so
      treat a spike — not the baseline — as the signal.
    """
    from config import SLA_SCORE, SLA_DAYS
    from db_backend import RealDictCursor

    conn = conn or get_conn()
    today = datetime.now(DASHBOARD_TZ).date()

    cur = conn.cursor(cursor_factory=RealDictCursor)
    placeholders = ", ".join(["%s"] * len(_SLA_ACTIVE_STATUSES))
    cur.execute(
        "SELECT v.id, v.title, c.canonical_name AS org, v.llm_score, v.status, "
        "       v.status_updated_at, v.first_seen "
        "FROM vacancy v JOIN company c ON v.company_id = c.id "
        f"WHERE v.llm_score >= %s AND v.status IN ({placeholders})",
        (SLA_SCORE, *_SLA_ACTIVE_STATUSES),
    )
    stuck = []
    for r in cur.fetchall():
        ref = _as_date(r["status_updated_at"]) or _as_date(r["first_seen"])
        if ref is None:
            continue
        age = (today - ref).days
        if age >= SLA_DAYS:
            stuck.append(
                {
                    "id": str(r["id"]),
                    "org": r["org"],
                    "title": r["title"],
                    "llm_score": r["llm_score"],
                    "status": r["status"],
                    "days_stuck": age,
                }
            )
    stuck.sort(key=lambda s: s["days_stuck"], reverse=True)

    cur.execute(
        "SELECT v.status_updated_at FROM vacancy v "
        "WHERE v.llm_score >= %s AND v.status IN ('archived', 'passed')",
        (SLA_SCORE,),
    )
    leakage = 0
    for r in cur.fetchall():
        d = _as_date(r["status_updated_at"])
        if d is not None and (today - d).days <= SLA_DAYS:
            leakage += 1
    cur.close()

    return {
        "sla_score": SLA_SCORE,
        "sla_days": SLA_DAYS,
        "stuck": stuck,
        "stuck_count": len(stuck),
        "leakage_count": leakage,
    }


def _count_screening_processing() -> dict:
    """Tonight's screening cohort that is not on the board yet: rows with no
    ``screening_state`` and rows whose preparation ``failed``.

    Same run-level exception as ``unscored_count``: these rows are NOT shipped in
    ``groups`` (below the floor, unprepared), so the browser cannot derive the
    count. The cohort predicate is prepare_screening's own (``load_pool`` +
    ``eligible``) plus the ``[screening] window_days`` first_seen cut, so the
    processing line and the night agree on who is "in tonight's cohort".
    """
    from datetime import timedelta

    from database_supabase import _vacancy_has_column
    import prepare_screening
    import settings

    # Migration-only columns (0027): a pre-migration install has no cohort.
    if not _vacancy_has_column("screening_state"):
        return {"unprepared": 0, "failed": 0}
    window_days = int(settings.screening().get("window_days", 14))
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    states = [
        r.get("screening_state")
        for r in prepare_screening.load_pool()
        if prepare_screening.eligible(r) and str(r.get("first_seen") or "")[:10] >= cutoff
    ]
    return {"unprepared": states.count(None), "failed": states.count("failed")}


def _count_unscored(all_vacs: dict) -> int:
    """Count vacancies that are genuinely awaiting scoring.

    Only still-untriaged (status "unseen") rows that lack a score count — so
    already-passed/skipped/liked vacancies (which a user has already acted on
    and will never be re-scored) don't inflate the dashboard's
    "N fetched, none scored yet" hint.
    """
    return sum(
        1
        for v in all_vacs.values()
        if (v.get("llm_score") is None or v.get("llm_score", -1) < 0)
        and (v.get("status") or "unseen") == "unseen"
        # A row the filter pass excluded from scoring (migration 0025) is not
        # awaiting scoring — it must not inflate the "run scoring next" hint.
        and not v.get("scoring_excluded_reason")
    )


# ---------------------------------------------------------------------------
# Triage data preparation
# ---------------------------------------------------------------------------


def prepare_triage_data() -> list[dict]:
    """Load triage reviews from triage.json for dashboard Pipeline tab.

    Returns flat list of review dicts with session metadata merged in.
    """
    triage_path = PROJECT_ROOT / "triage" / "triage.json"
    if not triage_path.exists():
        return []
    try:
        with open(triage_path, encoding="utf-8") as f:
            tdb = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    reviews = []
    for session in tdb.get("sessions", []):
        session_id = session.get("session_id")
        session_date = session.get("date", "")
        for r in session.get("reviews", []):
            review = {
                "vacancy_id": r.get("vacancy_id", ""),
                "org": r.get("org", ""),
                "title": r.get("title", ""),
                "llm_score": r.get("llm_score"),
                "verdict": r.get("verdict", ""),
                "first_impression": r.get("first_impression", ""),
                "cv_notes": r.get("cv_notes", ""),
                "preparation": r.get("preparation", ""),
                "skip_reason": r.get("skip_reason", ""),
                "research_question": r.get("research_question", ""),
                "deadline": r.get("deadline", ""),
                "github_issue": r.get("github_issue"),
                "session_id": session_id,
                "session_date": session_date,
            }
            reviews.append(review)
    return reviews


# Color palette for org badges — synced with paper-white --bg: #FAF9F7
_ORG_PALETTE = [
    ("#F26B3A", "#FFF5F0"),  # coral (--coral)
    ("#10B981", "#ECFDF5"),  # emerald (--emerald)
    ("#8B5CF6", "#F5F3FF"),  # lavender (--lavender)
    ("#E5A000", "#FFFBEB"),  # amber (--amber)
    ("#C2522A", "#FEF2F2"),  # terracotta
    ("#0284C7", "#F0F9FF"),  # sky
    ("#DB2777", "#FDF2F8"),  # rose
    ("#34D399", "#ECFDF8"),  # mint (--mint)
    ("#8B7355", "#F5F2EE"),  # earth
    ("#7B68AE", "#F0ECF8"),  # violet
]


def _build_org_colors(org_names: list[str]) -> dict:
    """Assign deterministic fg/bg color pairs to sorted org names."""
    org_colors = {}
    for i, org in enumerate(sorted(org_names)):
        org_colors[org] = list(_ORG_PALETTE[i % len(_ORG_PALETTE)])
    return org_colors


def _project_application(app: dict | None) -> dict | None:
    """Public-payload projection of an application row.

    The DAL (``applications.py``) returns the FULL row — artifact VALUES (inline
    answer prose, cover_letter_path, cv_version, research_urls) and free-text
    ``notes`` — for CLI/agent use. None of that may enter public code or the
    public dashboard (STRATEGY: application artifacts never leave the machine).
    The dashboard only ever renders status/channel/applied_at and the artifact
    KEYS (see catalog.js, companies.js ``buildApplicationsSection``), so the
    payload carries exactly that display shape and nothing more.

    ``artifacts`` stays an object keyed by the (sorted) artifact keys with the
    values blanked to ``True`` — the JS calls ``Object.keys(a.artifacts)`` and
    never reads a value, so rendering is unchanged while no private string can
    ride along.
    """
    if not app:
        return app
    artifacts = app.get("artifacts") or {}
    return {
        "status": app.get("status", ""),
        "channel": app.get("channel", ""),
        "applied_at": app.get("applied_at"),
        "artifacts": {k: True for k in sorted(artifacts)},
        "artifact_count": len(artifacts),
    }


def _build_group(
    v: dict,
    org_colors: dict,
    company_hq: dict,
    strong_model: str | None = None,
    applications_by_vac: dict | None = None,
) -> dict:
    """Build one frontend group dict from a vacancy row.

    ``strong_model`` is the currently-configured STRONG scoring tier (see
    ``scoring_settings.scoring_model``), used only to derive
    ``screen_only_score`` below — pass ``None`` to skip that derivation (e.g.
    a caller that doesn't care about score provenance).

    ``applications_by_vac`` is an optional {vacancy_id: application} map (see
    ``applications.applications_by_vacancy``); when provided, the vacancy's own
    application (if any) is attached so a card can show it. ``None`` -> no
    application field is derived (a caller that doesn't care).
    """
    # Locations v2: {work_mode, region, country, city, compensation, url}
    locs = v.get("locations", [])
    if not locs:
        locs = [
            {
                "work_mode": None,
                "region": None,
                "country": None,
                "city": None,
                "compensation": v.get("compensation", ""),
                "url": "",
            }
        ]

    # Sort by city/country for stable ordering
    locs.sort(key=lambda l: l.get("city") or l.get("country") or "")

    region_priority = {
        "europe": 0,
        "americas": 1,
        "remote": 2,
        "asia": 3,
        "africa": 4,
        "oceania": 5,
    }
    regions = [l.get("region") or "other" for l in locs]
    best_region = min(regions, key=lambda r: region_priority.get(r, 9))

    comp = next((l.get("compensation", "") for l in locs if l.get("compensation")), "")

    org_url = v.get("org_url", "")
    if not org_url:
        org_url = COMPANIES.get(v["org"], {}).get("careers_url", "")

    llm_score = (
        v.get("llm_score")
        if v.get("llm_score") is not None and v.get("llm_score", -1) >= 0
        else None
    )

    # Score provenance (two-pass scoring): which model tier last wrote
    # llm_score. NULL/absent (pre-two-pass rows, or a caller that skips
    # provenance) -> no badge, never a crash. "screen_only_score" flags a role
    # whose CURRENT score was never confirmed by the strong model AND is high
    # enough to show up in the dashboard's visible match band (APPLYABLE_SCORE)
    # — exactly the case a kept-cheap score could be mistaken for a genuine one.
    scored_by = v.get("scored_by")
    screen_only_score = bool(
        scored_by
        and strong_model
        and scored_by != strong_model
        and llm_score is not None
        and llm_score >= APPLYABLE_SCORE
    )

    # Build locations list for frontend (v2 structure)
    # Two concerns: PLACE (city/country) and WORK MODE (remote/hybrid/onsite)
    # Fallback: use company HQ when vacancy has no place data
    org_hq = company_hq.get(v["org"], "")
    entry_locations = []
    for m in locs:
        place_parts = [p for p in [m.get("city"), m.get("country")] if p]
        wm = (m.get("work_mode") or "").lower()
        region = (m.get("region") or "").lower()

        # Build place string
        place = ", ".join(place_parts) if place_parts else ""

        # Build work mode with region specificity
        if wm == "remote":
            if region == "europe":
                wm_display = "Remote · Europe"
            elif region == "us" or region == "americas":
                wm_display = "Remote · Americas"
            elif region and region != "other" and region != "remote":
                wm_display = "Remote · " + region.capitalize()
            else:
                wm_display = "Remote"
        elif wm == "hybrid":
            wm_display = "Hybrid"
        else:
            wm_display = ""  # onsite is implied, don't show

        # Combine: place + work mode
        # If no place from vacancy, use company HQ as fallback
        if not place and org_hq:
            place = "HQ: " + org_hq

        if place and wm_display:
            loc_display = place + ", " + wm_display
        elif place:
            loc_display = place
        elif wm_display:
            loc_display = wm_display
        else:
            loc_display = ""

        entry_locations.append(
            {
                "location": loc_display,
                "url": m.get("url") or "",
                "work_mode": wm or "",
                "region": m.get("region") or "",
                "city": m.get("city") or "",
                "country": m.get("country") or "",
            }
        )

    return {
        "id": v["id"],
        "org": v["org"],
        "title": v["title"],
        "org_url": org_url,
        "region": best_region,
        "llm_score": llm_score,
        "scored_by": scored_by,
        "screen_only_score": screen_only_score,
        "llm_summary": v.get("llm_summary", ""),
        "llm_reasoning": v.get("llm_reasoning", ""),
        "llm_hard_requirements": v.get("llm_hard_requirements", []),
        "us_eligibility": v.get("us_eligibility", ""),
        "snippet": v.get("snippet", ""),
        "full_description": v.get("full_description", ""),
        "compensation": comp,
        "deadline": v.get("deadline", ""),
        "first_seen": v.get("first_seen", ""),
        "last_seen": v.get("last_seen", ""),
        "org_color": org_colors.get(v["org"], ["#F97316", "#FFF7ED"]),
        "locations": entry_locations,
        "member_ids": [],
        "company_id": v.get("company_id", ""),
        # RAW provenance field (not a pre-computed aggregate): the display name
        # of the job board this role came from (== board.name), "" for a direct
        # ATS fetch. Ships so the browser can DERIVE per-board yield (scored /
        # fit / liked) in boards.js — the same "derive, never bake" path the
        # company rollups and Geo table take (STRATEGY guardrail 9).
        "source_board": v.get("source_board") or "",
        # The application attached to this vacancy (1:1), or None. Card shows a
        # small "applied · <status>" block; the full section is a later ticket.
        # Projected to the display shape — artifact values and notes stay private.
        "application": _project_application((applications_by_vac or {}).get(v.get("id"))),
        # RAW screening facts (migration 0027): the stored JSON object as the
        # night prepared it (posting_facts / profile_comparison / unknowns), its
        # state ("ready" | "failed" | None) and when it was prepared. The Screen
        # view DERIVES its lists and groups from these in the browser — no
        # pre-baked group ships (STRATEGY guardrail 9).
        "screening": v.get("screening"),
        "screening_state": v.get("screening_state"),
        "screening_prepared_at": v.get("screening_prepared_at") or "",
    }


def _company_hq_map():
    """Map canonical_name → HQ location for all companies."""
    from db_conn import get_conn
    from db_backend import RealDictCursor

    _conn = get_conn()
    _cur = _conn.cursor(cursor_factory=RealDictCursor)
    _cur.execute("SELECT canonical_name, about->>'hq_location' as hq FROM company")
    out = {r["canonical_name"]: r["hq"] for r in _cur.fetchall() if r.get("hq")}
    _cur.close()
    return out


def prepare_archived_data() -> list:
    """Lean list of archived vacancies for the Archive tab (no full_description)."""
    from scoring_settings import scoring_model
    import applications

    archived = load_vacancies(status="archived", include_inactive_companies=True, light=True)
    company_hq = _company_hq_map()
    apps_by_vac = applications.applications_by_vacancy()
    all_orgs = sorted(set(v["org"] for v in archived.values()))
    org_colors = _build_org_colors(all_orgs)
    strong_model = scoring_model()
    groups = [
        _build_group(v, org_colors, company_hq, strong_model, apps_by_vac)
        for v in archived.values()
    ]
    groups.sort(key=lambda g: (-(g["llm_score"] if g["llm_score"] is not None else -1), g["org"]))
    return groups


def prepare_report_data(db: dict = None) -> dict:
    """Group vacancies + org colors, plus the one non-derivable run-level fact.

    Loads from Supabase. The db param is accepted but ignored (backward compat).
    Returns dict with keys: groups, org_colors, stats.

    KISS derivation (phase 2): the payload ships RAW scored roles only; the
    browser derives every dashboard number from ``groups`` through the one shared
    visibility filter (see ``public/modules/derive.js``). So counts that were
    baked here and drifted stale between runs — total_roles, total_postings,
    new_today, with_comp, europe_count, "relevant" — are gone; the frontend
    computes them live from ``groups`` (e.g. total_roles == ``groups.length``).

    The single survivor is ``stats.unscored_count``: it counts fetched-but-unscored
    vacancies, which are deliberately NOT shipped in ``groups`` (the dashboard only
    shows scored roles). The browser therefore cannot derive it from the raw
    payload — it is a genuine run-level fact about rows that never leave the DB,
    so it stays as the smallest possible raw scalar (drives the "N fetched, none
    scored yet — run scoring next" empty state instead of a blank screen).
    """
    from scoring_settings import scoring_model
    import applications

    # Roles scoring > 40 surface on the dashboard regardless of their company's
    # status — a strong match is worth reviewing even before its company is
    # approved (score_floor_any_company). Company-status gating still hides the
    # long tail of low/unscored roles from not-yet-approved companies.
    all_vacs = load_vacancies(
        include_candidate_companies=True,
        status_exclude=["archived"],
        score_floor_any_company=40,
    )
    # Exclude unscored vacancies from dashboard — they appear after /score.
    #
    # Also drop the weak tail: an UNDECIDED role below CATALOG_MIN_SCORE never
    # reaches the dashboard at all (decision 2026-08-10). The 40 floor used to
    # live only in the Catalog tab's client-side filter and in
    # score_floor_any_company above, which gates unapproved companies — so a
    # weak role at an APPROVED company was shipped and shown everywhere else.
    # That is how "EA Funds — Director" (32, a scraped fundraiser page) and
    # "Elevate Philanthropy — Historical Projects" (28, no description) ended up
    # in front of him. One floor, applied once, before the data leaves Python.
    #
    # An ACTIVE decision outranks the score: the weakest role in the liked
    # basket scores 15, and a role being worked cannot be hidden by a number.
    # 'passed' / 'skipped' are NOT such decisions — they are dead ends, and
    # keeping every one of them shipped the whole rejected pile back onto the
    # board (a bulk pass of 190 roles reappeared in the list immediately).
    # Below the floor they are history, not work; the Archive tab still has them.
    #
    # A role the night PREPARED for screening (screening_state = 'ready') ships
    # at any status and any score: the Screen view keeps a Kept / Put aside row
    # visible after the decision, and the quoted evidence outranks the number.
    vacancies = [
        v
        for v in all_vacs.values()
        if v.get("screening_state") == "ready"
        or (
            v.get("llm_score") is not None
            and v.get("llm_score", -1) >= 0
            and (v.get("status") in _ACTIVE_STATUSES or v.get("llm_score", -1) >= CATALOG_MIN_SCORE)
        )
    ]
    # Fetched-but-not-yet-scored vacancies (rows NOT shipped in `groups`) — the one
    # count the browser can't derive from the raw payload; see the docstring.
    unscored_count = _count_unscored(all_vacs)

    # --- Build org color map (needed before building groups) ---
    all_orgs = sorted(set(v["org"] for v in vacancies))
    org_colors = _build_org_colors(all_orgs)

    # --- Build company HQ lookup for location fallback (from Supabase) ---
    company_hq = _company_hq_map()

    # --- Build grouped list (each vacancy is already unique by org+title) ---
    strong_model = scoring_model()
    apps_by_vac = applications.applications_by_vacancy()
    groups = [_build_group(v, org_colors, company_hq, strong_model, apps_by_vac) for v in vacancies]

    # --- Sort ---
    def sort_key(g):
        score = g["llm_score"] if g["llm_score"] is not None else -1
        return (
            -score,
            g["org"],
            g["title"],
        )

    groups.sort(key=sort_key)

    return {
        "groups": groups,
        "org_colors": org_colors,
        # Only non-derivable run-level facts ship (rows NOT in `groups`); every
        # other dashboard number is derived in the browser (KISS derivation).
        "stats": {
            "unscored_count": unscored_count,
            "screening_processing": _count_screening_processing(),
        },
    }


# ---------------------------------------------------------------------------
# Company data preparation
# ---------------------------------------------------------------------------

_EXEC_SUMMARY_RE = re.compile(
    r"##\s*(?:🎯\s*)?EXECUTIVE SUMMARY\s*\n\s*\n(.+?)(?:\n\s*\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _load_company_lookup() -> dict[str, dict]:
    """Load company data from Supabase keyed by canonical name."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, canonical_name, category, product, website, careers_url,
               offices, experience_match, personal_interest,
               user_comments, status, status_reason
        FROM company
    """)
    lookup = {}
    for (
        cid,
        name,
        category,
        product,
        website,
        careers_url,
        offices,
        exp_match,
        pers_interest,
        user_comments,
        status,
        status_reason,
    ) in cur.fetchall():
        db_status = (status or "").lower()
        review_map = {"active": "approved", "candidate": "pending", "inactive": "rejected"}
        lookup[name] = {
            "company_id": str(cid),
            "name": name,
            "category": category or "",
            "description": (product or "")[:200],
            "website": website or "",
            "careers_url": careers_url or "",
            "offices": offices or "",
            "experience_match": _parse_float(exp_match),
            "personal_interest": _parse_float(pers_interest),
            "notes": (user_comments or "")[:250],
            "status": db_status,
            "review_status": review_map.get(db_status, "pending"),
            "status_reason": (status_reason or "")[:200],
        }
    cur.close()
    return lookup


def _parse_float(val) -> float | None:
    if not val or str(val).strip() in ("", "TBD"):
        return None
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


def _load_executive_summaries() -> dict[str, dict]:
    """Scan Companies/**/*.md for EXECUTIVE SUMMARY sections.

    Returns dict keyed by canonical company name with keys:
      - summary: executive summary text (no truncation)
      - md_content: full raw markdown content of the file
    """
    summaries = {}
    companies_dir = PROJECT_ROOT / "Companies"
    if not companies_dir.exists():
        return summaries
    for md_path in companies_dir.rglob("*.md"):
        canonical = resolve_canonical_name(md_path.stem)
        text = md_path.read_text(encoding="utf-8", errors="replace")
        m = _EXEC_SUMMARY_RE.search(text)
        summary = ""
        if m:
            summary = m.group(1).strip()
            summary = summary.replace("**", "")
        summaries[canonical] = {
            "summary": summary,
            "md_content": text,
        }
    return summaries


def _load_enrichment() -> dict[str, dict]:
    """Load enrichment data from Supabase company table.

    Returns dict keyed by canonical name with about, mission_fit, social sub-dicts.
    """
    all_enrich = load_all_enrichment()
    # Reshape to match old enriched.json structure expected by prepare_company_data
    result = {}
    for name, data in all_enrich.items():
        result[name] = {
            "about": data.get("about", {}),
            "mission_fit": data.get("mission_fit", {}),
        }
    return result


from database_supabase import calculate_company_tier  # noqa: E402

try:  # The configured custom-boost key (+ legacy back-compat). Optional import.
    from prompts import CUSTOM_BOOST_KEYS as _BOOST_KEYS  # noqa: E402
except Exception:  # pragma: no cover - degrade to known keys if prompts unavailable
    _BOOST_KEYS = ("career_narrative_boost", "mpa_narrative_boost")


def _custom_boost(mission: dict):
    """Read the optional custom boost from a mission_fit dict.

    Accepts the configured key (e.g. 'career_narrative_boost') and the legacy
    'mpa_narrative_boost'. Returns None when absent or non-numeric.
    """
    if not isinstance(mission, dict):
        return None
    for key in _BOOST_KEYS:
        val = mission.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return val
    return None


def prepare_company_data(db: dict = None, org_colors: dict = None) -> list[dict]:
    """Build one raw row per company for the dashboard frontend.

    Loads from Supabase. db param is accepted but ignored (backward compat).

    KISS derivation (phase 2): the row carries company ENTITY fields (identity,
    enrichment, tier, monitoring) plus the raw ``vacancy_ids`` list — but NO
    per-company vacancy NUMBERS. vacancy_count, applyable_count, the average LLM
    score, the hot-vacancy signal and the region breakdown are all derived in the
    browser from those roles (public/modules/derive.js ``companyRollup``), so they
    react to a like/pass or a passing day with no run and a company's count can
    never disagree with the roles listed under it.
    """
    if org_colors is None:
        org_colors = {}
    import applications
    from database_supabase import load_company_evidence_summary

    company_lookup = _load_company_lookup()
    summaries = _load_executive_summaries()
    enrichment = _load_enrichment()
    vacancies = load_vacancies(include_inactive_companies=True, light=True)
    # Applications + research, keyed by company_id, so the profile shows
    # everything related in one place (nothing lost in folders). Both degrade to
    # {} on a fresh/pre-migration DB.
    apps_by_company = applications.applications_by_company()
    research_by_company = load_company_evidence_summary()

    # Load source tracking from company table
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT canonical_name, last_fetched, fetch_status FROM company")
    sources = {}
    for name, lf, fs in cur.fetchall():
        sources[name] = {
            "last_fetched": lf.isoformat() if lf else "",
            "fetch_status": fs or "",
        }
    cur.close()

    # --- Collect per-org vacancy ids (keyed by canonical name) ---
    # This id list is the ONLY per-company vacancy fact that ships now: every
    # NUMBER — vacancy_count, applyable_count, avg score, the hot-vacancy signal
    # and the region breakdown — is derived in the browser from these roles
    # (KISS derivation, phase 2; public/modules/derive.js `companyRollup`). The
    # browser can't reconstruct which roles belong to a company, so only the ids
    # ride along.
    org_vacancy_ids: dict[str, list] = {}
    for vid, v in vacancies.items():
        org = resolve_canonical_name(v.get("org", ""))
        if not org:
            continue
        org_vacancy_ids.setdefault(org, []).append(vid)

    # --- Collect all unique company names (keyed by canonical name) ---
    all_names: dict[str, str] = {}  # canonical → display name

    # From config.py (canonical = key itself)
    for name in COMPANIES:
        all_names[name] = name
    # From Supabase company table
    for canonical, row in company_lookup.items():
        if canonical not in all_names:
            all_names[canonical] = canonical
    # From Supabase vacancies (already resolved to canonical above)
    for canonical in org_vacancy_ids:
        if canonical not in all_names:
            all_names[canonical] = canonical

    # --- Orphan detection: configured companies with 0 vacancies ---
    for name, cfg in COMPANIES.items():
        if cfg.get("strategy") != "manual_check" and name not in org_vacancy_ids:
            print(
                f"  ⚠ Orphan: {name} (strategy={cfg.get('strategy')}) — 0 vacancies after aggregation"
            )

    # --- Build company records ---
    companies = []
    for canonical, display_name in all_names.items():
        cfg = COMPANIES.get(canonical, {})
        csv_row = company_lookup.get(canonical, {})
        vacancy_ids = org_vacancy_ids.get(canonical, [])

        is_in_config = canonical in COMPANIES
        is_monitored = canonical in company_lookup
        is_researched = is_in_config or is_monitored

        # Tier: calculated from enrichment (set later); CSV Tier column unused

        # Careers URL: config > CSV
        careers_url = cfg.get("careers_url", "") or csv_row.get("careers_url", "")

        slug = cfg.get("slug", display_name.lower().replace(" ", "-").replace(".", ""))

        summary_data = summaries.get(canonical, {})
        exec_summary = summary_data.get("summary", "") if isinstance(summary_data, dict) else ""
        md_content = summary_data.get("md_content", "") if isinstance(summary_data, dict) else ""
        has_deep = bool(exec_summary or md_content)

        # --- Enrichment data from enriched.json ---
        enrich = enrichment.get(canonical, {})
        about = enrich.get("about", {})
        mission = enrich.get("mission_fit", {})
        social = enrich.get("social", {})
        is_enriched = bool(about.get("description") or mission.get("alignment_score") is not None)

        company_id = csv_row.get("company_id", "")
        company_applications = apps_by_company.get(company_id, []) if company_id else []
        company_research = research_by_company.get(company_id, []) if company_id else []

        companies.append(
            {
                "name": display_name,
                "company_id": company_id,
                "slug": slug,
                "category": csv_row.get("category", ""),
                "product": csv_row.get("description", ""),
                "website": csv_row.get("website", ""),
                "careers_url": careers_url,
                "offices": about.get("hq_location", "") or csv_row.get("offices", ""),
                "strategy": cfg.get("strategy", ""),
                "status": csv_row.get("status", ""),
                "review_status": csv_row.get("review_status", "pending"),
                "status_reason": csv_row.get("status_reason", ""),
                "experience_match": csv_row.get("experience_match"),
                "personal_interest": csv_row.get("personal_interest"),
                "notes": csv_row.get("notes", ""),
                "has_deep_analysis": has_deep,
                "executive_summary": exec_summary,
                "md_content": md_content,
                "org_color": org_colors.get(display_name, list(_ORG_PALETTE[0])),
                "is_monitored": is_monitored,
                "is_researched": is_researched,
                "needs_source": bool(cfg.get("strategy"))
                and cfg.get("strategy") != "manual_check"
                and len(vacancy_ids) == 0,
                "is_archived": csv_row.get("status", "") == "inactive",
                "is_manual_check": cfg.get("strategy") == "manual_check",
                # Per-company NUMBERS (vacancy_count, applyable_count, avg score,
                # hot-vacancy signal, region breakdown) are derived in the browser
                # from the roles below (KISS derivation, phase 2;
                # public/modules/derive.js). Only the raw role-id list ships.
                "last_fetched": sources.get(canonical, {}).get("last_fetched", ""),
                "fetch_status": sources.get(canonical, {}).get("fetch_status", ""),
                "vacancy_ids": vacancy_ids,
                # --- Enrichment fields ---
                "is_enriched": is_enriched,
                "description": about.get("description", ""),
                "founded_year": about.get("founded_year", ""),
                "employee_count": about.get("employee_count", ""),
                "funding_status": about.get("funding_status", ""),
                "hq_location": about.get("hq_location", ""),
                "sector": about.get("sector", ""),
                "logo_url": about.get("logo_url", ""),
                "alignment_score": mission.get("alignment_score"),
                "alignment_label": mission.get("alignment_label", ""),
                "fit_dimensions": mission.get("dimensions", {}),
                "fit_evidence": mission.get("evidence_anchors", []),
                "fit_strengths": mission.get("strengths", []),
                "fit_risks": mission.get("risks", []),
                "fit_approach": mission.get("approach", ""),
                "experience_reasoning": mission.get("experience_match_reasoning", ""),
                "mission_verdict": mission.get("mission_verdict", ""),
                "glassdoor_rating": social.get("glassdoor_rating"),
                "linkedin_employees": social.get("linkedin_employees", ""),
                "recent_news": social.get("recent_news", []),
                # --- Applications + research ---
                # Everything the user has done toward this company: applications
                # (status + artifact KEYS only — values and notes stay private)
                # and the research rows collected in company_evidence. Shown as a
                # small block in the company profile only — the standalone
                # Applications dashboard section this projection once also fed
                # was deleted (0 real usage; the Triage "applied" column is
                # the tracker).
                "applications": [_project_application(a) for a in company_applications],
                "application_count": len(company_applications),
                "research": company_research,
                "research_count": len(company_research),
            }
        )

        # Calculate composite tier from enrichment scores. custom_boost is an
        # optional user-defined signal (career_narrative_boost / legacy
        # mpa_narrative_boost in mission_fit) — unrelated to the removed MPA
        # Prestige display column, it only nudges the composite/tier.
        c = companies[-1]
        custom_boost = _custom_boost(mission)
        tier_letter, composite = calculate_company_tier(c["alignment_score"], custom_boost)
        c["calculated_tier"] = tier_letter
        c["composite_score"] = composite

    # Filter out parsing artifacts (phantom orgs from ATS boards)
    if PARSING_ARTIFACTS:
        companies = [c for c in companies if c["name"] not in PARSING_ARTIFACTS]

    # Sort: calculated tier (S→A→B→C→none), then name ASC
    _TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}
    companies.sort(key=lambda c: (_TIER_ORDER.get(c["calculated_tier"], 4), c["name"]))
    return companies


# ---------------------------------------------------------------------------
# Boards catalogue — for the dashboard's Boards section (baked, works offline).
# ---------------------------------------------------------------------------


def prepare_boards_catalog() -> list:
    """The job-board catalogue for the dashboard's Boards section.

    Every board defined in ``config/defaults.toml [boards.*]`` with its neutral
    ``audience`` description, source strategy, tier, TTL, url, whether it is
    persisted as ENABLED (``board.enabled``, PR #39), and its ``last_fetched``
    when the board table has one. This is baked into the payload so the Boards
    section renders its full read-only catalogue (audience + enabled state) in
    simple mode too — the live ``/api/board-statuses`` endpoint only adds the
    vacancy counts on top. Never raises: a missing board table (fork on an old
    schema) degrades to ``enabled=False`` and no ``last_fetched``.
    """
    import settings

    boards_cfg = settings.boards()

    enabled_ids: set[str] = set()
    last_fetched: dict[str, str] = {}
    try:
        import database_supabase as _dal

        enabled_ids = set(_dal.get_enabled_boards())
    except Exception:
        enabled_ids = set()
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, last_fetched FROM board")
        for bid, lf in cur.fetchall():
            if lf:
                last_fetched[bid] = lf.isoformat() if hasattr(lf, "isoformat") else str(lf)
        cur.close()
    except Exception:
        last_fetched = {}

    catalog = []
    for board_id in sorted(boards_cfg):
        cfg = boards_cfg[board_id]
        catalog.append(
            {
                "id": board_id,
                "name": cfg.get("name", board_id),
                "audience": cfg.get("audience", ""),
                "strategy": cfg.get("strategy", ""),
                "tier": cfg.get("tier", ""),
                "ttl_days": cfg.get("ttl_days"),
                "url": cfg.get("url", ""),
                "enabled": board_id in enabled_ids,
                "last_fetched": last_fetched.get(board_id, ""),
            }
        )
    return catalog


# ---------------------------------------------------------------------------
# Settings — RESOLVED config dials for the dashboard's Settings section.
# ---------------------------------------------------------------------------


def prepare_settings_payload() -> dict:
    """Resolved, read-only dials for the dashboard's Settings section.

    Only RESOLVED VALUES leave here — never personal profile prose, secrets or
    artifact values (guard tests enforce that). Each row carries a neutral
    ``source`` pointer (a file + section) so the user knows the one line to edit.
    Grouped into volume / scoring / thresholds. Never raises: every value falls
    back to its neutral default.
    """
    import settings as _settings
    import scoring_settings

    def _safe(fn, fallback):
        try:
            return fn()
        except Exception:
            return fallback

    vol = _safe(_settings.volume, {})
    th = _safe(_settings.thresholds, {})

    def _row(key, value, source):
        return {"key": key, "value": value, "source": source}

    # Product language — the ONE choice that switches the whole product
    # (agent replies, run reports, digest, dashboard default). Displayed here with
    # the single place to change it, so a stranger sees where the language lives.
    import product_language

    product_rows = [
        _row(
            "set_product_language",
            _safe(product_language.language_label, "English"),
            "config/user_profile.md → ## OUTPUT_LANGUAGE (change via /jobs-profile)",
        ),
    ]

    volume_rows = [
        _row(
            "set_max_active_companies",
            vol.get("max_active_companies"),
            "config/defaults.toml → [volume] max_active_companies",
        ),
        _row(
            "set_daily_scoring_limit",
            vol.get("daily_scoring_limit"),
            "config/defaults.toml → [volume] daily_scoring_limit",
        ),
        _row(
            "set_digest_size",
            vol.get("digest_size"),
            "config/defaults.toml → [volume] digest_size",
        ),
    ]

    scoring_rows = [
        _row(
            "set_scoring_model",
            _safe(scoring_settings.scoring_model, "sonnet"),
            "config/user_profile.md → ## VOLUME scoring_model (else default sonnet)",
        ),
        _row(
            "set_screen_model",
            _safe(scoring_settings.screen_model, "haiku"),
            "config/user_profile.md → ## VOLUME screen_model (else default haiku)",
        ),
        _row(
            "set_escalate_threshold",
            _safe(scoring_settings.escalation_threshold, 50),
            "config/user_profile.md → ## VOLUME escalate_threshold (else default 50)",
        ),
        _row(
            "set_max_per_run",
            _safe(scoring_settings.max_per_run, 150),
            "config/user_profile.md → ## VOLUME max_per_run (else [volume] daily_scoring_limit)",
        ),
    ]

    threshold_rows = [
        _row(
            "set_llm_score_threshold",
            th.get("llm_score_threshold"),
            "config/defaults.toml → [thresholds] llm_score_threshold",
        ),
        _row("set_tier_s", th.get("tier_s"), "config/defaults.toml → [thresholds] tier_s"),
        _row("set_tier_a", th.get("tier_a"), "config/defaults.toml → [thresholds] tier_a"),
        _row("set_tier_b", th.get("tier_b"), "config/defaults.toml → [thresholds] tier_b"),
        _row("set_tier_c", th.get("tier_c"), "config/defaults.toml → [thresholds] tier_c"),
    ]

    return {
        "groups": [
            {"key": "settings_grp_product", "rows": product_rows},
            {"key": "settings_grp_volume", "rows": volume_rows},
            {"key": "settings_grp_scoring", "rows": scoring_rows},
            {"key": "settings_grp_thresholds", "rows": threshold_rows},
        ],
    }


# ---------------------------------------------------------------------------
# Learning-cycle hint — a light "there are verdicts to review" flag for Today.
# ---------------------------------------------------------------------------


def prepare_learning_hint() -> dict | None:
    """A tiny, deterministic hint for the Today section: is a learning cycle
    pending? Reuses ``learning.build_review()`` (no LLM, the same seam the
    ``/jobs-new`` gate calls) and keeps only the counts — never the proposal
    text. Returns ``None`` (no hint shown) when unavailable or nothing pending.
    """
    try:
        import learning

        review = learning.build_review()
    except Exception:
        return None
    if not review or not review.get("has_content"):
        return None
    props = review.get("proposals", {}) or {}
    proposal_count = len(props.get("filter_words", []) or []) + len(
        props.get("factor_moves", []) or []
    )
    return {
        "pending": True,
        "verdicts": int(review.get("verdicts_since_last_review", 0) or 0),
        "proposals": proposal_count,
    }
