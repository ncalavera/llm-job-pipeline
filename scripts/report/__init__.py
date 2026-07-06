"""Dashboard assembler — generates data.js only.

The ``public/index.html`` shell is a static, hand-maintained, version-controlled
file. Generation NEVER rewrites it (doing so reintroduced stale copy and dirtied
git on every run). This module only regenerates ``public/data.js``.
"""

import json
import os

from .data_prep import (
    prepare_report_data,
    prepare_company_data,
    prepare_triage_data,
    prepare_archived_data,
    prepare_boards_catalog,
    prepare_settings_payload,
    prepare_learning_hint,
)

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

        cfg = settings.dashboard()
        style = str(cfg.get("style", "illustrated")).strip().lower()
        if style in _DASHBOARD_STYLES:
            return style
    except Exception:
        pass
    return "illustrated"


def _resolve_language() -> str:
    """Pick the dashboard's DEFAULT language — the baked ``config.language``.

    This is the server-chosen default the frontend uses on first load; an
    explicit client toggle (``localStorage['dashboard_lang']``) still wins in the
    browser (see ``public/modules/i18n.js``). The default is the ONE product
    language: the profile's ``## OUTPUT_LANGUAGE`` drives it via
    ``product_language.resolve()``. ``DASHBOARD_LANGUAGE`` stays as an explicit
    per-render override; ``product_language`` itself also honours the legacy
    ``[dashboard] language`` knob when the profile is silent. Falls back to
    ``en`` when nothing resolves to a bundled language.
    """
    import i18n

    available = set(i18n.available_languages())
    env = (os.environ.get("DASHBOARD_LANGUAGE") or "").strip().lower()
    if env in available:
        return env
    try:
        import product_language

        lang = product_language.resolve()
        if lang in available:
            return lang
    except Exception:
        pass
    return i18n.DEFAULT_LANGUAGE


def _resolve_pack() -> str:
    """Pick the illustration pack: env DASHBOARD_PACK overrides config default.

    Falls back to ``default`` when unset. Any non-empty name is accepted (the
    pack's images are resolved from ``images/<pack>/`` at render time).
    """
    env = (os.environ.get("DASHBOARD_PACK") or "").strip()
    if env:
        return env
    try:
        import settings

        return str(settings.dashboard().get("illustration_pack", "default")).strip() or "default"
    except Exception:
        return "default"


def _resolve_show_mpa() -> bool:
    """Whether to show the personal "MPA Prestige" column/profile metric.

    Off by default (it is specific to an MPA/MPP application strategy). Opt in
    with env ``DASHBOARD_MPA=1`` or ``[dashboard] show_mpa_column = true``.
    """
    env = (os.environ.get("DASHBOARD_MPA") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        import settings

        return bool(settings.dashboard().get("show_mpa_column", False))
    except Exception:
        return False


def _guard_shared_snapshot_profile() -> None:
    """Refuse to bake example/default settings into the SHARED production snapshot.

    The linked-worktree trap: ``config/user_profile.md`` is gitignored, so a run
    launched from a git worktree that lacks it — and cannot reach the main
    checkout's copy — resolves the bundled EXAMPLE profile. On the
    Postgres/Supabase backend that payload (settings, language, thresholds) is
    upserted into the SINGLE shared ``dashboard_snapshot`` row, overwriting the
    user profile's real values (e.g. flipping the dashboard language to English).
    ``prompts`` first tries the main checkout's profile; if that ALSO fails and
    we are on Postgres, stop here rather than corrupt shared state.

    SQLite/local stays permissive so fresh installs (the onboarding path) and the
    offline test suite keep working. An explicit ``USER_PROFILE_PATH`` (how the
    test suite pins the example) counts as a real profile and is never blocked.
    """
    import db_backend

    if db_backend.IS_SQLITE:
        return
    try:
        import prompts

        if prompts.has_real_profile():
            return
    except Exception:
        return  # never block a healthy prod render on a resolver hiccup
    raise RuntimeError(
        "Refusing to write the shared production dashboard snapshot: no real "
        "config/user_profile.md was found, so the bundled EXAMPLE profile would "
        "be baked into shared state (settings, language, thresholds), "
        "overwriting the user profile's real values. This is the linked-worktree "
        "trap — user_profile.md is gitignored and does not travel with a git "
        "worktree. Re-run from the MAIN checkout (where config/user_profile.md "
        "exists), or set USER_PROFILE_PATH to your real profile. Nothing was "
        "written."
    )


def generate_dashboard(db: dict = None) -> None:
    """Generate the vacancy dashboard data file: ``public/data.js``.

    Loads all data from Supabase. db param is accepted but ignored (backward compat).
    The ``index.html`` shell is static and intentionally NOT written here.
    """
    # Fail fast before any DB read: never bake the EXAMPLE profile into the
    # shared production snapshot (the linked-worktree trap). SQLite is permissive.
    _guard_shared_snapshot_profile()

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
    has_both = sum(
        1 for c in csv_companies if c.get("description") and c.get("alignment_score") is not None
    )
    has_neither = sum(
        1 for c in csv_companies if not c.get("description") and c.get("alignment_score") is None
    )
    # --- Monitoring strategy breakdown ---
    auto_fetch = sum(1 for c in monitored if c.get("strategy") and c["strategy"] != "manual_check")
    manual_check = sum(1 for c in monitored if c.get("strategy") == "manual_check")
    # Strategy type counts
    strategy_counts = {}
    for c in monitored:
        s = c.get("strategy", "")
        if s:
            # Group ATS strategies by type
            if s in (
                "greenhouse",
                "lever",
                "ashby",
                "workable",
                "recruitee",
                "teamtailor_rss",
                "bamboohr",
            ):
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

    # --- Latency / SLA health metrics for the "Сегодня" tab ---
    from .data_prep import compute_latency_metrics

    latency_metrics = compute_latency_metrics()

    # --- Build triage data for Triage tab ---
    triage_reviews = prepare_triage_data()

    # --- Build archived vacancies for Archive tab ---
    archived_groups = prepare_archived_data()

    # --- Boards catalogue (Boards section) + resolved dials (Settings section) ---
    # Both are read-only, baked so the sections render in simple mode without the
    # live API. Settings carries RESOLVED values only (no profile prose/secrets).
    boards_catalog = prepare_boards_catalog()
    settings_payload = prepare_settings_payload()
    # A light "verdicts pending" flag for the Today section (deterministic, no LLM).
    learning_hint = prepare_learning_hint()
    for g in archived_groups:
        canonical = resolve_canonical_name(g["org"])
        g["calculated_tier"] = tier_lookup.get(canonical)
        g["company_slug"] = slug_lookup.get(canonical)
        g["company_name"] = canonical if slug_lookup.get(canonical) else None

    # --- Resolve presentation knobs (env overrides config) ---
    import i18n
    from .packs import pack_images

    language = _resolve_language()
    pack = _resolve_pack()

    # --- Build VACANCY_DATA payload for JS ---
    from datetime import datetime, timezone

    vacancy_data = {
        "config": {
            # Timezone-aware UTC (ends in "+00:00"): browsers parse an
            # offset-less ISO string as browser-LOCAL time, which would skew
            # the fallback banner's 48h staleness check by up to
            # ±14h depending on the viewer's timezone.
            "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "api_base": "",  # Vercel uses relative paths
            "dashboard_style": _resolve_dashboard_style(),
            "language": language,
            "i18n": i18n.strings(language),
            # All language maps so the dashboard can switch RU/EN client-side
            # without a rebuild. "i18n" stays the server-chosen default for the
            # first render and as a fallback when "i18n_all" is absent.
            "i18n_all": {lang: i18n.strings(lang) for lang in i18n.available_languages()},
            "pack": pack,
            "pack_images": pack_images(pack),
            "show_mpa_column": _resolve_show_mpa(),
        },
        "stats": stats,
        "enrichment_stats": enrichment_stats,
        "latency_metrics": latency_metrics,
        "vacancy_ids": [g["id"] for g in groups],
        "groups": groups,
        "companies": companies,
        "triage_reviews": triage_reviews,
        "archived_groups": archived_groups,
        # Read-only catalogue + resolved dials for the Boards / Settings sections.
        "boards_catalog": boards_catalog,
        "settings": settings_payload,
        "learning": learning_hint,
    }

    # --- Persist the payload to the active sink ---
    # Simple mode bakes public/data.js (served statically, no API host). Full
    # mode upserts the snapshot row so /api/vacancies serves it live with no
    # redeploy, and does NOT write data.js.
    _persist_dashboard(vacancy_data)


def _upsert_dashboard_snapshot(vacancy_data: dict, conn) -> None:
    """Upsert the assembled payload into the dashboard_snapshot 'current' row.

    Full mode only. /api/vacancies reads the 'current' row, so a browser refresh
    shows current data without a Vercel redeploy.

    Before overwriting, the existing 'current' payload is copied to a 'previous'
    row so a bad run (e.g. a truncated fetch that mass-archived real vacancies)
    can be rolled back — the live snapshot is no longer a single destructive
    overwrite with nothing to fall back to. Roll back with:
        UPDATE dashboard_snapshot c SET payload = p.payload, updated_at = now()
        FROM dashboard_snapshot p WHERE c.id = 'current' AND p.id = 'previous';

    NOTE: this commits ``conn``. Callers must commit their own pending writes
    BEFORE calling generate_dashboard (per AGENTS.md the DAL leaves commits to
    the caller); the fetch and score paths already do.
    """
    from db_backend import Json

    cur = conn.cursor()
    # Snapshot the current payload as 'previous' (no-op on the very first run).
    cur.execute(
        "INSERT INTO dashboard_snapshot (id, payload, updated_at) "
        "SELECT 'previous', payload, updated_at FROM dashboard_snapshot "
        "WHERE id = 'current' "
        "ON CONFLICT (id) DO UPDATE "
        "SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at",
    )
    cur.execute(
        "INSERT INTO dashboard_snapshot (id, payload, updated_at) "
        "VALUES ('current', %s, now()) "
        "ON CONFLICT (id) DO UPDATE "
        "SET payload = EXCLUDED.payload, updated_at = now()",
        (Json(vacancy_data),),
    )
    conn.commit()


def _persist_dashboard(vacancy_data: dict) -> None:
    """Route the payload to the active backend's sink.

    SQLite (simple mode) -> static public/data.js. Supabase (full mode) ->
    dashboard_snapshot row, and NO data.js.
    """
    import db_backend

    if db_backend.IS_SQLITE:
        _write_data_js(vacancy_data)
    else:
        _upsert_dashboard_snapshot(vacancy_data, db_backend.get_conn())


def _write_data_js(vacancy_data: dict) -> None:
    """Bake public/data.js — the static snapshot for simple/local mode."""
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    data_js_path = PUBLIC_DIR / "data.js"
    payload_json = json.dumps(vacancy_data, ensure_ascii=False).replace("</", "<\\/")
    with open(data_js_path, "w", encoding="utf-8") as f:
        f.write("// Auto-generated \u2014 DO NOT EDIT\n")
        f.write(f"var VACANCY_DATA = {payload_json};\n")
