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

Absorbs, in order:
  * test_geo_silent_filter.py — the two "silent no-op" geo-filter bugs: a
    misspelled/prose region id that never matches any world region, and a
    mistyped ``## HARD_FILTERS`` heading that voids the whole section. Both
    must now WARN LOUDLY instead of silently doing nothing.
  * test_company_title_filters.py — the per-company title INCLUDE-filters
    (``## COMPANY_TITLE_FILTERS``), the opposite mechanism: keeps a
    high-volume company active but lets only profile-relevant titles through.

NAME COLLISION: this file and test_company_title_filters.py both defined
``test_parse_ignores_html_comment_examples``. The one absorbed from
test_company_title_filters.py is renamed
``test_parse_company_title_ignores_html_comment_examples`` below; this file's
own ``test_parse_ignores_html_comment_examples`` (HARD_FILTERS parser) is
unchanged.
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
    parsed = hf._parse_section("exclude_countries: (none)\nexclude_title_keywords: (none)")
    assert parsed == dict(hf._GEO_DEFAULTS)


def test_parse_lists_lowercased_and_trimmed():
    parsed = hf._parse_section(
        "exclude_countries: United States, Canada\nexclude_title_keywords:  Engineer ,Developer "
    )
    assert parsed["exclude_countries"] == ["united states", "canada"]
    assert parsed["exclude_title_keywords"] == ["engineer", "developer"]


def test_parse_geo_policy_fields():
    parsed = hf._parse_section(
        "ban_regions: Africa, South_Asia\n"
        "keep_countries: Georgia, Singapore\n"
        "ban_us_only: yes\n"
        "onsite_ok_regions: europe\n"
        "onsite_penalty: 15\n"
    )
    assert parsed["ban_regions"] == ["africa", "south_asia"]
    assert parsed["keep_countries"] == ["georgia", "singapore"]
    assert parsed["ban_us_only"] is True
    assert parsed["onsite_ok_regions"] == ["europe"]
    assert parsed["onsite_penalty"] == 15


def test_parse_geo_bool_and_int_placeholders():
    parsed = hf._parse_section("ban_us_only: no\nonsite_penalty: (none)")
    assert parsed["ban_us_only"] is False
    assert parsed["onsite_penalty"] == 0
    # Garbage int falls back to the default rather than raising.
    assert hf._parse_section("onsite_penalty: lots")["onsite_penalty"] == 0


def test_parse_missing_fields_are_empty():
    assert hf._parse_section("") == dict(hf._GEO_DEFAULTS)


def test_parse_ignores_html_comment_examples():
    # The example countries inside an HTML comment must NOT be parsed as real.
    body = (
        "<!-- example: exclude_countries: france, spain -->\n"
        "exclude_countries: (none)\n"
        "exclude_title_keywords: (none)\n"
    )
    parsed = hf._parse_section(body)
    assert parsed == dict(hf._GEO_DEFAULTS)


def test_load_missing_profile_returns_empty(monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", "/nonexistent/profile/does_not_exist.md")
    importlib.reload(hf)
    assert hf.load_hard_filters() == dict(hf._GEO_DEFAULTS)


# ---------------------------------------------------------------------------
# End-to-end: reload config + filter under a temp profile
# ---------------------------------------------------------------------------

# Engineering vacancy in the US only — the historical owner default dropped it.
ENG_US_VAC = {
    "title": "Senior Software Engineer",
    "locations": [
        {
            "country": "United States",
            "region": "americas",
            "city": "New York",
            "work_mode": "onsite",
        },
    ],
}


def _reload_pipeline(profile_path: Path):
    """Reload the import chain so module-level filter constants pick up the
    profile at ``profile_path``. Returns (config, filter_vacancies, filters)
    modules. ``filters`` is returned AFTER the reload chain: importing
    database_supabase reloads filters in place (rebinding its cached blacklist
    pattern to the fresh profile), so this reference reflects the new profile.
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
    import filters  # reloaded in place by database_supabase's import above

    return config, filter_vacancies, filters


@pytest.fixture
def restore_default_profile():
    """Restore the import chain to the default profile after the test."""
    saved = os.environ.get("USER_PROFILE_PATH")
    yield
    if saved is None:
        os.environ.pop("USER_PROFILE_PATH", None)
    else:
        os.environ["USER_PROFILE_PATH"] = saved
    for mod in ("prompts", "hard_filters", "config", "database_supabase", "filter_vacancies"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])


def test_empty_profile_keeps_engineering_and_us(tmp_path, restore_default_profile):
    profile = _write_profile(
        tmp_path,
        "exclude_countries: (none)\nexclude_title_keywords: (none)",
    )
    config, filter_vacancies, filters = _reload_pipeline(profile)

    assert config.EXCLUDE_COUNTRIES == []
    assert config.EXCLUDE_TITLE_KEYWORDS == []
    # Engineering title is NOT dropped on title.
    assert filters.title_words_blacklisted("Senior Software Engineer") is False
    # US-only vacancy is NOT dropped on geography (no country excluded).
    assert filter_vacancies._all_locations_excluded(ENG_US_VAC) is False


def test_profile_with_filters_drops_engineering_and_us(tmp_path, restore_default_profile):
    profile = _write_profile(
        tmp_path,
        "exclude_countries: united states, canada\nexclude_title_keywords: engineer, developer",
    )
    config, filter_vacancies, filters = _reload_pipeline(profile)

    assert config.EXCLUDE_COUNTRIES == ["united states", "canada"]
    assert "engineer" in config.EXCLUDE_TITLE_KEYWORDS
    # Engineering title IS now dropped on title.
    assert filters.title_words_blacklisted("Senior Software Engineer") is True
    # US-only vacancy IS now dropped on geography (US listed in profile).
    assert filter_vacancies._all_locations_excluded(ENG_US_VAC) is True
    # A non-excluded, non-engineering role still survives.
    assert filters.title_words_blacklisted("Head of Operations") is False
    assert (
        filter_vacancies._all_locations_excluded(
            {"locations": [{"country": "Germany", "city": "Berlin"}]}
        )
        is False
    )
    # Universal junk is still dropped regardless of profile.
    assert filters.title_words_blacklisted("Talent Pool — Future Roles") is True
    # A format role NOT in the profile keywords still survives.
    assert filters.title_words_blacklisted("Volunteer Coordinator") is False


def test_region_ban_drops_country_but_whitelist_and_remote_survive(
    tmp_path, restore_default_profile
):
    """ban_regions drops on-site banned-region roles; whitelist + remote survive."""
    profile = _write_profile(
        tmp_path,
        "ban_regions: south_asia, ex_ussr\nkeep_countries: georgia\nexclude_title_keywords: (none)",
    )
    _config, filter_vacancies, _filters = _reload_pipeline(profile)
    ax = filter_vacancies._all_locations_excluded
    # India (south_asia) on-site → dropped, by explicit country and by free text.
    assert ax({"locations": [{"country": "India", "work_mode": "onsite"}]}) is True
    assert ax({"locations": [{"location": "Delhi, Gurgaon", "work_mode": "onsite"}]}) is True
    # Remote India → kept (reachable from anywhere).
    assert ax({"locations": [{"country": "India", "work_mode": "remote"}]}) is False
    # Georgia (ex_ussr) is whitelisted → kept even on-site.
    assert ax({"locations": [{"country": "Georgia", "work_mode": "onsite"}]}) is False
    # Russia (ex_ussr, not whitelisted) → dropped.
    assert ax({"locations": [{"country": "Russia", "work_mode": "onsite"}]}) is True
    # Europe is in no banned region → kept.
    assert ax({"locations": [{"country": "Germany", "city": "Berlin"}]}) is False


def test_excluded_country_keeps_multi_country_posting(tmp_path, restore_default_profile):
    """A posting that lists an excluded country AND a kept country survives."""
    profile = _write_profile(
        tmp_path,
        "exclude_countries: united states\nexclude_title_keywords: (none)",
    )
    _config, filter_vacancies, _filters = _reload_pipeline(profile)
    vac = {
        "locations": [
            {"country": "United States", "city": "New York"},
            {"country": "United Kingdom", "city": "London"},
        ]
    }
    assert filter_vacancies._all_locations_excluded(vac) is False


# ===========================================================================
# --- from test_geo_silent_filter.py ---
#
# Regression tests for the two "silent no-op" geo-filter bugs.
#
# Both bugs let a user's geography rule quietly do NOTHING, with no signal —
# the worst failure mode for a non-engineer who trusts the filter is on:
#
#   1. config.py: a misspelled / prose region id in the profile's ban_regions
#      or onsite_ok_regions (e.g. "east asia" for "east_asia", or a country
#      name) never matched any world region, so the ban silently never fired.
#   2. hard_filters.py: a mistyped ``## HARD_FILTERS`` heading (wrong level or
#      a typo) made the whole section unparseable, voiding EVERY geo ban and
#      title exclude with no warning.
#
# The fix is the same principle for both: validate, WARN LOUDLY, never
# silently drop, never crash. These tests assert the warning fires and state
# plainly what happens to the rule (an unrecognised region ban is SKIPPED,
# not best-effort).
# ===========================================================================

# ---------------------------------------------------------------------------
# Bug 1 — region-id validation in config.py (source of truth: geo.known_regions)
# ---------------------------------------------------------------------------


def test_known_regions_is_the_country_region_value_set():
    """geo.known_regions() is exactly the closed set of resolvable region ids."""
    import geo

    known = geo.known_regions()
    assert "africa" in known
    assert "east_asia" in known
    assert "narnia" not in known
    # It is the value-set of the country→region map, nothing more.
    assert known == frozenset(geo._country_region_map().values())


def test_misspelled_region_id_is_skipped_and_warns(capsys):
    """A misspelled / prose region id is DROPPED (skipped), not best-effort
    applied, and the user is told loudly which id did nothing."""
    import config

    result = config._validate_regions(["africa", "east asia", "narnia"], "ban_regions")

    # Recognised id survives; the two unrecognised ones are SKIPPED entirely.
    # (Best-effort is impossible: a region with no member countries can never
    # match a vacancy, so there is nothing to apply.)
    assert result == frozenset({"africa"})

    err = capsys.readouterr().err
    assert "ban_regions" in err
    assert "east asia" in err and "narnia" in err
    assert "IGNORED" in err or "NO effect" in err


def test_all_known_region_ids_pass_without_warning(capsys):
    """Correctly spelled ids are kept and produce no warning noise."""
    import config

    result = config._validate_regions(["Africa", " south_asia "], "onsite_ok_regions")
    assert result == frozenset({"africa", "south_asia"})
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Bug 2 — mistyped HARD_FILTERS heading / field name in hard_filters.py
# ---------------------------------------------------------------------------


def _write_silent_filter_profile(tmp_path: Path, body: str) -> Path:
    profile = tmp_path / "user_profile.md"
    profile.write_text(body, encoding="utf-8")
    return profile


def test_mistyped_hard_filters_heading_warns_instead_of_silent_empty(tmp_path, monkeypatch, capsys):
    """An H3 (wrong-level) HARD_FILTERS heading loses the section — but must now
    WARN that all filters are inactive, not silently return empty."""
    import prompts
    import hard_filters as hf

    profile = _write_silent_filter_profile(
        tmp_path,
        "## USER_PROFILE\n\nTest person.\n\n"
        "### HARD_FILTERS\n\n"  # H3 instead of H2 → section not recognised
        "ban_regions: africa, south_asia\n"
        "exclude_title_keywords: engineer\n\n"
        "## NOTES\n\nx\n",
    )
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))
    prompts.clear_profile_cache()

    result = hf.load_hard_filters()

    # Filters are inactive (the section did not parse) ...
    assert result == dict(hf._GEO_DEFAULTS)
    # ... but the user is now warned loudly rather than left guessing.
    err = capsys.readouterr().err
    assert "HARD_FILTERS" in err
    assert "INACTIVE" in err


def test_name_typo_hard_filters_heading_warns(tmp_path, monkeypatch, capsys):
    """A heading name typo ('## HARD FILTER') also warns."""
    import prompts
    import hard_filters as hf

    profile = _write_silent_filter_profile(
        tmp_path,
        "## USER_PROFILE\n\nTest person.\n\n"
        "## HARD FILTER\n\n"  # missing the plural + underscore
        "ban_regions: africa\n\n"
        "## NOTES\n\nx\n",
    )
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))
    prompts.clear_profile_cache()

    hf.load_hard_filters()
    assert "INACTIVE" in capsys.readouterr().err


def test_deliberately_absent_section_does_not_warn(tmp_path, monkeypatch, capsys):
    """A profile with NO HARD_FILTERS heading is the documented default — quiet."""
    import prompts
    import hard_filters as hf

    profile = _write_silent_filter_profile(
        tmp_path,
        "## USER_PROFILE\n\nTest person.\n\n## NOTES\n\nnothing here\n",
    )
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))
    prompts.clear_profile_cache()

    assert hf.load_hard_filters() == dict(hf._GEO_DEFAULTS)
    assert capsys.readouterr().err == ""


def test_correct_but_empty_section_does_not_warn(tmp_path, monkeypatch, capsys):
    """A correct '## HARD_FILTERS' with an empty body is intentional — quiet."""
    import prompts
    import hard_filters as hf

    profile = _write_silent_filter_profile(
        tmp_path,
        "## USER_PROFILE\n\nTest person.\n\n## HARD_FILTERS\n\n## NOTES\n\nx\n",
    )
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))
    prompts.clear_profile_cache()

    hf.load_hard_filters()
    assert capsys.readouterr().err == ""


def test_unrecognised_field_name_warns(capsys):
    """A mistyped field name inside a good section warns instead of silently
    voiding just that one filter."""
    import hard_filters as hf

    parsed = hf._parse_section("ban_region: africa\nexclude_title_keywords: engineer\n")

    # The typo'd field ('ban_region', missing the 's') did not populate ban_regions.
    assert parsed["ban_regions"] == []
    assert parsed["exclude_title_keywords"] == ["engineer"]
    err = capsys.readouterr().err
    assert "ban_region" in err
    assert "INACTIVE" in err


def test_space_before_colon_still_parses(capsys):
    """A stray space before the colon must not silently void the field."""
    import hard_filters as hf

    parsed = hf._parse_section("ban_regions : africa, south_asia\n")
    assert parsed["ban_regions"] == ["africa", "south_asia"]
    assert capsys.readouterr().err == ""


# ===========================================================================
# --- from test_company_title_filters.py ---
#
# Tests for the per-company title INCLUDE-filters.
#
# ``## COMPANY_TITLE_FILTERS`` is the opposite of the GLOBAL HARD_FILTERS: it
# keeps a high-volume company active but lets ONLY profile-relevant titles
# through to scoring. For a LISTED company a vacancy survives the filter
# stage only when its title matches one of the company's include patterns;
# UNLISTED companies are untouched; a missing/empty section is the feature
# off.
#
# These prove:
#   * the parser reads ``- <company> :: <patterns>`` entries and skips
#     malformed lines with a loud warning (never a crash);
#   * a listed company + non-matching title is DROPPED with a rule-named
#     reason;
#   * a listed company + matching title PASSES;
#   * an unlisted company is UNTOUCHED;
#   * an empty/absent section is a no-op.
#
# Because config.py and filters.py compute their constants at import time,
# the end-to-end tests reload that chain under a temporary USER_PROFILE_PATH
# (mirroring the HARD_FILTERS tests above).
# ===========================================================================

# ---------------------------------------------------------------------------
# Parser unit tests (no module reloading needed)
# ---------------------------------------------------------------------------


def test_parse_basic_entry_lowercases_patterns():
    parsed = hf._parse_company_title_filters(
        "- World Food Programme :: Monitoring, Evaluation, Data"
    )
    assert parsed == {"World Food Programme": ["monitoring", "evaluation", "data"]}


def test_parse_multiple_entries_and_merge_dedup():
    parsed = hf._parse_company_title_filters(
        "- WFP :: monitoring, data\n"
        "* FHI 360 :: research, strategy\n"
        "- WFP :: data, evaluation\n"  # merges into WFP, dedups "data"
    )
    assert parsed["WFP"] == ["monitoring", "data", "evaluation"]
    assert parsed["FHI 360"] == ["research", "strategy"]


def test_parse_company_title_ignores_html_comment_examples():
    body = "<!-- - Example Org :: should, not, parse -->\n- Real Org :: policy officer\n"
    parsed = hf._parse_company_title_filters(body)
    assert parsed == {"Real Org": ["policy officer"]}


def test_parse_ignores_plain_prose():
    parsed = hf._parse_company_title_filters("This is just a sentence with no separator.\n")
    assert parsed == {}


def test_parse_empty_body_is_off():
    assert hf._parse_company_title_filters("") == {}


def test_parse_malformed_missing_separator_warns_and_skips(capsys):
    parsed = hf._parse_company_title_filters("- World Food Programme\n- OK Org :: data")
    assert parsed == {"OK Org": ["data"]}
    err = capsys.readouterr().err
    assert "COMPANY_TITLE_FILTERS" in err
    assert "malformed" in err


def test_parse_malformed_empty_patterns_warns_and_skips(capsys):
    parsed = hf._parse_company_title_filters("- Empty Org ::   \n- OK Org :: data")
    assert parsed == {"OK Org": ["data"]}
    assert "malformed" in capsys.readouterr().err


def test_load_missing_section_is_off(monkeypatch):
    monkeypatch.setattr(hf, "_load_user_profile", lambda: {"USER_PROFILE": "x"})
    assert hf.load_company_title_filters() == {}


def test_load_reads_section(monkeypatch):
    monkeypatch.setattr(
        hf,
        "_load_user_profile",
        lambda: {"COMPANY_TITLE_FILTERS": "- WFP :: data, evaluation"},
    )
    assert hf.load_company_title_filters() == {"WFP": ["data", "evaluation"]}


# ---------------------------------------------------------------------------
# End-to-end: reload config + filters under a temp profile
# ---------------------------------------------------------------------------


def _write_company_title_profile(tmp_path: Path, company_filters_block: str) -> Path:
    """Write a minimal profile with the given COMPANY_TITLE_FILTERS body.

    Headers stay at column 0 (the profile parser requires that).
    """
    profile = tmp_path / "user_profile.md"
    profile.write_text(
        "## USER_PROFILE\n\n"
        "Test person.\n\n"
        "## COMPANY_TITLE_FILTERS\n\n"
        f"{company_filters_block}\n\n"
        "## OUTPUT_LANGUAGE\n\n"
        "English\n",
        encoding="utf-8",
    )
    return profile


def _reload_company_title_pipeline(profile_path: Path):
    """Reload the import chain so module-level constants pick up the temp profile.

    Returns (config, filters). filters is reloaded in place by importing
    database_supabase, so its cached COMPANY_TITLE_FILTERS map reflects the new
    profile.
    """
    os.environ["USER_PROFILE_PATH"] = str(profile_path)
    import prompts
    import hard_filters
    import config
    import database_supabase

    importlib.reload(prompts)
    importlib.reload(hard_filters)
    importlib.reload(config)
    importlib.reload(database_supabase)
    import filters

    return config, filters


@pytest.fixture
def restore_default_company_title_profile():
    saved = os.environ.get("USER_PROFILE_PATH")
    yield
    if saved is None:
        os.environ.pop("USER_PROFILE_PATH", None)
    else:
        os.environ["USER_PROFILE_PATH"] = saved
    for mod in ("prompts", "hard_filters", "config", "database_supabase", "filters"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])


def test_listed_company_nonmatching_title_is_dropped(
    tmp_path, restore_default_company_title_profile
):
    profile = _write_company_title_profile(
        tmp_path,
        "- World Food Programme :: monitoring, evaluation, data",
    )
    config, filters = _reload_company_title_pipeline(profile)

    assert config.COMPANY_TITLE_FILTERS == {
        "World Food Programme": ["monitoring", "evaluation", "data"]
    }
    reason = filters.company_title_filter_reason("World Food Programme", "Chief Financial Officer")
    assert reason == "company_title_filter — not in World Food Programme include list"


def test_listed_company_matching_title_passes(tmp_path, restore_default_company_title_profile):
    profile = _write_company_title_profile(
        tmp_path,
        "- World Food Programme :: monitoring, evaluation, data",
    )
    _config, filters = _reload_company_title_pipeline(profile)

    # Whole-word, case-insensitive match anywhere in the title.
    assert (
        filters.company_title_filter_reason("World Food Programme", "Senior Data Analyst") is None
    )
    assert (
        filters.company_title_filter_reason(
            "world food programme", "Monitoring & Evaluation Officer"
        )
        is None
    )


def test_unlisted_company_is_untouched(tmp_path, restore_default_company_title_profile):
    profile = _write_company_title_profile(
        tmp_path,
        "- World Food Programme :: monitoring, evaluation, data",
    )
    _config, filters = _reload_company_title_pipeline(profile)

    # A company with no include-list is never dropped, whatever the title.
    assert filters.company_title_filter_reason("Some Other NGO", "Chief Financial Officer") is None


def test_empty_section_is_a_noop(tmp_path, restore_default_company_title_profile):
    profile = _write_company_title_profile(tmp_path, "")
    config, filters = _reload_company_title_pipeline(profile)

    assert config.COMPANY_TITLE_FILTERS == {}
    assert filters.company_title_filter_reason("World Food Programme", "Anything At All") is None


# ---------------------------------------------------------------------------
# Alias resolution, HTML entities, regex-special patterns, compile-once cache
# ---------------------------------------------------------------------------


@pytest.fixture
def filters_mod():
    import filters

    return filters


def test_alias_spelling_still_hits_include_list(monkeypatch, filters_mod):
    """Profile lists the long board spelling; the org arrives as the short alias.

    Both sides go through resolve_canonical_name, so they land on the same key
    and the include-filter still bites — the exact high-volume-org use case.
    """
    aliases = {
        "wfp - world food programme": "World Food Programme",
        "wfp": "World Food Programme",
    }
    monkeypatch.setattr(
        filters_mod, "resolve_canonical_name", lambda n: aliases.get(n.strip().lower(), n)
    )
    monkeypatch.setattr(
        filters_mod,
        "_COMPANY_TITLE_INCLUDE",
        filters_mod._build_company_title_include(
            {"WFP - World Food Programme": ["programme officer", "data"]}
        ),
    )
    # Alias org spelling + non-matching title → still dropped.
    assert (
        filters_mod.company_title_filter_reason("WFP", "Chief Financial Officer")
        == "company_title_filter — not in WFP include list"
    )
    # Alias org spelling + matching title → passes.
    assert filters_mod.company_title_filter_reason("WFP", "Programme Officer") is None


def test_html_entity_org_spelling_matches(monkeypatch, filters_mod):
    """A board delivering the org with &amp; still hits the include-list."""
    monkeypatch.setattr(
        filters_mod,
        "_COMPANY_TITLE_INCLUDE",
        filters_mod._build_company_title_include({"Health & Hope": ["research"]}),
    )
    assert (
        filters_mod.company_title_filter_reason("Health &amp; Hope", "Fundraising Manager")
        == "company_title_filter — not in Health &amp; Hope include list"
    )
    assert filters_mod.company_title_filter_reason("Health &amp; Hope", "Research Officer") is None


def test_regex_special_pattern_is_escaped(monkeypatch, filters_mod):
    """A regex-special include pattern ('M&E') matches literally, never crashes."""
    monkeypatch.setattr(
        filters_mod,
        "_COMPANY_TITLE_INCLUDE",
        filters_mod._build_company_title_include({"Big NGO": ["m&e"]}),
    )
    assert filters_mod.company_title_filter_reason("Big NGO", "M&E Officer") is None
    assert filters_mod.company_title_filter_reason("Big NGO", "Finance Officer") is not None


def test_include_map_holds_precompiled_patterns(monkeypatch, filters_mod):
    """The map caches COMPILED patterns (dict hit + one search per role)."""
    import re as _re

    built = filters_mod._build_company_title_include({"Org A": ["data"], "org a": ["evaluation"]})
    assert list(built) == ["org a"]  # same canonical key → merged, compiled once
    assert all(isinstance(p, _re.Pattern) for p in built.values())
    # Merged pattern list covers both profile spellings' patterns.
    assert built["org a"].search("data analyst")
    assert built["org a"].search("evaluation officer")
