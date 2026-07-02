"""Filter unscored vacancies: classify, report, delete, suggest blacklist updates.

Usage:
    python3 scripts/filter_vacancies.py                          # Analyze + HTML report + JSON
    python3 scripts/filter_vacancies.py --delete-ids id1,id2,... # Archive & remove from Supabase
    python3 scripts/filter_vacancies.py --suggest-blacklist       # Analyze deleted patterns
"""

import argparse
import html as _html
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser. Defined before the heavy project imports so
    ``--help`` / ``-h`` prints usage without connecting to the database or
    loading the user profile (those happen only when a real command runs)."""
    parser = argparse.ArgumentParser(description="Filter unscored vacancies")
    parser.add_argument("--delete-ids", type=str, help="Comma-separated IDs to archive and remove")
    parser.add_argument("--suggest-blacklist", action="store_true", help="Analyze deleted patterns")
    parser.add_argument(
        "--dedup",
        action="store_true",
        help="Run dedup pre-step: clean exact hash dupes + find fuzzy dupes",
    )
    return parser


# Print help and exit BEFORE importing anything that touches the DB or profile.
from cli_help import wants_help

if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

from config import (
    VACANCIES_DIR,
    GLOBAL_BLACKLIST,
    GEO_BANNED_COUNTRIES,
    GEO_BANNED_REGIONS,
    GEO_KEEP_COUNTRIES_SET,
    GEO_ACTIVE,
)
from geo import canonical_country, country_for_location, country_banned
from db_backend import RealDictCursor
import filters
from database_supabase import (
    load_vacancies,
    delete_vacancies as db_delete_vacancies,
    get_conn,
    update_vacancy_fields,
    get_archived_hashes,
    record_archived_hashes,
)


# ---------------------------------------------------------------------------
# HTML stripping for description blacklist checking
# ---------------------------------------------------------------------------

_BLOCK_TAGS = re.compile(r"<(script|style)[^>]*>.*?</(script|style)>", re.DOTALL | re.IGNORECASE)
_HTML_TAGS = re.compile(r"<[^>]+>")
_MULTI_WS = re.compile(r"\s{2,}")
_DESCRIPTION_SCAN_CHARS: int = 600


def _strip_html(raw: str) -> str:
    """Strip HTML tags, script/style blocks, and decode entities."""
    if not raw or "<" not in raw:
        return raw
    text = _BLOCK_TAGS.sub(" ", raw)
    text = _HTML_TAGS.sub(" ", text)
    text = _html.unescape(text)
    return _MULTI_WS.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Dedup — ported from clean_vacancies.py. The cross-board fuzzy matcher itself
# now lives in scripts/filters.py; the exact-hash cleanup below stays here.
# ---------------------------------------------------------------------------

_FUZZY_THRESHOLD: float = 0.85
_PROTECTED_STATUSES: frozenset[str] = frozenset(
    {
        "liked",
        "to_apply",
        "to_research",
        "to_network",
        "applied",
        "archived",
    }
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _strip_punct(title: str) -> str:
    """Strip all punctuation, lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title.lower())).strip()


def _pick_winner(a: dict, b: dict) -> tuple[dict, dict]:
    """Pick winner/loser for a duplicate pair.

    Priority: protected status → llm_score → description length → first_seen.
    """
    a_protected = a.get("status", "unseen") in _PROTECTED_STATUSES
    b_protected = b.get("status", "unseen") in _PROTECTED_STATUSES

    if a_protected and not b_protected:
        return a, b
    if b_protected and not a_protected:
        return b, a
    if a_protected and b_protected:
        return a, b  # both protected — caller handles as manual_review

    # Both unseen — compare quality
    a_score = a.get("llm_score") or 0
    b_score = b.get("llm_score") or 0
    if a_score != b_score:
        return (a, b) if a_score > b_score else (b, a)

    a_desc = len(a.get("full_description") or "")
    b_desc = len(b.get("full_description") or "")
    if a_desc != b_desc:
        return (a, b) if a_desc > b_desc else (b, a)

    a_seen = a.get("first_seen", "9999")
    b_seen = b.get("first_seen", "9999")
    return (a, b) if a_seen <= b_seen else (b, a)


def _merge_fields(winner: dict, loser: dict) -> dict:
    """Compute fields to copy from loser to winner. Returns kwargs for update_vacancy_fields."""
    updates = {}

    # full_description: take longer
    w_desc = winner.get("full_description") or ""
    l_desc = loser.get("full_description") or ""
    if len(l_desc) > len(w_desc):
        updates["full_description"] = l_desc

    # snippet: fill if missing
    if not winner.get("snippet") and loser.get("snippet"):
        updates["snippet"] = loser["snippet"]

    # locations: deduplicated merge
    w_locs = winner.get("locations") or []
    l_locs = loser.get("locations") or []
    if l_locs:
        existing = {json.dumps(loc, sort_keys=True) for loc in w_locs}
        merged = list(w_locs)
        for loc in l_locs:
            if json.dumps(loc, sort_keys=True) not in existing:
                merged.append(loc)
        if len(merged) > len(w_locs):
            updates["locations"] = merged

    # LLM scoring: copy if winner unscored
    if not winner.get("llm_score") and loser.get("llm_score"):
        updates["llm_score"] = loser["llm_score"]
        if loser.get("llm_reasoning"):
            updates["llm_reasoning"] = loser["llm_reasoning"]
        if loser.get("llm_summary"):
            updates["llm_summary"] = loser["llm_summary"]

    # Date range: expand
    w_first = winner.get("first_seen", "9999")
    l_first = loser.get("first_seen", "9999")
    if l_first < w_first:
        updates["first_seen"] = l_first

    w_last = winner.get("last_seen", "0000")
    l_last = loser.get("last_seen", "0000")
    if l_last > w_last:
        updates["last_seen"] = l_last

    return updates


def _clean_exact_dupes() -> dict:
    """Pre-classification: find and clean exact dedup_hash duplicates.

    Returns dict with counts: {examined, merged, protected, archived_path}.
    Archive losers to PROJECT_ROOT/vacancies/archive/ before deleting.
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT dedup_hash, array_agg(id::text) as ids
        FROM vacancy
        WHERE dedup_hash IS NOT NULL
        GROUP BY dedup_hash
        HAVING count(*) > 1
    """)
    dupe_groups = cur.fetchall()
    cur.close()

    if not dupe_groups:
        return {"examined": 0, "merged": 0, "protected": 0}

    # Load full vacancy data for all dupes
    all_dupe_ids = []
    for group in dupe_groups:
        all_dupe_ids.extend(group["ids"])

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM vacancy WHERE id = ANY(%s::uuid[])", (all_dupe_ids,))
    vac_by_id = {str(row["id"]): dict(row) for row in cur.fetchall()}
    cur.close()

    losers_to_delete = []
    losers_data = {}
    protected_count = 0

    for group in dupe_groups:
        ids = group["ids"]
        vacs = [vac_by_id[uid] for uid in ids if uid in vac_by_id]
        if len(vacs) < 2:
            continue

        # Sort by quality, pick best as winner
        remaining = list(vacs)
        while len(remaining) > 1:
            winner, loser = _pick_winner(remaining[0], remaining[1])
            loser_id = str(loser["id"])

            # Skip if loser is protected
            if loser.get("status", "unseen") in _PROTECTED_STATUSES:
                protected_count += 1
                remaining.remove(loser)
                continue

            # Merge fields from loser into winner
            merge_updates = _merge_fields(winner, loser)
            if merge_updates:
                winner_id = str(winner["id"])
                update_vacancy_fields(winner_id, **merge_updates)

            losers_to_delete.append(loser_id)
            losers_data[loser_id] = _serialize_vacancy(loser)
            remaining.remove(loser)

    if not losers_to_delete:
        return {"examined": len(dupe_groups), "merged": 0, "protected": protected_count}

    # Archive before delete
    archive_dir = PROJECT_ROOT / "vacancies" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"dupe_cleanup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    archive_path.write_text(
        json.dumps(
            {
                "archived_at": datetime.now().isoformat(),
                "source": "filter_dedup",
                "count": len(losers_to_delete),
                "vacancies": losers_data,
            },
            indent=2,
            default=str,
        )
    )

    # Delete losers
    db_delete_vacancies(losers_to_delete)
    conn.commit()

    return {
        "examined": len(dupe_groups),
        "merged": len(losers_to_delete),
        "protected": protected_count,
        "archived_path": str(archive_path),
    }


def _find_fuzzy_dupes(vacancies: dict) -> list[dict]:
    """Find fuzzy title duplicates within company groups (cross-board aware).

    Thin wrapper over filters.find_duplicates — the matcher's single home.
    """
    return filters.find_duplicates(vacancies)


def _serialize_vacancy(vac: dict) -> dict:
    """Serialize vacancy dict for JSON archive (convert dates to ISO strings)."""
    result = {}
    for k, v in vac.items():
        if isinstance(v, (date, datetime)):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _get_vacancy_url(vac: dict) -> str:
    """Get best URL for a vacancy: first location URL, then top-level."""
    locs = vac.get("locations", [])
    for loc in locs:
        url = loc.get("url", "")
        if url:
            return url
    return vac.get("url", "")


# ---------------------------------------------------------------------------
# Personal geography exclusions — driven by the user profile, NOT hardcoded.
#
# EXCLUDE_COUNTRIES comes from config.py, which reads the `## HARD_FILTERS`
# section of the user profile. It is EMPTY by default → no vacancy is dropped on
# geography. A location is excluded ONLY when its resolved COUNTRY is EXACTLY in
# this list (normalized). Geo BUCKETS (us / europe / …) are region LABELS only
# and are NEVER used for exclusion — excluding "canada" must not drop the United
# States just because both share the "us" bucket. No country is privileged.
# ---------------------------------------------------------------------------


def _normalize_country(name: str) -> str:
    """Canonicalize a country name for exact comparison (alias-proof)."""
    return canonical_country(name)


# Canonicalised policy caches, built once in config.py and aliased here so the
# geo gate (and the snapshot test, which patches these names) share one view.
_BANNED_COUNTRIES = GEO_BANNED_COUNTRIES
_BANNED_REGIONS = GEO_BANNED_REGIONS
_KEEP_COUNTRIES = GEO_KEEP_COUNTRIES_SET
_GEO_ACTIVE = GEO_ACTIVE


def _country_is_banned(country: str) -> bool:
    """True if a resolved country is hard-banned by the profile. Thin wrapper over
    the shared geo.country_banned predicate (single source of truth)."""
    return country_banned(
        country,
        banned_regions=_BANNED_REGIONS,
        keep_countries=_KEEP_COUNTRIES,
        banned_countries=_BANNED_COUNTRIES,
    )


def _entry_is_excluded_geo(loc: dict) -> bool:
    """True if a single location entry's COUNTRY is hard-banned by the profile.

    Banned = the resolved country is explicitly excluded OR sits in a banned
    world region, and is not whitelisted. Always False when no geo ban is set.
    Handles v2 entries (country/city fields) and v1 entries (free-text
    'location'): country_for_location() resolves the country from either form.
    A location whose country cannot be resolved is never excluded.
    """
    if not _GEO_ACTIVE:
        return False  # nothing banned → keep everything on geography

    # Multi-city free text (";"-joined): excluded only when EVERY part resolves
    # to a banned country. A single kept part keeps the whole entry.
    raw_text = _normalize_country(loc.get("location") or "") or _normalize_country(
        loc.get("city") or ""
    )
    if ";" in raw_text and not _normalize_country(loc.get("country") or ""):
        parts = [p.strip() for p in raw_text.split(";") if p.strip()]
        if not parts:
            return False
        return all(
            _country_is_banned(country_for_location({"location": part}) or "") for part in parts
        )

    country = country_for_location(loc)
    if not country:
        return False
    return _country_is_banned(country)


_REMOTE_MARKERS = ("remote", "worldwide", "anywhere", "global", "distributed")


def _entry_is_remote(loc: dict) -> bool:
    """True if a location entry signals remote / location-independent work.

    Protects remote postings from the geography gate: a role open remotely is
    reachable regardless of the country stamped on it, so it must never be
    dropped on geography alone (a 'Remote — United States' role may well accept
    a non-US worker with timezone overlap)."""
    if "remote" in (loc.get("work_mode") or "").lower():
        return True
    text = " ".join(str(loc.get(k) or "") for k in ("location", "city", "region")).lower()
    return any(m in text for m in _REMOTE_MARKERS)


def _all_locations_excluded(vac: dict) -> bool:
    """True if EVERY location of the vacancy is in a profile-excluded country.

    This is the single geography gate. It privileges no country — a location is
    a delete vote only when its resolved COUNTRY is exactly in the user's
    _EXCLUDED_COUNTRIES. Returns False when the user excluded no countries, when
    the vacancy has no location, when any single location is NOT excluded
    (multi-country postings that include a kept country survive), or when any
    location is remote (geography must not drop a remote-open role).
    """
    if not _GEO_ACTIVE:
        return False

    locs = vac.get("locations", [])
    if any(_entry_is_remote(loc) for loc in locs):
        return False
    if not locs:
        # Legacy: no locations array — check top-level fields.
        region = vac.get("region", "")
        loc_str = (vac.get("location") or "").lower()
        if not loc_str and not region:
            return False
        synthetic = {"location": loc_str, "region": region}
        return _entry_is_excluded_geo(synthetic)

    for loc in locs:
        if not _entry_is_excluded_geo(loc):
            return False
    return True


def _geo_delete_category(vac: dict) -> str | None:
    """Return 'delete_geo' if ALL location entries sit in profile-excluded
    countries (no country privileged), else None.

    Vacancies with no locations → kept (handled by caller).
    """
    locs = vac.get("locations", [])
    if not locs:
        return None
    return "delete_geo" if _all_locations_excluded(vac) else None


def _has_full_description(vac: dict) -> bool:
    """Check if vacancy has meaningful full_description."""
    # Check top-level
    desc = vac.get("full_description", "") or ""
    if len(desc.strip()) >= 100:
        return True
    # Check locations
    for loc in vac.get("locations", []):
        loc_desc = loc.get("full_description", "") or ""
        if len(loc_desc.strip()) >= 100:
            return True
    return False


def _full_description_length(vac: dict) -> int:
    """Get length of best full_description across vacancy and locations."""
    best = len((vac.get("full_description", "") or "").strip())
    for loc in vac.get("locations", []):
        loc_len = len((loc.get("full_description", "") or "").strip())
        best = max(best, loc_len)
    return best


def classify_vacancies(db: dict = None) -> dict:
    """Classify unscored vacancies into categories.

    Loads from Supabase. db param is accepted but ignored.
    Returns dict with category lists of (vacancy_id, vacancy) tuples.
    Categories are mutually exclusive, applied in priority order.
    """
    categories = {
        "delete_blacklist": [],
        "delete_junk": [],
        "delete_rearchived": [],
        "delete_geo": [],
        "delete_stale_blind": [],
        "reenrich_blind": [],
        "reenrich_thin": [],
        "ready": [],
    }
    today = date.today()
    all_vacs = load_vacancies(unscored_only=True)
    archive_hashes = get_archived_hashes()

    for vid, vac in all_vacs.items():
        # Only unscored vacancies
        score = vac.get("llm_score")
        if score is not None and score >= 0:
            continue

        if vac.get("status", "unseen") in _PROTECTED_STATUSES:
            continue

        title = vac.get("title", "")
        org = vac.get("org", "")
        has_url = bool(_get_vacancy_url(vac))
        has_desc = _has_full_description(vac)
        desc_len = _full_description_length(vac)

        # Priority 1: title blacklist match
        if filters.title_words_blacklisted(title):
            categories["delete_blacklist"].append((vid, vac))
            continue

        # Priority 2: content junk (reCAPTCHA, donation widgets, error pages)
        junk_reason = filters.is_content_junk(vac.get("full_description", ""))
        if junk_reason:
            vac["_junk_reason"] = junk_reason
            categories["delete_junk"].append((vid, vac))
            continue

        # Priority 3: re-archived (same org+title previously archived)
        dedup_hash = vac.get("dedup_hash", "")
        if dedup_hash and dedup_hash in archive_hashes:
            categories["delete_rearchived"].append((vid, vac))
            continue

        # Priority 4: geo — every location sits in a profile-excluded country
        geo_cat = _geo_delete_category(vac)
        if geo_cat:
            categories[geo_cat].append((vid, vac))
            continue

        # Priority 6: stale blind (has URL, no description, >7 days old)
        if has_url and not has_desc:
            first_seen = vac.get("first_seen", "")
            if first_seen:
                try:
                    seen_date = date.fromisoformat(first_seen)
                    if (today - seen_date).days > 7:
                        categories["delete_stale_blind"].append((vid, vac))
                        continue
                except ValueError:
                    pass

        # Priority 5: re-enrich blind (has URL, no description, fresh)
        if has_url and not has_desc:
            categories["reenrich_blind"].append((vid, vac))
            continue

        # Priority 6: re-enrich thin (short description)
        if desc_len > 0 and desc_len < 100:
            categories["reenrich_thin"].append((vid, vac))
            continue

        # Everything else is ready
        categories["ready"].append((vid, vac))

    return categories


# ---------------------------------------------------------------------------
# Stats aggregation
# ---------------------------------------------------------------------------


def compute_stats(categories: dict) -> dict:
    """Compute summary statistics from classified vacancies."""
    total = sum(len(v) for v in categories.values())
    delete_count = (
        len(categories["delete_blacklist"])
        + len(categories.get("delete_junk", []))
        + len(categories.get("delete_rearchived", []))
        + len(categories.get("delete_geo", []))
        + len(categories["delete_stale_blind"])
    )
    company_gated = 0  # removed: company gate handled by active/inactive status
    reenrich_count = len(categories["reenrich_blind"]) + len(categories["reenrich_thin"])
    ready_count = len(categories["ready"])

    # Ready breakdown
    ready_by_tier = Counter()
    ready_by_region = Counter()
    ready_by_relevance = Counter()
    ready_by_org = Counter()
    for _, vac in categories["ready"]:
        ready_by_tier[vac.get("tier") or "C"] += 1
        ready_by_region[vac.get("region", "other")] += 1
        ready_by_relevance[vac.get("relevance_score", 0)] += 1
        ready_by_org[vac.get("org", "Unknown")] += 1

    # Per-org table (across all categories)
    org_stats = defaultdict(
        lambda: {"tier": "C", "total": 0, "ready": 0, "delete": 0, "reenrich": 0}
    )
    delete_cats = [
        "delete_blacklist",
        "delete_junk",
        "delete_rearchived",
        "delete_geo",
        "delete_stale_blind",
    ]
    reenrich_cats = ["reenrich_blind", "reenrich_thin"]
    for cat_name, items in categories.items():
        for _, vac in items:
            org = vac.get("org", "Unknown")
            org_stats[org]["tier"] = vac.get("tier") or "C"
            org_stats[org]["total"] += 1
            if cat_name in delete_cats:
                org_stats[org]["delete"] += 1
            elif cat_name in reenrich_cats:
                org_stats[org]["reenrich"] += 1
            elif cat_name == "ready":
                org_stats[org]["ready"] += 1

    return {
        "total": total,
        "delete_count": delete_count,
        "company_gated": company_gated,
        "reenrich_count": reenrich_count,
        "ready_count": ready_count,
        "ready_by_tier": dict(
            sorted(
                ready_by_tier.items(), key=lambda x: {"S": 0, "A": 1, "B": 2, "C": 3}.get(x[0], 99)
            )
        ),
        "ready_by_region": dict(ready_by_region.most_common()),
        "ready_by_relevance": dict(sorted(ready_by_relevance.items(), reverse=True)),
        "ready_by_org": dict(ready_by_org.most_common(15)),
        "org_stats": dict(sorted(org_stats.items(), key=lambda x: (-x[1]["delete"], x[0]))),
    }


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    """Escape HTML."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _truncate(text: str, maxlen: int = 60) -> str:
    if len(text) <= maxlen:
        return text
    return text[: maxlen - 1] + "\u2026"


def generate_html_report(categories: dict, stats: dict, output_path: Path) -> Path:
    """Generate REPORT-filter.html with analysis results."""
    total = stats["total"]
    delete_count = stats["delete_count"]
    reenrich_count = stats["reenrich_count"]
    ready_count = stats["ready_count"]

    # Funnel percentages
    del_pct = (delete_count / total * 100) if total else 0
    reen_pct = (reenrich_count / total * 100) if total else 0
    ready_pct = (ready_count / total * 100) if total else 0

    # Build delete sections
    delete_sections_html = ""

    delete_cats = [
        ("delete_blacklist", "Blacklist Match", "#D4787A", "Title matched GLOBAL_BLACKLIST"),
        (
            "delete_junk",
            "Content Junk",
            "#9B59B6",
            "reCAPTCHA, donation widgets, error pages, navigation snippets",
        ),
        ("delete_rearchived", "Re-archived", "#E67E22", "Previously archived (90-day cooldown)"),
        (
            "delete_geo",
            "Excluded country",
            "#A0846B",
            "All locations in a country the profile excludes, no remote",
        ),
        ("delete_stale_blind", "Stale Blind", "#A89680", "Has URL, no description, >7 days"),
    ]
    for cat_key, cat_label, cat_color, cat_desc in delete_cats:
        items = categories[cat_key]
        if not items:
            continue
        rows = ""
        for vid, vac in sorted(items, key=lambda x: x[1].get("org", "")):
            org = _esc(vac.get("org", "?"))
            title = _esc(_truncate(vac.get("title", "?"), 55))
            locs = vac.get("locations", [])
            loc = _esc(locs[0].get("location", "?") if locs else "?")
            rows += f"""<tr>
                <td style="font-weight:500">{org}</td>
                <td>{title}</td>
                <td style="color:var(--text-secondary);font-size:13px">{loc}</td>
            </tr>\n"""
        delete_sections_html += f"""
        <details class="delete-group" style="margin-bottom:16px">
            <summary style="cursor:pointer;padding:12px 16px;background:white;border-radius:12px;
                border-left:4px solid {cat_color};box-shadow:0 2px 8px rgba(0,0,0,0.05)">
                <span style="font-weight:600">{cat_label}</span>
                <span class="badge" style="background:{cat_color}20;color:{cat_color};margin-left:8px">
                    {len(items)}
                </span>
                <span style="color:var(--text-secondary);font-size:13px;margin-left:8px">{cat_desc}</span>
            </summary>
            <div style="padding:8px 0 0">
                <table class="data-table">
                    <thead><tr>
                        <th style="width:25%">Organization</th>
                        <th style="width:45%">Position</th>
                        <th style="width:30%">Location</th>
                    </tr></thead>
                    <tbody>{rows}</tbody>
                </table>
            </div>
        </details>"""

    # Build reenrich section
    reenrich_rows = ""
    all_reenrich = categories["reenrich_blind"] + categories["reenrich_thin"]
    for vid, vac in sorted(
        all_reenrich, key=lambda x: {"S": 0, "A": 1, "B": 2, "C": 3}.get(x[1].get("tier"), 99)
    ):
        org = _esc(vac.get("org", "?"))
        title = _esc(_truncate(vac.get("title", "?"), 50))
        url = _get_vacancy_url(vac)
        url_short = _esc(_truncate(url, 40))
        kind = "blind" if (vid, vac) in categories["reenrich_blind"] else "thin"
        badge_color = "#E8915A" if kind == "blind" else "#D97706"
        reenrich_rows += f"""<tr>
            <td style="font-weight:500">{org}</td>
            <td>{title}</td>
            <td><a href="{_esc(url)}" style="color:var(--accent-deep);text-decoration:none;font-size:13px"
                   target="_blank">{url_short}</a></td>
            <td><span class="badge" style="background:{badge_color}20;color:{badge_color}">{kind}</span></td>
        </tr>\n"""

    # Ready breakdown by tier
    tier_rows = ""
    for tier, count in sorted(stats["ready_by_tier"].items()):
        tier_label = {
            "S": "S — Strategic",
            "A": "A — Strong Fit",
            "B": "B — Monitor",
            "C": "C — Low Priority",
        }.get(str(tier), f"Tier {tier}")
        tier_rows += f"<tr><td>{tier_label}</td><td style='font-weight:600'>{count}</td></tr>\n"

    # Ready breakdown by region
    region_rows = ""
    region_labels = {"europe": "Europe", "remote": "Remote", "us": "US", "other": "Other"}
    for region, count in stats["ready_by_region"].items():
        region_rows += f"<tr><td>{region_labels.get(region, region)}</td><td style='font-weight:600'>{count}</td></tr>\n"

    # Top ready orgs
    ready_org_rows = ""
    for org, count in stats["ready_by_org"].items():
        ready_org_rows += f"<tr><td>{_esc(org)}</td><td style='font-weight:600'>{count}</td></tr>\n"

    # Per-org summary table
    org_summary_rows = ""
    for org, data in stats["org_stats"].items():
        del_pct_org = (data["delete"] / data["total"] * 100) if data["total"] else 0
        style = ' style="background:#FFF0E0"' if del_pct_org > 80 else ""
        org_summary_rows += f"""<tr{style}>
            <td style="font-weight:500">{_esc(org)}</td>
            <td>{data["tier"]}</td>
            <td>{data["total"]}</td>
            <td style="color:#059669">{data["ready"]}</td>
            <td style="color:#D4787A">{data["delete"]}</td>
            <td style="color:#E8915A">{data["reenrich"]}</td>
            <td>{del_pct_org:.0f}%</td>
        </tr>\n"""

    firecrawl_credits = len(categories["reenrich_blind"]) * 5 + len(categories["reenrich_thin"]) * 5

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Filter Report — Vacancy Pipeline</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
    --bg: #FFF8F0;
    --bg-warm: #FFF0E0;
    --bg-card: #FFFFFF;
    --text: #3D2E1F;
    --text-secondary: #7A6554;
    --text-light: #A89680;
    --accent-warm: #E8915A;
    --accent-deep: #C76B35;
    --accent-sage: #8BA37A;
    --accent-lavender: #9B8AB8;
    --accent-sky: #7EB5D6;
    --accent-rose: #D4787A;
    --surface: #EDE5D8;
    --shadow-sm: 0 2px 8px rgba(0,0,0,0.06);
    --shadow-md: 0 2px 16px rgba(42,31,20,0.06);
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
}}
body::after {{
    content:''; position:fixed; inset:0; opacity:0.12; pointer-events:none; z-index:0;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='300'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='1'/%3E%3C/svg%3E");
    background-size:200px 200px;
}}
.container {{ max-width:1000px; margin:0 auto; padding:40px 24px 80px; position:relative; z-index:1; }}
.hero {{ text-align:center; padding:48px 20px 32px; }}
.hero-label {{ font-size:11px; letter-spacing:3px; text-transform:uppercase; color:var(--text-light); font-weight:500; margin-bottom:12px; }}
.hero h1 {{ font-size:42px; font-weight:300; line-height:1.2; margin-bottom:8px; }}
.hero p {{ font-size:15px; color:var(--text-secondary); }}
.stat-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin:32px 0; }}
.stat-card {{ background:var(--bg-card); border-radius:14px; padding:20px; box-shadow:var(--shadow-sm);
    border-top:3px solid var(--surface); text-align:center; }}
.stat-card .num {{ font-size:32px; font-weight:600; letter-spacing:-0.03em; }}
.stat-card .label {{ font-size:12px; color:var(--text-secondary); margin-top:4px; text-transform:uppercase; letter-spacing:0.05em; }}
.section {{ margin:48px 0; }}
.section-label {{ font-size:11px; letter-spacing:3px; text-transform:uppercase; color:var(--text-light); font-weight:500; margin-bottom:8px; }}
.section h2 {{ font-size:26px; font-weight:400; margin-bottom:16px; }}
.funnel {{ background:white; border-radius:14px; padding:24px; box-shadow:var(--shadow-sm); margin:24px 0; }}
.funnel-bar {{ display:flex; height:40px; border-radius:8px; overflow:hidden; margin:12px 0; }}
.funnel-seg {{ display:flex; align-items:center; justify-content:center; color:white; font-size:13px; font-weight:500; }}
.data-table {{ width:100%; border-collapse:collapse; font-size:14px; }}
.data-table thead {{ border-bottom:2px solid var(--surface); }}
.data-table th {{ text-align:left; padding:10px 12px; font-size:11px; letter-spacing:0.06em;
    text-transform:uppercase; color:var(--text-light); font-weight:500; }}
.data-table td {{ padding:8px 12px; border-bottom:1px solid #f0ebe3; }}
.data-table tbody tr:hover {{ background:#FBF8F3; }}
.card {{ background:var(--bg-card); border-radius:14px; padding:24px; box-shadow:var(--shadow-sm); margin:16px 0; }}
.badge {{ display:inline-block; border-radius:9999px; padding:2px 10px; font-size:12px; font-weight:500; }}
.mini-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin:16px 0; }}
.mini-card {{ background:white; border-radius:12px; padding:16px; box-shadow:var(--shadow-sm); }}
.mini-card h4 {{ font-size:13px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:0.05em; margin-bottom:8px; }}
.reco {{ background:linear-gradient(135deg, var(--bg-warm) 0%, #FFF5EB 100%); border-left:4px solid var(--accent-warm);
    border-radius:0 14px 14px 0; padding:20px 24px; margin:16px 0; }}
.reco h3 {{ font-size:16px; font-weight:500; margin-bottom:8px; }}
.reco ul {{ padding-left:20px; font-size:14px; color:var(--text-secondary); }}
.reco li {{ margin-bottom:4px; }}
@media (max-width:768px) {{
    .stat-grid {{ grid-template-columns:repeat(2,1fr); }}
    .mini-grid {{ grid-template-columns:1fr; }}
    .hero h1 {{ font-size:28px; }}
}}
</style>
</head>
<body>
<div class="container">
    <div class="hero">
        <div class="hero-label">Vacancy Pipeline</div>
        <h1>Filter Report</h1>
        <p>{datetime.now().strftime("%d %B %Y, %H:%M")} &mdash; {
        total
    } unscored vacancies analyzed</p>
    </div>

    <!-- Stat cards -->
    <div class="stat-grid" style="grid-template-columns:repeat(5,1fr)">
        <div class="stat-card" style="border-top-color:var(--text)">
            <div class="num">{total}</div>
            <div class="label">Unscored Total</div>
        </div>
        <div class="stat-card" style="border-top-color:#059669">
            <div class="num" style="color:#059669">{ready_count}</div>
            <div class="label">Ready to Score</div>
        </div>
        <div class="stat-card" style="border-top-color:var(--accent-rose)">
            <div class="num" style="color:var(--accent-rose)">{delete_count}</div>
            <div class="label">Delete</div>
        </div>
        <div class="stat-card" style="border-top-color:var(--accent-warm)">
            <div class="num" style="color:var(--accent-warm)">{reenrich_count}</div>
            <div class="label">Re-enrich</div>
        </div>
    </div>

    <!-- Funnel -->
    <div class="funnel">
        <div style="font-size:13px;color:var(--text-secondary);margin-bottom:8px">
            FUNNEL: {total} unscored &rarr; {delete_count} delete + {
        reenrich_count
    } re-enrich &rarr; <strong>{ready_count} ready</strong>
        </div>
        <div class="funnel-bar">
            <div class="funnel-seg" style="width:{del_pct:.1f}%;background:var(--accent-rose)">
                {f"{del_pct:.0f}%" if del_pct > 5 else ""}
            </div>
            <div class="funnel-seg" style="width:{reen_pct:.1f}%;background:var(--accent-warm)">
                {f"{reen_pct:.0f}%" if reen_pct > 5 else ""}
            </div>
            <div class="funnel-seg" style="width:{ready_pct:.1f}%;background:#059669">
                {f"{ready_pct:.0f}%" if ready_pct > 5 else ""}
            </div>
        </div>
        <div style="display:flex;gap:24px;font-size:12px;color:var(--text-secondary)">
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:var(--accent-rose);margin-right:4px"></span>Delete ({
        delete_count
    })</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:var(--accent-warm);margin-right:4px"></span>Re-enrich ({
        reenrich_count
    })</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#059669;margin-right:4px"></span>Ready ({
        ready_count
    })</span>
        </div>
    </div>

    <!-- Ready breakdown -->
    <div class="section">
        <div class="section-label">Ready to Score</div>
        <h2>Breakdown: {ready_count} vacancies</h2>
        <div class="mini-grid">
            <div class="mini-card">
                <h4>By Tier</h4>
                <table class="data-table">{tier_rows}</table>
            </div>
            <div class="mini-card">
                <h4>By Region</h4>
                <table class="data-table">{region_rows}</table>
            </div>
            <div class="mini-card">
                <h4>Top Organizations</h4>
                <table class="data-table">{ready_org_rows}</table>
            </div>
        </div>
    </div>

    <!-- Delete section -->
    <div class="section">
        <div class="section-label">Delete Candidates</div>
        <h2>{delete_count} vacancies to remove</h2>
        {
        delete_sections_html
        if delete_sections_html
        else '<p style="color:var(--text-secondary)">No delete candidates found.</p>'
    }
    </div>

    <!-- Re-enrich section -->
    <div class="section">
        <div class="section-label">Re-enrich Candidates</div>
        <h2>{reenrich_count} vacancies need content</h2>
        <p style="color:var(--text-secondary);margin-bottom:16px">
            Estimated Firecrawl credits: ~{firecrawl_credits}
            ({len(categories["reenrich_blind"])} blind + {len(categories["reenrich_thin"])} thin)
        </p>
        {
        f'''<div class="card" style="overflow-x:auto">
            <table class="data-table">
                <thead><tr>
                    <th>Organization</th>
                    <th>Position</th>
                    <th>URL</th>
                    <th>Type</th>
                </tr></thead>
                <tbody>{reenrich_rows}</tbody>
            </table>
        </div>'''
        if reenrich_rows
        else '<p style="color:var(--text-secondary)">No re-enrich candidates.</p>'
    }
    </div>

    <!-- Per-org table -->
    <div class="section">
        <div class="section-label">Per Organization</div>
        <h2>Summary by company</h2>
        <div class="card" style="overflow-x:auto">
            <table class="data-table">
                <thead><tr>
                    <th>Organization</th>
                    <th>Tier</th>
                    <th>Total</th>
                    <th>Ready</th>
                    <th>Delete</th>
                    <th>Re-enrich</th>
                    <th>Del %</th>
                </tr></thead>
                <tbody>{org_summary_rows}</tbody>
            </table>
        </div>
    </div>

    <!-- Recommendations -->
    <div class="section">
        <div class="section-label">Next Steps</div>
        <h2>Recommendations</h2>
        <div class="reco">
            <h3>Suggested pipeline</h3>
            <ul>
                {
        "<li><strong>Delete</strong> "
        + str(delete_count)
        + " irrelevant vacancies to clean the DB</li>"
        if delete_count
        else ""
    }
                {
        "<li><strong>Re-enrich</strong> "
        + str(reenrich_count)
        + " vacancies via Firecrawl (~"
        + str(firecrawl_credits)
        + " credits)</li>"
        if reenrich_count
        else ""
    }
                <li><strong>Score</strong> {
        ready_count
    } ready vacancies with <code>/score</code></li>
                <li>Run <strong><code>--suggest-blacklist</code></strong> after deletion to improve future filtering</li>
            </ul>
        </div>
    </div>
</div>
</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Delete mode
# ---------------------------------------------------------------------------


def delete_vacancies_filtered(ids_to_delete: list[str]) -> Path:
    """Archive and remove specified vacancies from Supabase.

    Writes local JSON archive before deleting.
    Returns path to the archive file.
    """
    from db_backend import RealDictCursor

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Fetch vacancy data for archive
    cur.execute(
        "SELECT v.*, c.canonical_name AS org FROM vacancy v JOIN company c ON v.company_id = c.id WHERE v.id = ANY(%s::uuid[])",
        (ids_to_delete,),
    )
    rows = cur.fetchall()
    if not rows:
        print("No matching vacancy IDs found in DB.")
        sys.exit(1)

    found = {}
    for r in rows:
        uid = str(r["id"])
        vac = dict(r)
        vac["id"] = uid
        vac["company_id"] = str(vac["company_id"])
        for df in ("first_seen", "last_seen", "deadline"):
            if isinstance(vac.get(df), date):
                vac[df] = vac[df].isoformat()
        for df in ("llm_scored_at", "status_updated_at", "created_at", "updated_at"):
            if isinstance(vac.get(df), datetime):
                vac[df] = vac[df].isoformat()
        found[uid] = vac

    # Save archive
    archive_dir = VACANCIES_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    archive_path = archive_dir / f"filter_{ts}.json"

    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "archived_at": datetime.now().isoformat(),
                "source": "filter",
                "count": len(found),
                "vacancies": found,
            },
            f,
            indent=2,
            ensure_ascii=False,
            default=str,
        )

    # Record archived hashes for dedup (prevents re-fetch → re-score → re-archive)
    archive_entries = [
        (v.get("dedup_hash"), "filter_deleted") for v in found.values() if v.get("dedup_hash")
    ]
    record_archived_hashes(archive_entries)

    # Delete from Supabase
    count = db_delete_vacancies(ids_to_delete)
    conn.commit()
    cur.close()

    print(f"Deleted {count} vacancies")
    print(f"Archive: {archive_path.name}")
    return archive_path


# ---------------------------------------------------------------------------
# Blacklist suggestions
# ---------------------------------------------------------------------------


def suggest_blacklist() -> dict:
    """Analyze recent filter archives and suggest blacklist patterns.

    Returns dict with title_patterns and company_observations.
    """
    archive_dir = VACANCIES_DIR / "archive"
    filter_archives = sorted(archive_dir.glob("filter_*.json"), reverse=True)

    if not filter_archives:
        print("No filter archives found. Run --delete-ids first.")
        return {"title_patterns": [], "company_observations": []}

    # Read the latest filter archive
    latest = filter_archives[0]
    with open(latest, "r", encoding="utf-8") as f:
        data = json.load(f)

    vacancies = data.get("vacancies", {})
    if not vacancies:
        print(f"Archive {latest.name} has no vacancies.")
        return {"title_patterns": [], "company_observations": []}

    # Tokenize titles into bigrams and trigrams
    bl_lower = {kw.lower() for kw in GLOBAL_BLACKLIST}
    pattern_orgs = defaultdict(lambda: defaultdict(int))  # pattern -> org -> count

    for vid, vac in vacancies.items():
        title = vac.get("title", "").lower()
        org = vac.get("org", "Unknown")
        words = re.findall(r"[a-z]+", title)

        # Generate bigrams and trigrams
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                ngram = " ".join(words[i : i + n])
                # Skip if already covered by existing blacklist
                if any(bl in ngram or ngram in bl for bl in bl_lower):
                    continue
                pattern_orgs[ngram][org] += 1

    # Filter: patterns appearing 3+ times across 2+ orgs
    title_patterns = []
    for pattern, orgs in sorted(pattern_orgs.items(), key=lambda x: -sum(x[1].values())):
        total_count = sum(orgs.values())
        unique_orgs = len(orgs)
        if total_count >= 3 and unique_orgs >= 2:
            org_breakdown = ", ".join(
                f"{org}\u00d7{cnt}" for org, cnt in sorted(orgs.items(), key=lambda x: -x[1])
            )
            title_patterns.append(
                {
                    "pattern": pattern,
                    "count": total_count,
                    "orgs": unique_orgs,
                    "breakdown": org_breakdown,
                }
            )

    # Company observations: orgs with >80% deletion rate
    org_counts = Counter()
    org_deleted = Counter()
    all_vacs = load_vacancies(include_inactive_companies=True)
    # Count total unscored per org (before deletion)
    for vid, vac in {**all_vacs, **vacancies}.items():
        org = vac.get("org", "Unknown")
        org_counts[org] += 1
    for vid, vac in vacancies.items():
        org = vac.get("org", "Unknown")
        org_deleted[org] += 1

    company_observations = []
    for org, deleted in org_deleted.most_common():
        total = org_counts.get(org, deleted)
        if total >= 5 and deleted / total > 0.8:
            company_observations.append(
                {
                    "org": org,
                    "deleted": deleted,
                    "total": total,
                    "pct": deleted / total * 100,
                }
            )

    # Print suggestions
    print(f"\nBLACKLIST SUGGESTIONS (from {len(vacancies)} deleted vacancies in {latest.name}):\n")

    if title_patterns:
        print("  Title patterns (appear 3+ times across 2+ orgs):")
        for p in title_patterns[:15]:
            print(f'    "{p["pattern"]}" \u2014 {p["count"]} matches ({p["breakdown"]})')
    else:
        print("  No new title patterns found.")

    if company_observations:
        print("\n  Company-level observations:")
        for c in company_observations:
            print(
                f"    {c['org']}: {c['deleted']}/{c['total']} deleted ({c['pct']:.0f}%) \u2014 consider postponing"
            )
    else:
        print("\n  No company-level observations.")

    return {
        "title_patterns": title_patterns[:15],
        "company_observations": company_observations,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    args = build_parser().parse_args()

    from db_backend import print_backend_banner

    print_backend_banner()

    if args.suggest_blacklist:
        result = suggest_blacklist()
        print("\n" + json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.delete_ids:
        ids = [x.strip() for x in args.delete_ids.split(",") if x.strip()]
        delete_vacancies_filtered(ids)
        return

    # Optional: dedup pre-step (DB-mutating, must be explicit)
    dedup_result = None
    fuzzy_dupes = []
    if args.dedup:
        print("Running dedup pre-step...", file=sys.stderr, flush=True)
        dedup_result = _clean_exact_dupes()
        if dedup_result.get("merged", 0) > 0:
            print(
                f"  Exact dupes cleaned: {dedup_result['merged']} "
                f"(examined {dedup_result['examined']} groups, "
                f"{dedup_result.get('protected', 0)} protected)",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"  No exact dupes found ({dedup_result['examined']} groups examined)",
                file=sys.stderr,
                flush=True,
            )

        # Fuzzy dedup on remaining unscored vacancies
        all_vacs = load_vacancies(unscored_only=True)
        fuzzy_dupes = _find_fuzzy_dupes(all_vacs)
        if fuzzy_dupes:
            print(
                f"  Fuzzy dupes found: {len(fuzzy_dupes)} pairs (quarantined for review)",
                file=sys.stderr,
                flush=True,
            )
        else:
            print("  No fuzzy dupes found", file=sys.stderr, flush=True)

    # Default: analyze + report + JSON
    categories = classify_vacancies()
    stats = compute_stats(categories)

    # --- Structured filter summary (for feedback loop) ---
    print("\n--- Filter Summary ---", file=sys.stderr, flush=True)
    for cat in [
        "delete_blacklist",
        "delete_junk",
        "delete_rearchived",
        "delete_geo",
        "delete_stale_blind",
    ]:
        count = len(categories.get(cat, []))
        if count:
            print(f"  {cat}: {count}", file=sys.stderr, flush=True)
    junk_items = categories.get("delete_junk", [])
    if junk_items:
        junk_by_company = Counter(v.get("org", "?") for _, v in junk_items)
        junk_by_reason = Counter(v.get("_junk_reason", "?") for _, v in junk_items)
        print(
            f"  Junk by company: {dict(junk_by_company.most_common(5))}",
            file=sys.stderr,
            flush=True,
        )
        print(f"  Junk by reason: {dict(junk_by_reason)}", file=sys.stderr, flush=True)
    rearchived = categories.get("delete_rearchived", [])
    if rearchived:
        rearch_by_company = Counter(v.get("org", "?") for _, v in rearchived)
        print(
            f"  Re-archived by company: {dict(rearch_by_company.most_common(5))}",
            file=sys.stderr,
            flush=True,
        )
    print(f"  Ready to score: {stats['ready_count']}", file=sys.stderr, flush=True)
    print("--- End Filter Summary ---\n", file=sys.stderr, flush=True)

    report_path = Path("REPORT-filter.html")
    generate_html_report(categories, stats, report_path)

    # Collect all delete IDs by category
    delete_ids = {}
    for cat in [
        "delete_blacklist",
        "delete_junk",
        "delete_rearchived",
        "delete_geo",
        "delete_stale_blind",
    ]:
        delete_ids[cat] = [vid for vid, _ in categories.get(cat, [])]

    reenrich_ids = {
        "blind": [vid for vid, _ in categories["reenrich_blind"]],
        "thin": [vid for vid, _ in categories["reenrich_thin"]],
    }

    output = {
        "total_unscored": stats["total"],
        "categories": {cat: len(items) for cat, items in categories.items()},
        "delete_ids": delete_ids,
        "reenrich_ids": reenrich_ids,
        "ready": stats["ready_count"],
        "report_path": str(report_path),
    }
    if dedup_result:
        output["dedup"] = dedup_result
    if fuzzy_dupes:
        output["fuzzy_dupes"] = fuzzy_dupes

    print(json.dumps(output, indent=2, ensure_ascii=False))

    from database_supabase import validate_db

    validate_db()


if __name__ == "__main__":
    main()
