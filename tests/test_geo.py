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

Absorbs the non-duplicate part of test_geo_exclusion.py: the place-agnostic
mechanism tests that pin fictional countries into a temporary geo map to prove
the exclusion gate is symmetric across buckets, plus the bucket-sibling
regression (excluding one country in a shared bucket must not also drop
another country in that same bucket). The rest of test_geo_exclusion.py
duplicated real-country coverage already above and was dropped.
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
    profile.write_text(
        textwrap.dedent("""
        ## HARD_FILTERS

        exclude_countries: united states, canada, russia, georgia, armenia, turkey, nigeria, india
        exclude_title_keywords: (none)
    """).strip()
        + "\n",
        encoding="utf-8",
    )

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
    (
        "london_inperson",
        {"country": "United Kingdom", "city": "London", "work_mode": "onsite"},
        "uk",
    ),
    ("berlin_inperson", {"country": "Germany", "city": "Berlin", "work_mode": "onsite"}, "germany"),
    ("paris_europe", {"country": "France", "city": "Paris", "work_mode": "onsite"}, "europe"),
    ("dublin_europe", {"city": "Dublin", "work_mode": "onsite"}, "europe"),
    ("tbilisi_inperson", {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"}, "other"),
    ("tbilisi_remote", {"country": "Georgia", "city": "Tbilisi", "work_mode": "remote"}, "other"),
    ("moscow_remote", {"country": "Russia", "city": "Moscow", "work_mode": "remote"}, "other"),
    ("moscow_inperson", {"country": "Russia", "city": "Moscow", "work_mode": "onsite"}, "other"),
    ("istanbul_other", {"country": "Turkey", "city": "Istanbul", "work_mode": "onsite"}, "other"),
    ("lagos_other", {"country": "Nigeria", "city": "Lagos", "work_mode": "onsite"}, "other"),
    (
        "india_remote_other",
        {"country": "India", "city": "Bangalore", "work_mode": "remote"},
        "other",
    ),
    ("nyc_us", {"country": "United States", "city": "New York", "work_mode": "onsite"}, "us"),
    ("sf_us", {"city": "San Francisco", "work_mode": "onsite"}, "us"),
    ("canada_us_bucket", {"country": "Canada", "city": "Toronto", "work_mode": "onsite"}, "us"),
    ("remote_global", {"work_mode": "remote"}, "unknown"),
    ("empty_unknown", {}, "unknown"),
    ("v1_london_text", {"location": "London, UK"}, "uk"),
    ("v1_us_text", {"location": "Remote, USA"}, "us"),
    ("region_europe_only", {"region": "europe"}, "europe"),
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
    ("belarus_free", {"location": "Minsk, Belarus"}, "other"),
    ("belarus_country", {"country": "Belarus"}, "other"),
    ("austria_free", {"location": "Vienna, Austria"}, "europe"),
    ("uk_genuine", {"location": "London, UK"}, "uk"),
    ("us_genuine", {"location": "Austin, US"}, "us"),
    ("usa_genuine", {"location": "Remote, USA"}, "us"),
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

# Each excluded country drops the same way — but ONLY for an on-site role. A
# location in an excluded country drops solely when it is not remote.
EXCLUDED_SINGLE_LOC_CASES = [
    ("georgia_inperson", {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"}),
    ("turkey_inperson", {"country": "Turkey", "city": "Istanbul", "work_mode": "onsite"}),
    ("nigeria_inperson", {"country": "Nigeria", "city": "Lagos"}),
    ("us_inperson", {"country": "United States", "city": "New York", "work_mode": "onsite"}),
    ("canada_inperson", {"country": "Canada", "city": "Toronto", "work_mode": "onsite"}),
]


@pytest.mark.parametrize(
    "case_id,loc", EXCLUDED_SINGLE_LOC_CASES, ids=[c[0] for c in EXCLUDED_SINGLE_LOC_CASES]
)
def test_single_excluded_location_dropped(fv, case_id, loc):
    """A vacancy whose only location is an ON-SITE role in an excluded country
    drops — same mechanism for every country, no special carve-out."""
    vac = {"locations": [loc]}
    assert fv._all_locations_excluded(vac) is True
    assert fv._geo_delete_category(vac) == "delete_geo"


# Remote carve-out: a remote-open role is reachable regardless of the country
# stamped on it, so the geography gate must NOT drop it — even when that country
# is profile-excluded. Signalled either by work_mode=remote or a "Remote" text.
REMOTE_IN_EXCLUDED_COUNTRY_CASES = [
    ("georgia_remote", {"country": "Georgia", "city": "Tbilisi", "work_mode": "remote"}),
    ("us_remote_mode", {"country": "United States", "city": "New York", "work_mode": "remote"}),
    ("us_v1_text", {"location": "Remote, USA"}),
]


@pytest.mark.parametrize(
    "case_id,loc",
    REMOTE_IN_EXCLUDED_COUNTRY_CASES,
    ids=[c[0] for c in REMOTE_IN_EXCLUDED_COUNTRY_CASES],
)
def test_remote_in_excluded_country_kept(fv, case_id, loc):
    """A remote role survives the geography gate even in an excluded country."""
    vac = {"locations": [loc]}
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


# A NOT-excluded country (recognised or not) keeps the vacancy.
KEPT_SINGLE_LOC_CASES = [
    ("uk", {"country": "United Kingdom", "city": "London", "work_mode": "onsite"}),
    ("germany", {"country": "Germany", "city": "Berlin"}),
    ("france", {"country": "France", "city": "Paris"}),
    ("global_remote", {"work_mode": "remote"}),
]


@pytest.mark.parametrize(
    "case_id,loc", KEPT_SINGLE_LOC_CASES, ids=[c[0] for c in KEPT_SINGLE_LOC_CASES]
)
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
    vac = {
        "locations": [
            {"country": "United Kingdom", "city": "London"},
            {"country": "United States", "city": "New York"},
        ]
    }
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


def test_multi_any_kept_survives_other_mixed(fv):
    """[Tbilisi in-person, Berlin] → Berlin keeps it."""
    vac = {
        "locations": [
            {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"},
            {"country": "Germany", "city": "Berlin"},
        ]
    }
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None


def test_multi_all_excluded_dropped(fv):
    """Every entry in an excluded country → drop, regardless of which countries."""
    vac = {
        "locations": [
            {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"},
            {"country": "United States", "city": "New York"},
            {"country": "Armenia", "city": "Yerevan", "work_mode": "onsite"},
        ]
    }
    assert fv._all_locations_excluded(vac) is True
    assert fv._geo_delete_category(vac) == "delete_geo"


def test_empty_locations_kept(fv):
    assert fv._geo_delete_category({"locations": []}) is None
    assert fv._all_locations_excluded({"locations": []}) is False


# ===========================================================================
# --- from test_geo_exclusion.py ---
#
# The generic geography-exclusion mechanism in filter_vacancies.py.
#
# These tests are deliberately PLACE-AGNOSTIC: they invent fictional countries
# ("Country A", "Country B", …) and wire them into a temporary geo map (TOML)
# plus a temporary profile. So the test proves the MECHANISM, never that any
# real-world country is "the" excluded one. Swapping the excluded country in
# the profile is all it takes to change behaviour — there is no hardcoded
# geography.
# ===========================================================================

# Fictional countries, each pinned to a neutral structural bucket in the temp
# geo map. "country a" is the one the profile will exclude; the rest are kept.
_GEO_TOML = (
    textwrap.dedent("""
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
""").strip()
    + "\n"
)

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
        profile_path.write_text(
            textwrap.dedent(f"""
            ## HARD_FILTERS

            exclude_countries: {excl}
            exclude_title_keywords: (none)
        """).strip()
            + "\n",
            encoding="utf-8",
        )
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
# Symmetry — ANY country can be the excluded one. No bucket is privileged.
# ---------------------------------------------------------------------------


# (excluded country, a control country in a DIFFERENT bucket that must survive).
# Each excluded country lives in a distinct structural bucket, proving the gate
# is symmetric across buckets — none is special-cased. The control sits in
# another bucket so bucket-level exclusion never touches it.
@pytest.mark.parametrize(
    "excluded,excl_city,control,ctrl_city",
    [
        ("country a", "City A", "Country B", "City B"),  # other  → control uk
        ("country b", "City B", "Country C", "City C"),  # uk     → control de
        ("country c", "City C", "Country D", "City D"),  # de     → control europe
        ("country d", "City D", "Country E", "City E"),  # europe → control us
        ("country e", "City E", "Country A", "City A"),  # us     → control other
    ],
)
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
# REGRESSION — exclusion is EXACT-country, never by region bucket.
#
# The bug: excluding one country dropped every bucket-sibling too (excluding
# "canada" also dropped the US because both share the "us" bucket; excluding
# "france" dropped all of Europe). These tests pin two countries into the SAME
# bucket and prove that excluding one leaves the other untouched.
# ---------------------------------------------------------------------------

_SAME_BUCKET_TOML = (
    textwrap.dedent("""
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
""").strip()
    + "\n"
)


@pytest.fixture
def fv_same_bucket(tmp_path, monkeypatch):
    """Builder using a geo map where several countries share one bucket."""
    toml_path = tmp_path / "defaults.toml"
    toml_path.write_text(_SAME_BUCKET_TOML, encoding="utf-8")
    profile_path = tmp_path / "user_profile.md"

    def build(exclude_countries):
        excl = ", ".join(exclude_countries) if exclude_countries else "(none)"
        profile_path.write_text(
            textwrap.dedent(f"""
            ## HARD_FILTERS

            exclude_countries: {excl}
            exclude_title_keywords: (none)
        """).strip()
            + "\n",
            encoding="utf-8",
        )
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
    vac = {
        "locations": [
            {"country": "Country U1", "city": "City U1"},
            {"country": "Country U2", "city": "City U2"},
        ]
    }
    assert fv._all_locations_excluded(vac) is False
    assert fv._geo_delete_category(vac) is None
