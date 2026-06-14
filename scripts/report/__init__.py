"""Dashboard assembler — generates data.js only.

The ``public/index.html`` shell is a static, hand-maintained, version-controlled
file. Generation NEVER rewrites it (doing so reintroduced stale copy and dirtied
git on every run). This module only regenerates ``public/data.js``.
"""

import json
import os

from .data_prep import prepare_report_data, prepare_company_data, prepare_triage_data, prepare_archived_data

from config import PUBLIC_DIR, resolve_canonical_name


# Two supported dashboard styles. Public, owner-agnostic labels only.
_DASHBOARD_STYLES = ("illustrated", "minimal")


def _resolve_dashboard_style() -> str:
    """Pick the dashboard style: env DASHBOARD_STYLE overrides config default.

    Falls back to ``illustrated`` when unset or invalid.
    """
    env = (os.environ.get("DASHBOARD_STYLE") or "").strip().lower()
    if env in _DASHBOARD_STYLES:
        return env

    try:
        import settings
        cfg = settings.load_defaults().get("dashboard", {})
        style = str(cfg.get("style", "illustrated")).strip().lower()
        if style in _DASHBOARD_STYLES:
            return style
    except Exception:
        pass
    return "illustrated"


def generate_dashboard(db: dict = None) -> None:
    """Generate the vacancy dashboard data file: ``public/data.js``.

    Loads all data from Supabase. db param is accepted but ignored (backward compat).
    The ``index.html`` shell is static and intentionally NOT written here.
    """
    data = prepare_report_data()
    groups = data["groups"]
    stats = data["stats"]
    org_colors = data["org_colors"]

    # --- Build company data ---
    companies = prepare_company_data(org_colors=org_colors)

    # --- Inject calculated_tier + company_slug into groups via canonical name ---
    tier_lookup = {c["name"]: c.get("calculated_tier") for c in companies}
    slug_lookup = {c["name"]: c["slug"] for c in companies if c.get("is_monitored")}
    for g in groups:
        canonical = resolve_canonical_name(g["org"])
        g["calculated_tier"] = tier_lookup.get(canonical)
        g["company_slug"] = slug_lookup.get(canonical)
        g["company_name"] = canonical if slug_lookup.get(canonical) else None

    # --- Enrichment stats (CSV companies only) ---
    csv_companies = [c for c in companies if c.get("is_monitored")]
    monitored = [c for c in csv_companies if c.get("strategy")]
    has_about = sum(1 for c in csv_companies if c.get("description"))
    has_mission = sum(1 for c in csv_companies if c.get("alignment_score") is not None)
    has_both = sum(1 for c in csv_companies if c.get("description") and c.get("alignment_score") is not None)
    has_neither = sum(1 for c in csv_companies if not c.get("description") and c.get("alignment_score") is None)
    # --- Monitoring strategy breakdown ---
    auto_fetch = sum(1 for c in monitored if c.get("strategy") and c["strategy"] != "manual_check")
    manual_check = sum(1 for c in monitored if c.get("strategy") == "manual_check")
    # Strategy type counts
    strategy_counts = {}
    for c in monitored:
        s = c.get("strategy", "")
        if s:
            # Group ATS strategies by type
            if s in ("greenhouse", "lever", "ashby", "workable", "recruitee",
                      "teamtailor_rss", "bamboohr"):
                key = "ats_api"
            elif s in ("workday_api", "amazon_jobs", "unops_widget"):
                key = "custom_api"
            elif s == "firecrawl_scrape":
                key = "firecrawl"
            elif s == "manual_check":
                key = "manual"
            else:
                key = s
            strategy_counts[key] = strategy_counts.get(key, 0) + 1

    enrichment_stats = {
        "total_csv": len(csv_companies),
        "monitored": len(monitored),
        "auto_fetch": auto_fetch,
        "manual_check": manual_check,
        "strategy_counts": strategy_counts,
        "enriched_about": has_about,
        "enriched_mission": has_mission,
        "enriched_full": has_both,
        "empty_shells": has_neither,
    }

    # --- Build triage data for Triage tab ---
    triage_reviews = prepare_triage_data()

    # --- Build archived vacancies for Archive tab ---
    archived_groups = prepare_archived_data()
    for g in archived_groups:
        canonical = resolve_canonical_name(g["org"])
        g["calculated_tier"] = tier_lookup.get(canonical)
        g["company_slug"] = slug_lookup.get(canonical)
        g["company_name"] = canonical if slug_lookup.get(canonical) else None

    # --- Build VACANCY_DATA payload for JS ---
    from datetime import datetime
    vacancy_data = {
        "config": {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "api_base": "",  # Vercel uses relative paths
            "dashboard_style": _resolve_dashboard_style(),
        },
        "stats": stats,
        "enrichment_stats": enrichment_stats,
        "vacancy_ids": [g["id"] for g in groups],
        "groups": groups,
        "companies": companies,
        "triage_reviews": triage_reviews,
        "archived_groups": archived_groups,
    }

    # --- Write data.js ---
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    data_js_path = PUBLIC_DIR / "data.js"
    payload_json = json.dumps(vacancy_data, ensure_ascii=False).replace("</", "<\\/")
    with open(data_js_path, "w", encoding="utf-8") as f:
        f.write("// Auto-generated \u2014 DO NOT EDIT\n")
        f.write(f"var VACANCY_DATA = {payload_json};\n")

    # index.html is a static, hand-maintained file — intentionally NOT generated.
