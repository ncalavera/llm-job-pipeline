"""Tests for geo.geo_bucket and the geo delete-vote logic in filter_vacancies.

geo_bucket maps one location entry → uk | germany | europe | us | cis | other
| unknown. The filter deletes a vacancy only if ALL entries vote delete
(CIS in-person / rest-of-world); US-only handled by _is_usa_only.
"""
import pytest

from geo import geo_bucket
from filter_vacancies import _geo_entry_vote, _geo_delete_category, _is_usa_only


# ---------------------------------------------------------------------------
# geo_bucket — single-entry classification (the ≥15 case table)
# ---------------------------------------------------------------------------

# (case_id, loc_dict, expected_bucket)
BUCKET_CASES = [
    ("london_inperson",   {"country": "United Kingdom", "city": "London", "work_mode": "onsite"}, "uk"),
    ("berlin_inperson",   {"country": "Germany", "city": "Berlin", "work_mode": "onsite"}, "germany"),
    ("paris_europe",      {"country": "France", "city": "Paris", "work_mode": "onsite"}, "europe"),
    ("dublin_europe",     {"city": "Dublin", "work_mode": "onsite"}, "europe"),
    ("tbilisi_inperson",  {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"}, "cis"),
    ("tbilisi_remote",    {"country": "Georgia", "city": "Tbilisi", "work_mode": "remote"}, "cis"),
    ("moscow_remote",     {"country": "Russia", "city": "Moscow", "work_mode": "remote"}, "cis"),
    ("moscow_inperson",   {"country": "Russia", "city": "Moscow", "work_mode": "onsite"}, "cis"),
    ("istanbul_other",    {"country": "Turkey", "city": "Istanbul", "work_mode": "onsite"}, "other"),
    ("lagos_other",       {"country": "Nigeria", "city": "Lagos", "work_mode": "onsite"}, "other"),
    ("india_remote_other",{"country": "India", "city": "Bangalore", "work_mode": "remote"}, "other"),
    ("nyc_us",            {"country": "United States", "city": "New York", "work_mode": "onsite"}, "us"),
    ("sf_us",             {"city": "San Francisco", "work_mode": "onsite"}, "us"),
    ("canada_us_bucket",  {"country": "Canada", "city": "Toronto", "work_mode": "onsite"}, "us"),
    ("remote_global",     {"work_mode": "remote"}, "unknown"),
    ("empty_unknown",     {}, "unknown"),
    ("v1_london_text",    {"location": "London, UK"}, "uk"),
    ("v1_remote_usa",     {"location": "Remote, USA"}, "us"),
    ("region_europe_only",{"region": "europe"}, "europe"),
]


@pytest.mark.parametrize("case_id,loc,expected", BUCKET_CASES, ids=[c[0] for c in BUCKET_CASES])
def test_geo_bucket(case_id, loc, expected):
    assert geo_bucket(loc) == expected


# ---------------------------------------------------------------------------
# _geo_entry_vote — per-entry keep/delete vote
# ---------------------------------------------------------------------------

def test_vote_uk_keep():
    assert _geo_entry_vote({"country": "United Kingdom", "city": "London"})[0] == "keep"


def test_vote_germany_keep():
    assert _geo_entry_vote({"country": "Germany", "city": "Berlin"})[0] == "keep"


def test_vote_europe_keep():
    assert _geo_entry_vote({"country": "France", "city": "Paris"})[0] == "keep"


def test_vote_cis_inperson_delete():
    vote, reason = _geo_entry_vote({"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"})
    assert vote == "delete" and reason == "cis"


def test_vote_cis_remote_keep():
    """Remote from Georgia/Russia → keep (user works remotely)."""
    assert _geo_entry_vote({"country": "Georgia", "city": "Tbilisi", "work_mode": "remote"})[0] == "keep"
    assert _geo_entry_vote({"country": "Russia", "city": "Moscow", "work_mode": "remote"})[0] == "keep"


def test_vote_other_delete():
    vote, reason = _geo_entry_vote({"country": "Turkey", "city": "Istanbul"})
    assert vote == "delete" and reason == "row"


def test_vote_us_keep_handled_by_usa_path():
    """US votes keep here; _is_usa_only owns US deletion."""
    assert _geo_entry_vote({"country": "United States", "city": "New York"})[0] == "keep"


def test_vote_global_remote_keep():
    assert _geo_entry_vote({"work_mode": "remote"})[0] == "keep"


# ---------------------------------------------------------------------------
# _geo_delete_category — vacancy-level (ALL entries must vote delete)
# ---------------------------------------------------------------------------

def test_cat_tbilisi_inperson_delete_cis():
    vac = {"locations": [{"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"}]}
    assert _geo_delete_category(vac) == "delete_cis"


def test_cat_istanbul_delete_row():
    vac = {"locations": [{"country": "Turkey", "city": "Istanbul", "work_mode": "onsite"}]}
    assert _geo_delete_category(vac) == "delete_row"


def test_cat_lagos_delete_row():
    vac = {"locations": [{"country": "Nigeria", "city": "Lagos"}]}
    assert _geo_delete_category(vac) == "delete_row"


def test_cat_tbilisi_remote_kept():
    vac = {"locations": [{"country": "Georgia", "city": "Tbilisi", "work_mode": "remote"}]}
    assert _geo_delete_category(vac) is None


def test_cat_moscow_remote_kept():
    vac = {"locations": [{"country": "Russia", "city": "Moscow", "work_mode": "remote"}]}
    assert _geo_delete_category(vac) is None


def test_cat_london_inperson_kept():
    vac = {"locations": [{"country": "United Kingdom", "city": "London", "work_mode": "onsite"}]}
    assert _geo_delete_category(vac) is None


def test_cat_berlin_kept():
    vac = {"locations": [{"country": "Germany", "city": "Berlin"}]}
    assert _geo_delete_category(vac) is None


def test_cat_paris_kept():
    vac = {"locations": [{"country": "France", "city": "Paris"}]}
    assert _geo_delete_category(vac) is None


def test_cat_multi_london_nyc_any_match_kept():
    """[London, NYC] → any keep entry → not geo-deleted."""
    vac = {"locations": [
        {"country": "United Kingdom", "city": "London"},
        {"country": "United States", "city": "New York"},
    ]}
    assert _geo_delete_category(vac) is None


def test_cat_multi_tbilisi_berlin_kept():
    """[Tbilisi in-person, Berlin] → Berlin keeps it."""
    vac = {"locations": [
        {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"},
        {"country": "Germany", "city": "Berlin"},
    ]}
    assert _geo_delete_category(vac) is None


def test_cat_all_cis_inperson_delete_cis():
    vac = {"locations": [
        {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"},
        {"country": "Armenia", "city": "Yerevan", "work_mode": "onsite"},
    ]}
    assert _geo_delete_category(vac) == "delete_cis"


def test_cat_mixed_cis_and_row_dominant_cis():
    """All delete votes, mix of cis + row → cis wins (dominant)."""
    vac = {"locations": [
        {"country": "Georgia", "city": "Tbilisi", "work_mode": "onsite"},
        {"country": "Turkey", "city": "Istanbul"},
    ]}
    assert _geo_delete_category(vac) == "delete_cis"


def test_cat_empty_locations_kept():
    assert _geo_delete_category({"locations": []}) is None


def test_cat_global_remote_kept():
    vac = {"locations": [{"work_mode": "remote"}]}
    assert _geo_delete_category(vac) is None


# ---------------------------------------------------------------------------
# Regression guard: US deletion still owned by _is_usa_only, not geo.
# ---------------------------------------------------------------------------

def test_remote_usa_still_deleted_by_usa_path():
    vac = {"locations": [{"location": "Remote, USA"}]}
    assert _is_usa_only(vac) is True
    # geo does not also delete it (US votes keep) — avoids double-count.
    assert _geo_delete_category(vac) is None


def test_canada_deleted_by_usa_path():
    vac = {"locations": [{"country": "Canada", "city": "Toronto", "work_mode": "onsite"}]}
    assert _is_usa_only(vac) is True
