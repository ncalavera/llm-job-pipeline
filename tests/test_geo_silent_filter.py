"""Regression tests for the two "silent no-op" geo-filter bugs.

Both bugs let a user's geography rule quietly do NOTHING, with no signal — the
worst failure mode for a non-engineer who trusts the filter is on:

  1. config.py: a misspelled / prose region id in the profile's ban_regions or
     onsite_ok_regions (e.g. "east asia" for "east_asia", or a country name)
     never matched any world region, so the ban silently never fired.
  2. hard_filters.py: a mistyped ``## HARD_FILTERS`` heading (wrong level or a
     typo) made the whole section unparseable, voiding EVERY geo ban and title
     exclude with no warning.

The fix is the same principle for both: validate, WARN LOUDLY, never silently
drop, never crash. These tests assert the warning fires and state plainly what
happens to the rule (an unrecognised region ban is SKIPPED, not best-effort).
"""

import sys
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


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


def _write_profile(tmp_path: Path, body: str) -> Path:
    profile = tmp_path / "user_profile.md"
    profile.write_text(body, encoding="utf-8")
    return profile


def test_mistyped_hard_filters_heading_warns_instead_of_silent_empty(tmp_path, monkeypatch, capsys):
    """An H3 (wrong-level) HARD_FILTERS heading loses the section — but must now
    WARN that all filters are inactive, not silently return empty."""
    import prompts
    import hard_filters as hf

    profile = _write_profile(
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

    profile = _write_profile(
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

    profile = _write_profile(
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

    profile = _write_profile(
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
