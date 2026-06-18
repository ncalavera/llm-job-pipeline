"""Tests for the generic geography-exclusion mechanism in filter_vacancies.py.

A vacancy is dropped on geography ONLY when EVERY one of its locations sits in a
country the user's profile excludes. A posting that also lists a kept country
survives. NO country is privileged in code — the mechanism is symmetric.

These tests are deliberately PLACE-AGNOSTIC: they invent fictional countries
("Country A", "Country B", …) and wire them into a temporary geo map (TOML) plus
a temporary profile. So the test proves the MECHANISM, never that any real-world
country is "the" excluded one. Swapping the excluded country in the profile is
all it takes to change behaviour — there is no hardcoded geography.
"""
import importlib
import os
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = str((Path(__file__).resolve().parent.parent / "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

# Fictional countries, each pinned to a neutral structural bucket in the temp
# geo map. "country a" is the one the profile will exclude; the rest are kept.
_GEO_TOML = textwrap.dedent("""
    [geo.countries]
    uk = ["country b"]
    de = ["country c"]
    europe = ["country d"]
    us = ["country e"]
    other = ["country a", "country f"]

    [geo.cities]
    uk = ["city b"]
    de = ["city c"]
    europe = ["city d"]
    us = ["city e"]
    other = ["city a", "city f"]

    [geo.work_mode]
    remote = ["remote"]
    hybrid = ["hybrid"]
""").strip() + "\n"

_RELOAD_CHAIN = ("settings", "geo", "prompts", "hard_filters", "config", "filter_vacancies")


def _reload_all():
    import settings
    settings.clear_cache()
    for mod in _RELOAD_CHAIN:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)


@pytest.fixture
def fv_factory(tmp_path, monkeypatch):
    """Returns a builder: exclude_list → reloaded filter_vacancies module.

    Writes the fictional geo map + a profile excluding the given countries, then
    reloads the whole chain so the module picks up the temp config.
    """
    toml_path = tmp_path / "defaults.toml"
    toml_path.write_text(_GEO_TOML, encoding="utf-8")
    profile_path = tmp_path / "user_profile.md"

    def build(exclude_countries: list[str]):
        excl = ", ".join(exclude_countries) if exclude_countries else "(none)"
        profile_path.write_text(textwrap.dedent(f"""
            ## HARD_FILTERS

            exclude_countries: {excl}
            exclude_title_keywords: (none)
        """).strip() + "\n", encoding="utf-8")
        monkeypatch.setenv("DEFAULTS_TOML_PATH", str(toml_path))
        monkeypatch.setenv("USER_PROFILE_PATH", str(profile_path))
        _reload_all()
        import filter_vacancies
        importlib.reload(filter_vacancies)
        return filter_vacancies

    yield build

    # Restore real config for the rest of the suite.
    monkeypatch.delenv("DEFAULTS_TOML_PATH", raising=False)
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    _reload_all()


# ---------------------------------------------------------------------------
# Core mechanism — one excluded country ("Country A").
# ---------------------------------------------------------------------------

def test_single_excluded_location_drops(fv_factory):
    """A vacancy located only in the excluded country drops."""
    fv = fv_factory(["country a"])
    vac = {"locations": [{"country": "Country A", "city": "City A"}]}
    assert fv._all_locations_excluded(vac) is True
    assert fv._geo_delete_category(vac) == "delete_geo"


def test_single_nonexcluded_location_survives(fv_factory):
    """A vacancy located only in a NON-excluded country survives."""
    fv = fv_factory(["country a"])
    vac = {"locations": [{"country": "Country B", "city": "City B"}]}
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


def test_multi_location_with_any_nonexcluded_survives(fv_factory):
    """[Country A (excluded), Country B (kept)] → the kept country wins."""
    fv = fv_factory(["country a"])
    vac = {"locations": [
        {"country": "Country A", "city": "City A"},
        {"country": "Country B", "city": "City B"},
    ]}
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


def test_multi_location_all_excluded_drops(fv_factory):
    """Every location in an excluded country → drop (multiple excluded)."""
    fv = fv_factory(["country a", "country f"])
    vac = {"locations": [
        {"country": "Country A", "city": "City A"},
        {"country": "Country F", "city": "City F"},
    ]}
    assert fv._all_locations_excluded(vac) is True
    assert fv._geo_delete_category(vac) == "delete_geo"


def test_empty_profile_drops_nothing(fv_factory):
    """With no excluded countries, even an all-Country-A vacancy survives."""
    fv = fv_factory([])
    vac = {"locations": [{"country": "Country A", "city": "City A"}]}
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


# ---------------------------------------------------------------------------
# Symmetry — ANY country can be the excluded one. No bucket is privileged.
# ---------------------------------------------------------------------------

# (excluded country, a control country in a DIFFERENT bucket that must survive).
# Each excluded country lives in a distinct structural bucket, proving the gate
# is symmetric across buckets — none is special-cased. The control sits in
# another bucket so bucket-level exclusion never touches it.
@pytest.mark.parametrize("excluded,excl_city,control,ctrl_city", [
    ("country a", "City A", "Country B", "City B"),  # other  → control uk
    ("country b", "City B", "Country C", "City C"),  # uk     → control de
    ("country c", "City C", "Country D", "City D"),  # de     → control europe
    ("country d", "City D", "Country E", "City E"),  # europe → control us
    ("country e", "City E", "Country A", "City A"),  # us     → control other
])
def test_any_country_excludes_symmetrically(fv_factory, excluded, excl_city, control, ctrl_city):
    """Whichever country the profile names is the one that drops — identical
    mechanism across every structural bucket, none special-cased. A country in a
    different bucket always survives."""
    fv = fv_factory([excluded])
    dropped = {"locations": [{"country": excluded.title(), "city": excl_city}]}
    assert fv._all_locations_excluded(dropped) is True
    assert fv._geo_delete_category(dropped) == "delete_geo"

    kept = {"locations": [{"country": control, "city": ctrl_city}]}
    assert fv._all_locations_excluded(kept) is False


# ---------------------------------------------------------------------------
# Edge cases independent of which country is named.
# ---------------------------------------------------------------------------

def test_empty_locations_survives(fv_factory):
    fv = fv_factory(["country a"])
    assert fv._all_locations_excluded({"locations": []}) is False
    assert fv._geo_delete_category({"locations": []}) is None


def test_no_locations_key_survives(fv_factory):
    fv = fv_factory(["country a"])
    assert fv._all_locations_excluded({}) is False


def test_global_remote_survives(fv_factory):
    """A location with no country (global remote) is never geo-excluded."""
    fv = fv_factory(["country a"])
    vac = {"locations": [{"work_mode": "remote"}]}
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


def test_v1_freetext_excluded_drops(fv_factory):
    """v1 free-text 'location' resolves through the same geo map."""
    fv = fv_factory(["country a"])
    vac = {"locations": [{"location": "City A, Country A"}]}
    assert fv._all_locations_excluded(vac) is True


def test_v1_freetext_mixed_survives(fv_factory):
    fv = fv_factory(["country a"])
    vac = {"locations": [
        {"location": "City A, Country A"},
        {"location": "City B, Country B"},
    ]}
    assert fv._all_locations_excluded(vac) is False


# ---------------------------------------------------------------------------
# REGRESSION — exclusion is EXACT-country, never by region bucket.
#
# The bug: excluding one country dropped every bucket-sibling too (excluding
# "canada" also dropped the US because both share the "us" bucket; excluding
# "france" dropped all of Europe). These tests pin two countries into the SAME
# bucket and prove that excluding one leaves the other untouched.
# ---------------------------------------------------------------------------

_SAME_BUCKET_TOML = textwrap.dedent("""
    [geo.countries]
    uk = []
    de = []
    europe = ["country e1", "country e2"]
    us = ["country u1", "country u2"]
    other = ["country o1"]

    [geo.cities]
    uk = []
    de = []
    europe = ["city e1", "city e2"]
    us = ["city u1", "city u2"]
    other = ["city o1"]

    [geo.work_mode]
    remote = ["remote"]
    hybrid = ["hybrid"]
""").strip() + "\n"


@pytest.fixture
def fv_same_bucket(tmp_path, monkeypatch):
    """Builder using a geo map where several countries share one bucket."""
    toml_path = tmp_path / "defaults.toml"
    toml_path.write_text(_SAME_BUCKET_TOML, encoding="utf-8")
    profile_path = tmp_path / "user_profile.md"

    def build(exclude_countries):
        excl = ", ".join(exclude_countries) if exclude_countries else "(none)"
        profile_path.write_text(textwrap.dedent(f"""
            ## HARD_FILTERS

            exclude_countries: {excl}
            exclude_title_keywords: (none)
        """).strip() + "\n", encoding="utf-8")
        monkeypatch.setenv("DEFAULTS_TOML_PATH", str(toml_path))
        monkeypatch.setenv("USER_PROFILE_PATH", str(profile_path))
        _reload_all()
        import filter_vacancies
        importlib.reload(filter_vacancies)
        return filter_vacancies

    yield build

    monkeypatch.delenv("DEFAULTS_TOML_PATH", raising=False)
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    _reload_all()


def test_excluding_one_country_keeps_its_bucket_sibling(fv_same_bucket):
    """Excluding "country u1" drops it but NOT "country u2" — even though both
    sit in the same "us" bucket. Core regression: NO bucket-wide exclusion."""
    fv = fv_same_bucket(["country u1"])
    dropped = {"locations": [{"country": "Country U1", "city": "City U1"}]}
    sibling = {"locations": [{"country": "Country U2", "city": "City U2"}]}
    assert fv._all_locations_excluded(dropped) is True
    assert fv._geo_delete_category(dropped) == "delete_geo"
    assert fv._all_locations_excluded(sibling) is False
    assert fv._geo_delete_category(sibling) is None


def test_excluding_one_country_keeps_other_bucket(fv_same_bucket):
    """A country in another shared bucket ("europe") is also untouched."""
    fv = fv_same_bucket(["country u1"])
    other_bucket = {"locations": [{"country": "Country E1", "city": "City E1"}]}
    assert fv._all_locations_excluded(other_bucket) is False


def test_multi_country_exact_matching_across_buckets(fv_same_bucket):
    """Excluding two countries in DIFFERENT buckets drops exactly those two and
    nothing else in their buckets."""
    fv = fv_same_bucket(["country u1", "country e1"])
    assert fv._all_locations_excluded({"locations": [{"country": "Country U1"}]}) is True
    assert fv._all_locations_excluded({"locations": [{"country": "Country E1"}]}) is True
    assert fv._all_locations_excluded({"locations": [{"country": "Country U2"}]}) is False
    assert fv._all_locations_excluded({"locations": [{"country": "Country E2"}]}) is False


def test_multi_location_mixed_excluded_sibling_survives(fv_same_bucket):
    """[excluded U1, kept-sibling U2] in the same bucket → kept sibling wins."""
    fv = fv_same_bucket(["country u1"])
    vac = {"locations": [
        {"country": "Country U1", "city": "City U1"},
        {"country": "Country U2", "city": "City U2"},
    ]}
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None
