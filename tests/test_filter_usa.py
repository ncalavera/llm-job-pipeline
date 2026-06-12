"""Tests for _is_usa_only and _is_loc_entry_blacklisted in filter_vacancies.py.

Covers:
- v2 structured location entries (country/region/city/work_mode)
- v1 text-based location entries (location key)
- Multi-location vacancies (the core bug fix for #192)
- Legacy top-level fields (no locations array)
- Edge cases (empty, remote-only, mixed)
"""
import pytest

from filter_vacancies import _is_usa_only, _is_loc_entry_blacklisted


# ---------------------------------------------------------------------------
# _is_loc_entry_blacklisted — unit tests for single entry classification
# ---------------------------------------------------------------------------

class TestLocEntryBlacklisted:
    def test_us_country_is_blacklisted(self):
        loc = {"country": "United States", "region": "americas", "city": "New York"}
        assert _is_loc_entry_blacklisted(loc) is True

    def test_usa_country_variant(self):
        loc = {"country": "USA", "region": "americas", "city": "San Francisco"}
        assert _is_loc_entry_blacklisted(loc) is True

    def test_us_short_country_variant(self):
        loc = {"country": "US", "region": "americas"}
        assert _is_loc_entry_blacklisted(loc) is True

    def test_canada_country_is_blacklisted(self):
        loc = {"country": "Canada", "region": "americas", "city": "Toronto"}
        assert _is_loc_entry_blacklisted(loc) is True

    def test_uk_country_not_blacklisted(self):
        loc = {"country": "United Kingdom", "region": "europe", "city": "London"}
        assert _is_loc_entry_blacklisted(loc) is False

    def test_germany_not_blacklisted(self):
        loc = {"country": "Germany", "region": "europe", "city": "Berlin"}
        assert _is_loc_entry_blacklisted(loc) is False

    def test_europe_region_not_blacklisted(self):
        loc = {"region": "europe"}
        assert _is_loc_entry_blacklisted(loc) is False

    def test_remote_region_not_blacklisted(self):
        loc = {"region": "remote", "work_mode": "remote"}
        assert _is_loc_entry_blacklisted(loc) is False

    def test_americas_region_no_country_blacklisted(self):
        """Ambiguous 'americas' without country → treat as US."""
        loc = {"region": "americas"}
        assert _is_loc_entry_blacklisted(loc) is True

    def test_global_remote_no_country_not_blacklisted(self):
        loc = {"work_mode": "remote"}
        assert _is_loc_entry_blacklisted(loc) is False

    def test_empty_entry_not_blacklisted(self):
        assert _is_loc_entry_blacklisted({}) is False

    def test_v1_location_text_us_city(self):
        loc = {"location": "New York, USA"}
        assert _is_loc_entry_blacklisted(loc) is True

    def test_v1_location_text_london(self):
        loc = {"location": "London, UK"}
        assert _is_loc_entry_blacklisted(loc) is False

    def test_v1_location_text_remote_usa(self):
        loc = {"location": "Remote, USA"}
        assert _is_loc_entry_blacklisted(loc) is True

    def test_v1_semicolon_all_us(self):
        loc = {"location": "New York, USA; San Francisco, USA"}
        assert _is_loc_entry_blacklisted(loc) is True

    def test_v1_semicolon_mixed_keeps(self):
        """Semicolon-separated with non-US → not blacklisted."""
        loc = {"location": "New York, USA; London, UK"}
        assert _is_loc_entry_blacklisted(loc) is False

    def test_non_us_country_with_americas_region(self):
        """Brazil is in americas but has explicit non-US country → not blacklisted."""
        loc = {"country": "Brazil", "region": "americas", "city": "São Paulo"}
        assert _is_loc_entry_blacklisted(loc) is False


# ---------------------------------------------------------------------------
# _is_usa_only — vacancy-level tests (the actual filter function)
# ---------------------------------------------------------------------------

class TestIsUsaOnly:
    # --- v2 structured locations ---

    def test_single_us_location_true(self):
        vac = {"locations": [
            {"country": "United States", "region": "americas", "city": "New York", "work_mode": "onsite"},
        ]}
        assert _is_usa_only(vac) is True

    def test_all_us_locations_true(self):
        vac = {"locations": [
            {"country": "United States", "region": "americas", "city": "San Francisco", "work_mode": "onsite"},
            {"country": "United States", "region": "americas", "city": "New York", "work_mode": "onsite"},
        ]}
        assert _is_usa_only(vac) is True

    def test_mixed_us_and_uk_false(self):
        """Core bug #192: multi-location with US + non-US must NOT be deleted."""
        vac = {"locations": [
            {"country": "United States", "region": "americas", "city": "New York", "work_mode": "onsite"},
            {"country": "United Kingdom", "region": "europe", "city": "London", "work_mode": "onsite"},
        ]}
        assert _is_usa_only(vac) is False

    def test_mixed_us_and_germany_false(self):
        vac = {"locations": [
            {"country": "United States", "region": "americas", "city": "San Francisco"},
            {"country": "Germany", "region": "europe", "city": "Berlin"},
        ]}
        assert _is_usa_only(vac) is False

    def test_mixed_us_and_remote_false(self):
        """US office + global remote → keep (not USA-only)."""
        vac = {"locations": [
            {"country": "United States", "region": "americas", "city": "New York", "work_mode": "onsite"},
            {"work_mode": "remote"},
        ]}
        assert _is_usa_only(vac) is False

    def test_single_europe_location_false(self):
        vac = {"locations": [
            {"country": "United Kingdom", "region": "europe", "city": "London", "work_mode": "onsite"},
        ]}
        assert _is_usa_only(vac) is False

    def test_empty_locations_false(self):
        vac = {"locations": []}
        assert _is_usa_only(vac) is False

    def test_no_locations_key_false(self):
        vac = {}
        assert _is_usa_only(vac) is False

    def test_remote_only_false(self):
        vac = {"locations": [
            {"work_mode": "remote"},
        ]}
        assert _is_usa_only(vac) is False

    # --- v1 text-based locations ---

    def test_v1_location_entries_us_only_true(self):
        vac = {"locations": [
            {"location": "New York, USA"},
            {"location": "San Francisco, USA"},
        ]}
        assert _is_usa_only(vac) is True

    def test_v1_location_entries_mixed_false(self):
        """Core bug #192 with v1 data: US + non-US location entries."""
        vac = {"locations": [
            {"location": "New York, USA"},
            {"location": "London, UK"},
        ]}
        assert _is_usa_only(vac) is False

    # --- Legacy top-level fields (no locations array) ---

    def test_legacy_us_region_true(self):
        vac = {"region": "us", "location": "New York"}
        assert _is_usa_only(vac) is True

    def test_legacy_us_region_remote_false(self):
        """Legacy: US region but remote → keep."""
        vac = {"region": "us", "location": "Remote, New York"}
        assert _is_usa_only(vac) is False

    def test_legacy_blacklisted_city_true(self):
        # The public repo ships a generic LOCATION_BLACKLIST (country markers,
        # not specific US cities). Use an explicit country marker here; add
        # city names to LOCATION_BLACKLIST in config.py for your own search.
        vac = {"location": "San Francisco, USA"}
        assert _is_usa_only(vac) is True

    def test_legacy_europe_city_false(self):
        vac = {"location": "Berlin, Germany"}
        assert _is_usa_only(vac) is False

    def test_legacy_semicolon_all_us_true(self):
        vac = {"location": "New York, USA; San Francisco, USA"}
        assert _is_usa_only(vac) is True

    def test_legacy_semicolon_mixed_false(self):
        vac = {"location": "New York, USA; London, UK"}
        assert _is_usa_only(vac) is False

    def test_legacy_no_location_no_region_false(self):
        vac = {"title": "Some Job"}
        assert _is_usa_only(vac) is False

    # --- Mixed v1/v2 entries in same array ---

    def test_mixed_v1_v2_us_only_true(self):
        vac = {"locations": [
            {"country": "United States", "region": "americas", "city": "NYC"},
            {"location": "San Francisco, USA"},
        ]}
        assert _is_usa_only(vac) is True

    def test_mixed_v1_v2_with_europe_false(self):
        vac = {"locations": [
            {"country": "United States", "region": "americas", "city": "NYC"},
            {"location": "London, UK"},
        ]}
        assert _is_usa_only(vac) is False
