"""Tests for geo.geo_bucket and the geo delete logic in filter_vacancies.

geo_bucket maps one location entry → uk | germany | europe | us | other |
unknown. These are NEUTRAL structural buckets (data labels). The filter deletes
a vacancy only if ALL of its location entries sit in a country the user's
profile excludes — no country is privileged in code.

To exercise the delete path these tests load filter_vacancies under a profile
that excludes a representative spread of countries (US, Canada, Russia, Georgia,
Armenia, Turkey, Nigeria, India) so we can show that EVERY excluded country is
treated identically. With an EMPTY profile none would be dropped — see
test_hard_filters.py.

geo_bucket itself is a pure function and needs no profile.
"""
import importlib
import os
import sys
import textwrap
from pathlib import Path

import pytest

from geo import geo_bucket

SCRIPTS = str((Path(__file__).resolve().parent.parent / "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


@pytest.fixture(scope="module")
def fv(tmp_path_factory):
    """filter_vacancies reloaded with a profile excluding a spread of countries."""
    tmp = tmp_path_factory.mktemp("geo_profile")
    profile = tmp / "user_profile.md"
    profile.write_text(textwrap.dedent("""
        ## HARD_FILTERS

        exclude_countries: united states, canada, russia, georgia, armenia, turkey, nigeria, india
        exclude_title_keywords: (none)
    """).strip() + "\n", encoding="utf-8")

    saved = os.environ.get("USER_PROFILE_PATH")
    os.environ["USER_PROFILE_PATH"] = str(profile)
    for mod in ("prompts", "hard_filters", "config", "filter_vacancies"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)
    import filter_vacancies
    importlib.reload(filter_vacancies)

    yield filter_vacancies

    if saved is None:
        os.environ.pop("USER_PROFILE_PATH", None)
    else:
        os.environ["USER_PROFILE_PATH"] = saved
    for mod in ("prompts", "hard_filters", "config", "filter_vacancies"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])


# ---------------------------------------------------------------------------
# geo_bucket — single-entry classification (the neutral structural classifier).
# A country name resolving to a bucket here is DATA, not a preference.
# ---------------------------------------------------------------------------

# (case_id, loc_dict, expected_bucket)
BUCKET_CASES = [
    ("london_inperson",   {"country": "United Kingdom", "city": "London", "work_mode": "onsite"}, "uk"),
    ("berlin_inperson",   {"country": "Germany", "city": "Berlin", "work_mode": "onsite"}, "germany"),
    ("paris_europe",      {"country": "France", "city": "Paris", "work_mode": "onsite"}, "europe"),
    ("dublin_europe",     {"city": "Dublin", "work_mode": "onsite"}, "europe"),
    ("tbilisi_inperson",  {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"}, "other"),
    ("tbilisi_remote",    {"country": "Georgia", "city": "Tbilisi", "work_mode": "remote"}, "other"),
    ("moscow_remote",     {"country": "Russia", "city": "Moscow", "work_mode": "remote"}, "other"),
    ("moscow_inperson",   {"country": "Russia", "city": "Moscow", "work_mode": "onsite"}, "other"),
    ("istanbul_other",    {"country": "Turkey", "city": "Istanbul", "work_mode": "onsite"}, "other"),
    ("lagos_other",       {"country": "Nigeria", "city": "Lagos", "work_mode": "onsite"}, "other"),
    ("india_remote_other",{"country": "India", "city": "Bangalore", "work_mode": "remote"}, "other"),
    ("nyc_us",            {"country": "United States", "city": "New York", "work_mode": "onsite"}, "us"),
    ("sf_us",             {"city": "San Francisco", "work_mode": "onsite"}, "us"),
    ("canada_us_bucket",  {"country": "Canada", "city": "Toronto", "work_mode": "onsite"}, "us"),
    ("remote_global",     {"work_mode": "remote"}, "unknown"),
    ("empty_unknown",     {}, "unknown"),
    ("v1_london_text",    {"location": "London, UK"}, "uk"),
    ("v1_us_text",        {"location": "Remote, USA"}, "us"),
    ("region_europe_only",{"region": "europe"}, "europe"),
]


@pytest.mark.parametrize("case_id,loc,expected", BUCKET_CASES, ids=[c[0] for c in BUCKET_CASES])
def test_geo_bucket(case_id, loc, expected):
    assert geo_bucket(loc) == expected


# ---------------------------------------------------------------------------
# Substring TRAP regression — bucket terms must match WHOLE tokens, never a
# substring inside another word. The classic bug: the "us" term firing on the
# "us" inside "Belarus" → "Minsk, Belarus" mislabelled as the us bucket. Also
# "us" inside "Austria" must not pull Austria into us.
# ---------------------------------------------------------------------------

SUBSTRING_TRAP_CASES = [
    ("belarus_free",    {"location": "Minsk, Belarus"},  "other"),
    ("belarus_country", {"country": "Belarus"},          "other"),
    ("austria_free",    {"location": "Vienna, Austria"}, "europe"),
    ("uk_genuine",      {"location": "London, UK"},      "uk"),
    ("us_genuine",      {"location": "Austin, US"},      "us"),
    ("usa_genuine",     {"location": "Remote, USA"},     "us"),
]


@pytest.mark.parametrize(
    "case_id,loc,expected",
    SUBSTRING_TRAP_CASES,
    ids=[c[0] for c in SUBSTRING_TRAP_CASES],
)
def test_geo_bucket_no_substring_traps(case_id, loc, expected):
    """A bucket term must match on token boundaries, not as a raw substring."""
    assert geo_bucket(loc) == expected


def test_belarus_not_us_explicit():
    """Pinned: the headline trap. 'Minsk, Belarus' is NOT the us bucket."""
    assert geo_bucket({"location": "Minsk, Belarus"}) != "us"
    assert geo_bucket({"country": "Belarus"}) != "us"


# ---------------------------------------------------------------------------
# _all_locations_excluded — vacancy-level gate. No country is privileged: every
# excluded country (US, Canada, Georgia, Turkey, …) is treated identically.
# ---------------------------------------------------------------------------

# Each excluded country drops the same way, regardless of bucket or work_mode.
EXCLUDED_SINGLE_LOC_CASES = [
    ("georgia_inperson",  {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"}),
    ("georgia_remote",    {"country": "Georgia", "city": "Tbilisi", "work_mode": "remote"}),
    ("turkey_inperson",   {"country": "Turkey", "city": "Istanbul", "work_mode": "onsite"}),
    ("nigeria_inperson",  {"country": "Nigeria", "city": "Lagos"}),
    ("us_inperson",       {"country": "United States", "city": "New York", "work_mode": "onsite"}),
    ("canada_inperson",   {"country": "Canada", "city": "Toronto", "work_mode": "onsite"}),
    ("us_v1_text",        {"location": "Remote, USA"}),
]


@pytest.mark.parametrize("case_id,loc", EXCLUDED_SINGLE_LOC_CASES, ids=[c[0] for c in EXCLUDED_SINGLE_LOC_CASES])
def test_single_excluded_location_dropped(fv, case_id, loc):
    """A vacancy whose only location is in an excluded country drops — same
    mechanism for every country, no special carve-out."""
    vac = {"locations": [loc]}
    assert fv._all_locations_excluded(vac) is True
    assert fv._geo_delete_category(vac) == "delete_geo"


# A NOT-excluded country (recognised or not) keeps the vacancy.
KEPT_SINGLE_LOC_CASES = [
    ("uk",          {"country": "United Kingdom", "city": "London", "work_mode": "onsite"}),
    ("germany",     {"country": "Germany", "city": "Berlin"}),
    ("france",      {"country": "France", "city": "Paris"}),
    ("global_remote", {"work_mode": "remote"}),
]


@pytest.mark.parametrize("case_id,loc", KEPT_SINGLE_LOC_CASES, ids=[c[0] for c in KEPT_SINGLE_LOC_CASES])
def test_single_unexcluded_location_kept(fv, case_id, loc):
    vac = {"locations": [loc]}
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


# ---------------------------------------------------------------------------
# Multi-location: drops only when EVERY entry is excluded; any kept entry wins.
# ---------------------------------------------------------------------------

def test_multi_any_kept_survives_us_mixed(fv):
    """[London, NYC] → London is kept, so the vacancy survives even though NYC
    (an excluded country) is present. US is not privileged into a drop."""
    vac = {"locations": [
        {"country": "United Kingdom", "city": "London"},
        {"country": "United States", "city": "New York"},
    ]}
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


def test_multi_any_kept_survives_other_mixed(fv):
    """[Tbilisi in-person, Berlin] → Berlin keeps it."""
    vac = {"locations": [
        {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"},
        {"country": "Germany", "city": "Berlin"},
    ]}
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


def test_multi_all_excluded_dropped(fv):
    """Every entry in an excluded country → drop, regardless of which countries."""
    vac = {"locations": [
        {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"},
        {"country": "United States", "city": "New York"},
        {"country": "Armenia", "city": "Yerevan", "work_mode": "onsite"},
    ]}
    assert fv._all_locations_excluded(vac) is True
    assert fv._geo_delete_category(vac) == "delete_geo"


def test_empty_locations_kept(fv):
    assert fv._geo_delete_category({"locations": []}) is None
    assert fv._all_locations_excluded({"locations": []}) is False
