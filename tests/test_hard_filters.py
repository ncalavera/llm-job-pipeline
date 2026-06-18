"""Tests for the profile-driven HARD filters.

HARD filters are the deterministic, pre-score row drops. Geography and
title-discipline exclusions come from the ``## HARD_FILTERS`` section of the
user profile and are EMPTY by default. These tests prove:

  1. The parser reads exclude_countries / exclude_title_keywords robustly
     (missing file, missing section, "(none)" -> empty).
  2. With an EMPTY profile, an engineering / US-only vacancy SURVIVES the
     pre-score filter.
  3. With the keywords/countries listed, the SAME vacancy is DROPPED.

Because config.py and the filter compute their constants at import time, the
end-to-end tests reload those modules under a temporary USER_PROFILE_PATH.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = str((Path(__file__).resolve().parent.parent / "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


# ---------------------------------------------------------------------------
# Parser unit tests (no module reloading needed)
# ---------------------------------------------------------------------------

import hard_filters as hf


def _write_profile(tmp_path: Path, hard_filters_block: str) -> Path:
    """Write a minimal profile file with the given HARD_FILTERS body.

    Built without indentation so the ``## SECTION`` headers stay at column 0
    (the profile parser requires that).
    """
    profile = tmp_path / "user_profile.md"
    profile.write_text(
        "## USER_PROFILE\n\n"
        "Test person.\n\n"
        "## HARD_FILTERS\n\n"
        f"{hard_filters_block}\n\n"
        "## OUTPUT_LANGUAGE\n\n"
        "English\n",
        encoding="utf-8",
    )
    return profile


def test_parse_none_placeholder_is_empty():
    parsed = hf._parse_section(
        "exclude_countries: (none)\nexclude_title_keywords: (none)"
    )
    assert parsed == {"exclude_countries": [], "exclude_title_keywords": []}


def test_parse_lists_lowercased_and_trimmed():
    parsed = hf._parse_section(
        "exclude_countries: United States, Canada\n"
        "exclude_title_keywords:  Engineer ,Developer "
    )
    assert parsed["exclude_countries"] == ["united states", "canada"]
    assert parsed["exclude_title_keywords"] == ["engineer", "developer"]


def test_parse_missing_fields_are_empty():
    assert hf._parse_section("") == {
        "exclude_countries": [],
        "exclude_title_keywords": [],
    }


def test_parse_ignores_html_comment_examples():
    # The example countries inside an HTML comment must NOT be parsed as real.
    body = (
        "<!-- example: exclude_countries: france, spain -->\n"
        "exclude_countries: (none)\n"
        "exclude_title_keywords: (none)\n"
    )
    parsed = hf._parse_section(body)
    assert parsed == {"exclude_countries": [], "exclude_title_keywords": []}


def test_load_missing_profile_returns_empty(monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", "/nonexistent/profile/does_not_exist.md")
    importlib.reload(hf)
    assert hf.load_hard_filters() == {
        "exclude_countries": [],
        "exclude_title_keywords": [],
    }


# ---------------------------------------------------------------------------
# End-to-end: reload config + filter under a temp profile
# ---------------------------------------------------------------------------

# Engineering vacancy in the US only — the historical owner default dropped it.
ENG_US_VAC = {
    "title": "Senior Software Engineer",
    "locations": [
        {"country": "United States", "region": "americas", "city": "New York",
         "work_mode": "onsite"},
    ],
}


def _reload_pipeline(profile_path: Path):
    """Reload the import chain so module-level filter constants pick up the
    profile at ``profile_path``. Returns (config, filter_vacancies, db) modules.
    """
    os.environ["USER_PROFILE_PATH"] = str(profile_path)
    import prompts
    import hard_filters
    import config
    import database_supabase
    import filter_vacancies
    # Reload in dependency order.
    importlib.reload(prompts)
    importlib.reload(hard_filters)
    importlib.reload(config)
    importlib.reload(database_supabase)
    importlib.reload(filter_vacancies)
    return config, filter_vacancies, database_supabase


@pytest.fixture
def restore_default_profile():
    """Restore the import chain to the default profile after the test."""
    saved = os.environ.get("USER_PROFILE_PATH")
    yield
    if saved is None:
        os.environ.pop("USER_PROFILE_PATH", None)
    else:
        os.environ["USER_PROFILE_PATH"] = saved
    for mod in ("prompts", "hard_filters", "config",
                "database_supabase", "filter_vacancies"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])


def test_empty_profile_keeps_engineering_and_us(tmp_path, restore_default_profile):
    profile = _write_profile(
        tmp_path,
        "exclude_countries: (none)\nexclude_title_keywords: (none)",
    )
    config, filter_vacancies, db = _reload_pipeline(profile)

    assert config.EXCLUDE_COUNTRIES == []
    assert config.EXCLUDE_TITLE_KEYWORDS == []
    # Engineering title is NOT dropped on title.
    assert db._is_blacklisted("Senior Software Engineer") is False
    # US-only vacancy is NOT dropped on geography (no country excluded).
    assert filter_vacancies._all_locations_excluded(ENG_US_VAC) is False


def test_profile_with_filters_drops_engineering_and_us(tmp_path, restore_default_profile):
    profile = _write_profile(
        tmp_path,
        "exclude_countries: united states, canada\n"
        "exclude_title_keywords: engineer, developer",
    )
    config, filter_vacancies, db = _reload_pipeline(profile)

    assert config.EXCLUDE_COUNTRIES == ["united states", "canada"]
    assert "engineer" in config.EXCLUDE_TITLE_KEYWORDS
    # Engineering title IS now dropped on title.
    assert db._is_blacklisted("Senior Software Engineer") is True
    # US-only vacancy IS now dropped on geography (US listed in profile).
    assert filter_vacancies._all_locations_excluded(ENG_US_VAC) is True
    # A non-excluded, non-engineering role still survives.
    assert db._is_blacklisted("Head of Operations") is False
    assert filter_vacancies._all_locations_excluded({
        "locations": [{"country": "Germany", "city": "Berlin"}]
    }) is False
    # Universal junk is still dropped regardless of profile.
    assert db._is_blacklisted("Talent Pool — Future Roles") is True
    # A format role NOT in the profile keywords still survives.
    assert db._is_blacklisted("Volunteer Coordinator") is False


def test_excluded_country_keeps_multi_country_posting(tmp_path, restore_default_profile):
    """A posting that lists an excluded country AND a kept country survives."""
    profile = _write_profile(
        tmp_path,
        "exclude_countries: united states\nexclude_title_keywords: (none)",
    )
    _config, filter_vacancies, _db = _reload_pipeline(profile)
    vac = {"locations": [
        {"country": "United States", "city": "New York"},
        {"country": "United Kingdom", "city": "London"},
    ]}
    assert filter_vacancies._all_locations_excluded(vac) is False
