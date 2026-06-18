"""Tests for the settings loader (scripts/settings.py).

Covers: it reads defaults.toml; a missing file / section / key degrades to the
documented neutral fallback (never crashes); types are correct; and a temp
defaults.toml drives the loader's output (proving config is data, not code).
"""

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    settings.clear_cache()
    yield
    settings.clear_cache()


def _point_at(monkeypatch, path: Path):
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(path))
    settings.clear_cache()


# ---------------------------------------------------------------------------
# Reads the shipped defaults.toml
# ---------------------------------------------------------------------------

def test_loads_shipped_defaults_non_empty():
    data = settings.load_defaults()
    assert isinstance(data, dict) and data, "shipped defaults.toml should parse non-empty"


def test_thresholds_types_and_values():
    t = settings.thresholds()
    assert isinstance(t["llm_score_threshold"], int)
    assert t["llm_score_threshold"] == 20
    assert t["tier_s"] > t["tier_a"] > t["tier_b"] >= t["tier_c"]
    assert isinstance(t["auto_review"], dict)
    assert t["auto_review"]["enabled"] is False  # opt-in


def test_junk_lists_are_lists_and_neutral():
    j = settings.junk()
    assert isinstance(j["words"], list) and j["words"], "universal junk words present"
    assert isinstance(j["substr"], list)
    assert isinstance(j["desc_substr"], list)


def test_regions_ship_empty():
    r = settings.region_keywords()
    assert r["europe"] == [] and r["us"] == [] and r["remote"] == []


def test_geo_maps_are_sets():
    countries = settings.geo_country_map()
    cities = settings.geo_city_map()
    assert isinstance(countries["uk"], set) and "united kingdom" in countries["uk"]
    assert isinstance(cities["us"], set) and "new york" in cities["us"]
    assert isinstance(settings.geo_city_country(), dict)


def test_boards_have_empty_blacklist():
    boards = settings.boards()
    assert boards, "shipped boards present"
    for cfg in boards.values():
        assert cfg.get("board_blacklist") == [], "every shipped board ships neutral"
    assert boards["hn_whoishiring"]["ttl_days"] == 30


# ---------------------------------------------------------------------------
# Robustness — missing file / section / key → documented neutral fallback
# ---------------------------------------------------------------------------

def test_missing_file_falls_back_to_neutral(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path / "does_not_exist.toml")
    assert settings.load_defaults() == {}
    # Each accessor returns its documented neutral default, no crash.
    assert settings.thresholds()["llm_score_threshold"] == 20
    assert settings.junk()["words"] == []
    assert settings.region_keywords()["europe"] == []
    assert settings.boards() == {}
    assert settings.geo_country_map()["uk"] == set()
    assert settings.parsing_location_hint_cities() == []
    assert settings.digest()["hot_vacancy_score"] == 55


def test_missing_section_falls_back(monkeypatch, tmp_path):
    p = tmp_path / "defaults.toml"
    p.write_text("[thresholds]\nllm_score_threshold = 5\n", encoding="utf-8")
    _point_at(monkeypatch, p)
    # thresholds present...
    assert settings.thresholds()["llm_score_threshold"] == 5
    # ...other sections missing → neutral fallback, not a crash.
    assert settings.junk()["words"] == []
    assert settings.boards() == {}


def test_malformed_toml_does_not_crash(monkeypatch, tmp_path):
    p = tmp_path / "defaults.toml"
    p.write_text("this is = = not valid toml [[[", encoding="utf-8")
    _point_at(monkeypatch, p)
    assert settings.load_defaults() == {}
    assert settings.thresholds()["llm_score_threshold"] == 20


# ---------------------------------------------------------------------------
# A temp defaults.toml drives the loader (config is data, not code)
# ---------------------------------------------------------------------------

def test_temp_toml_overrides_values(monkeypatch, tmp_path):
    p = tmp_path / "defaults.toml"
    p.write_text(textwrap.dedent("""
        [thresholds]
        llm_score_threshold = 42
        tier_s = 90

        [junk]
        words = ["custom junk"]

        [boards.myboard]
        strategy = "algolia_api"
        name = "My Board"
        url = "https://example.org"
        ttl_days = 9
    """).strip() + "\n", encoding="utf-8")
    _point_at(monkeypatch, p)

    assert settings.thresholds()["llm_score_threshold"] == 42
    assert settings.thresholds()["tier_s"] == 90
    assert settings.junk()["words"] == ["custom junk"]
    boards = settings.boards()
    assert set(boards) == {"myboard"}
    assert boards["myboard"]["ttl_days"] == 9
    # board_blacklist auto-defaulted to [] even though the TOML omitted it.
    assert boards["myboard"]["board_blacklist"] == []
