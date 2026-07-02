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

    Keys: llm_score_threshold, tier_s/a/b/c, composite_alignment_weight,
    composite_boost_weight, auto_discovery_status,
    auto_review {enabled, approve, reject}.
    """
    sec = _section("thresholds")
    auto = sec.get("auto_review", {}) if isinstance(sec.get("auto_review"), dict) else {}
    return {
        "llm_score_threshold": int(_num(sec, "llm_score_threshold", 20)),
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
    """Digest defaults (env vars still override at the call site)."""
    sec = _section("digest")
    return {
        "hot_vacancy_score": int(_num(sec, "hot_vacancy_score", 55)),
        "deadline_soon_days": int(_num(sec, "deadline_soon_days", 7)),
        "default_limit": int(_num(sec, "default_limit", 5)),
        "default_min_score": int(_num(sec, "default_min_score", 40)),
        "summary_fallback_chars": int(_num(sec, "summary_fallback_chars", 600)),
        "summary_max_chars": int(_num(sec, "summary_max_chars", 1500)),
        "message_max_chars": int(_num(sec, "message_max_chars", 4000)),
    }


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
