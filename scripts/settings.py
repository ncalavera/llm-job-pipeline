"""Settings loader — the single reader of ``config/defaults.toml``.

The pipeline keeps every tunable list, threshold, keyword, geo mapping and board
definition in ``config/defaults.toml`` (tool mechanics + neutral defaults) and
``config/user_profile.md`` (personal taste). This module is the ONLY place that
parses the TOML, exposing typed accessors that the rest of the code calls.
``scripts/config.py`` re-exports the resulting names so existing wide imports
keep working — but the DATA lives in TOML, not in Python.

Read with the stdlib ``tomllib`` (Python 3.11+). The loader only READS; nobody
writes TOML at runtime.

Robustness contract: a missing file, missing section or missing key NEVER
raises. Each accessor falls back to a documented neutral default (an empty list,
or today's sensible number). So a deleted/garbled ``defaults.toml`` degrades the
pipeline to "filter nothing extra, keep today's thresholds", never a crash.

Override the config path with the ``DEFAULTS_TOML_PATH`` env var (used by tests
to point at a temp file).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _PROJECT_ROOT / "config" / "defaults.toml"

# Cache keyed by the resolved path string so tests that monkeypatch the path get
# a fresh parse instead of a stale cached dict.
_cache: dict[str, dict] = {}


def defaults_path() -> Path:
    """Resolve the defaults.toml path (env DEFAULTS_TOML_PATH wins)."""
    override = os.environ.get("DEFAULTS_TOML_PATH")
    return Path(override) if override else _DEFAULT_PATH


def load_defaults() -> dict:
    """Return the parsed ``defaults.toml`` as a dict (cached per path).

    Missing or unreadable file → empty dict (every accessor then falls back to
    its neutral default). Never raises.
    """
    path = defaults_path()
    key = str(path)
    if key in _cache:
        return _cache[key]
    data: dict = {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        data = {}
    _cache[key] = data
    return data


def clear_cache() -> None:
    """Drop the parse cache (tests reload under a different TOML path)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _section(*names: str) -> dict:
    """Walk nested TOML tables; return {} if any level is missing/non-dict."""
    node: Any = load_defaults()
    for name in names:
        if not isinstance(node, dict):
            return {}
        node = node.get(name, {})
    return node if isinstance(node, dict) else {}


def _list(section: dict, key: str) -> list:
    val = section.get(key)
    return list(val) if isinstance(val, list) else []


def _num(section: dict, key: str, fallback: float) -> float:
    val = section.get(key)
    return val if isinstance(val, (int, float)) and not isinstance(val, bool) else fallback


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def thresholds() -> dict:
    """Numeric thresholds with neutral fallbacks (today's numbers).

    Keys: llm_score_threshold, board_stale_days, tier_s/a/b/c, composite_alignment_weight,
    composite_boost_weight, auto_discovery_status,
    auto_review {enabled, approve, reject}.
    """
    sec = _section("thresholds")
    auto = sec.get("auto_review", {}) if isinstance(sec.get("auto_review"), dict) else {}
    return {
        "llm_score_threshold": int(_num(sec, "llm_score_threshold", 20)),
        "board_stale_days": int(_num(sec, "board_stale_days", 14)),
        "tier_s": int(_num(sec, "tier_s", 65)),
        "tier_a": int(_num(sec, "tier_a", 50)),
        "tier_b": int(_num(sec, "tier_b", 35)),
        "tier_c": int(_num(sec, "tier_c", 0)),
        "composite_alignment_weight": float(_num(sec, "composite_alignment_weight", 0.70)),
        "composite_boost_weight": float(_num(sec, "composite_boost_weight", 0.30)),
        "auto_discovery_status": str(sec.get("auto_discovery_status", "candidate")),
        "auto_review": {
            "enabled": bool(auto.get("enabled", False)),
            "approve": int(_num(auto, "approve", 60)),
            "reject": int(_num(auto, "reject", 25)),
        },
    }


# ---------------------------------------------------------------------------
# Scoring cost/quality knobs
# ---------------------------------------------------------------------------


_DEFAULT_EVIDENCE_CAP = 25000
# Below this, the assembler emits evidence blocks with only "### SOURCE:"
# labels and near-zero content — a company would be silently scored on
# nothing. Treated as misconfiguration: reset to the default instead of
# honoring it.
_MIN_EVIDENCE_CAP = 1000


def scoring() -> dict:
    """Scoring cost/quality knobs. Neutral fallbacks (today's numbers).

    Keys:
      company_evidence_char_cap — max chars of company_evidence text sent to the
      WANT scorer per company (across ALL sources, labels included). This is the
      single dial trading input cost against evidence completeness: higher keeps
      more of each source's tail, lower cuts tokens per scoring call. The
      assembler trims each source proportionally by its share, keeping every
      source's HEAD (where the material anchors — funding, HQ, remote policy,
      leadership — sit), so a lower cap loses tails first. Default 25000. A
      non-numeric value falls back to the default; a numeric value below
      ``_MIN_EVIDENCE_CAP`` (1000) is also treated as misconfiguration and
      falls back to the default.
    """
    sec = _section("scoring")
    cap = int(_num(sec, "company_evidence_char_cap", _DEFAULT_EVIDENCE_CAP))
    if cap < _MIN_EVIDENCE_CAP:
        cap = _DEFAULT_EVIDENCE_CAP
    return {
        "company_evidence_char_cap": cap,
    }


# ---------------------------------------------------------------------------
# Volume — the single "how many vacancies do I want to see?" window.
# ---------------------------------------------------------------------------

# Neutral fallbacks (today's numbers) so a deleted [volume] section keeps the
# pipeline running at its historical volume. daily_scoring_limit mirrors
# scoring_settings.DEFAULT_MAX_PER_RUN; digest_size mirrors the old
# [digest] default_limit.
_VOLUME_DEFAULTS = {
    "max_active_companies": 200,
    "daily_scoring_limit": 150,
    "digest_size": 5,
}


def screening() -> dict:
    """The [screening] dials: ``pilot_limit`` — roles prepared per
    night. Non-positive / non-numeric falls back to 50; never raises."""
    sec = _section("screening")
    limit = int(_num(sec, "pilot_limit", 50))
    return {"pilot_limit": limit if limit > 0 else 50}


def volume() -> dict:
    """The [volume] dials with neutral fallbacks. Never raises.

    Keys (each wired to a real lever, see config/defaults.toml [volume]):
      max_active_companies — per-run cap on active companies fetched.
      daily_scoring_limit  — default scoring cap (profile ## VOLUME overrides).
      digest_size          — default Telegram digest size.

    A non-positive or non-numeric value falls back to the neutral default: a
    volume dial must never resolve to "do nothing" or crash a run.
    """
    sec = _section("volume")
    out = {}
    for key, default in _VOLUME_DEFAULTS.items():
        val = int(_num(sec, key, default))
        out[key] = val if val > 0 else default
    return out


# ---------------------------------------------------------------------------
# Dashboard presentation knobs
# ---------------------------------------------------------------------------


def dashboard() -> dict:
    """Dashboard presentation knobs (env vars override at the call site).

    Keys: style ("illustrated"|"minimal"), language ("en"|"ru"|...),
    illustration_pack (pack name, "default" = committed generic art).
    Neutral fallbacks so a missing section degrades to English + default pack.
    """
    sec = _section("dashboard")
    return {
        "style": str(sec.get("style", "illustrated")),
        "language": str(sec.get("language", "en")),
        "illustration_pack": str(sec.get("illustration_pack", "default")),
    }


# ---------------------------------------------------------------------------
# Junk / blacklist data
# ---------------------------------------------------------------------------


def junk() -> dict:
    """Universal-junk lists. Empty fallbacks → nothing extra filtered.

    Keys: words (word-boundary), substr (stem), desc_substr (description).
    """
    sec = _section("junk")
    return {
        "words": _list(sec, "words"),
        "substr": _list(sec, "substr"),
        "desc_substr": _list(sec, "desc_substr"),
    }


# ---------------------------------------------------------------------------
# Region keyword buckets (display-only `region` stamping). Empty by default.
# ---------------------------------------------------------------------------


def region_keywords() -> dict:
    """{europe, us, remote} keyword lists. Empty fallbacks (no region privileged)."""
    sec = _section("regions")
    return {
        "europe": _list(sec, "europe"),
        "us": _list(sec, "us"),
        "remote": _list(sec, "remote"),
    }


# ---------------------------------------------------------------------------
# Geo classification DATA (logic stays in geo.py)
# ---------------------------------------------------------------------------


def geo_country_map() -> dict[str, set]:
    """bucket → set of country names. Empty sets if missing."""
    sec = _section("geo", "countries")
    return {
        "uk": set(_list(sec, "uk")),
        "de": set(_list(sec, "de")),
        "europe": set(_list(sec, "europe")),
        "us": set(_list(sec, "us")),
        "other": set(_list(sec, "other")),
    }


def geo_city_map() -> dict[str, set]:
    """bucket → set of city names. Empty sets if missing."""
    sec = _section("geo", "cities")
    return {
        "uk": set(_list(sec, "uk")),
        "de": set(_list(sec, "de")),
        "europe": set(_list(sec, "europe")),
        "us": set(_list(sec, "us")),
        "other": set(_list(sec, "other")),
    }


def geo_city_country() -> dict:
    """city → country name map for parse_location(). {} if missing."""
    sec = _section("geo", "city_country")
    return {str(k): str(v) for k, v in sec.items()} if sec else {}


def geo_work_mode() -> dict[str, set]:
    """{remote, hybrid} keyword sets for parse_location(). Empty if missing."""
    sec = _section("geo", "work_mode")
    return {
        "remote": set(_list(sec, "remote")),
        "hybrid": set(_list(sec, "hybrid")),
    }


def geo_country_region() -> dict:
    """country name (lowercased) → world region id. {} if missing.

    Structural world data (which region a country is in). The user profile,
    not this map, decides which regions to ban or penalise.
    """
    sec = _section("geo", "country_region")
    return {str(k).lower().strip(): str(v).lower().strip() for k, v in sec.items()} if sec else {}


def geo_country_aliases() -> dict:
    """alias (lowercased) → canonical country name (lowercased). {} if missing.

    Lets EXACT country exclusion treat synonyms of one country as equal while
    keeping distinct countries distinct.
    """
    sec = _section("geo", "country_aliases")
    return {str(k).lower().strip(): str(v).lower().strip() for k, v in sec.items()} if sec else {}


# ---------------------------------------------------------------------------
# Parsing heuristics
# ---------------------------------------------------------------------------


def parsing_location_hint_cities() -> list:
    """City tokens for the markdown location EXTRACTOR (never a filter). []."""
    return _list(_section("parsing"), "location_hint_cities")


# ---------------------------------------------------------------------------
# Telegram digest
# ---------------------------------------------------------------------------


def digest() -> dict:
    """Digest defaults (env vars still override at the call site).

    ``default_limit`` is the digest SIZE — it comes from the single volume
    window ([volume] digest_size), not from a key in [digest], so "how many
    vacancies do I see" lives in one place.
    """
    sec = _section("digest")
    return {
        "hot_vacancy_score": int(_num(sec, "hot_vacancy_score", 55)),
        "deadline_soon_days": int(_num(sec, "deadline_soon_days", 7)),
        "default_limit": volume()["digest_size"],
        "mid_min_score": int(_num(sec, "mid_min_score", 40)),
        "dropped_max_lines": int(_num(sec, "dropped_max_lines", 25)),
        "summary_fallback_chars": int(_num(sec, "summary_fallback_chars", 600)),
        "summary_max_chars": int(_num(sec, "summary_max_chars", 1500)),
        "message_max_chars": int(_num(sec, "message_max_chars", 4000)),
    }


# ---------------------------------------------------------------------------
# Nightly unattended run
# ---------------------------------------------------------------------------

_NIGHTLY_DEFAULTS = {
    "max_items_per_night": 120,
    "company_gate_minutes": 30,
    "vacancy_gate_minutes": 120,
    "run_deadline_minutes": 225,
    "max_turns": 250,
}


def nightly() -> dict:
    """[nightly] knobs for scripts/nightly_run.py. Neutral fallbacks; never
    raises. Minute values stay floats (fractions are legal — tests use them);
    the item/turn caps are ints. A non-positive value falls back — a night
    dial must never resolve to "do nothing"."""
    sec = _section("nightly")
    out: dict = {}
    for key, default in _NIGHTLY_DEFAULTS.items():
        val = _num(sec, key, default)
        out[key] = val if val > 0 else default
    out["max_items_per_night"] = int(out["max_items_per_night"])
    out["max_turns"] = int(out["max_turns"])
    return out


def nightly_paused_until() -> str:
    """``[nightly] paused_until`` as a bare ``YYYY-MM-DD`` string, or "" when
    unset. Kept out of ``nightly()`` because every knob there is numeric and
    falls back when non-positive. ``NIGHTLY_PAUSED_UNTIL`` in the environment
    wins, so a pause can be lifted without editing a tracked file."""
    env = (os.environ.get("NIGHTLY_PAUSED_UNTIL") or "").strip()
    if env:
        return env
    val = _section("nightly").get("paused_until")
    return str(val).strip() if isinstance(val, str) else ""


# ---------------------------------------------------------------------------
# Job boards
# ---------------------------------------------------------------------------


def boards() -> dict:
    """All defined boards as {board_id: cfg dict}. {} if none defined.

    Each cfg is returned as a plain dict so callers may mutate freely. A
    ``board_blacklist`` key is always present (defaulting to []), so every board
    ships neutral even if the TOML omitted it.
    """
    sec = _section("boards")
    out: dict[str, dict] = {}
    for board_id, cfg in sec.items():
        if not isinstance(cfg, dict):
            continue
        cfg = dict(cfg)
        cfg.setdefault("board_blacklist", [])
        out[board_id] = cfg
    return out
