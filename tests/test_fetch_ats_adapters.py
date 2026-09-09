"""Unit tests for the ATS adapter fetchers under scripts/fetchers/ats/.

One section per adapter: ADP Workforce Now, Pinpoint, SmartRecruiters,
SuccessFactors, Teamtailor RSS. Absorbed test_fetch_adp_json.py,
test_fetch_pinpoint.py, test_fetch_smartrecruiters.py,
test_fetch_successfactors.py, test_fetch_teamtailor_rss.py.
"""

import json
import os
import re
from urllib.parse import parse_qs, urlparse

import pytest

import fetchers
from fetchers import (
    fetch_adp_json,
    _adp_location,
    _adp_job_url,
    _adp_snippet,
    _adp_cid,
    fetch_pinpoint,
    _pinpoint_location,
    fetch_smartrecruiters,
    _sr_location,
    _sr_department,
    fetch_successfactors,
    _parse_successfactors_tiles,
    _sf_base_url,
    _sf_sitemap_job_urls,
    _sf_job_detail,
    fetch_teamtailor_rss,
)
from fetchers.http import FetchError
from fetchers.ats.teamtailor import _teamtailor_hosts

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_json(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def _load(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# --- from test_fetch_adp_json.py ---
# ADP Workforce Now public feed (U5).
#
# Fixture ``fixtures/adp_job_requisitions.json`` is the real public feed for
# Rockefeller Foundation (cid 24726181-…), captured live; ``adp_empty.json`` is
# the same shape with an empty ``jobRequisitions`` array.
# ---------------------------------------------------------------------------

CID = "24726181-f57f-46a1-824d-3c8a89c3328a"

ADP_FEED = _load_json("adp_job_requisitions.json")
ADP_EMPTY = _load_json("adp_empty.json")


class AdpFakeResponse:
    def __init__(self, *, json_data=None, status=200):
        self._json = json_data
        self.status_code = status

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class AdpFakeRequests:
    def __init__(self, response=None, *, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, url, headers=None, timeout=None, params=None):
        self.calls.append(url)
        if self.raise_exc:
            raise self.raise_exc
        return self.response


class TestAdpHelpers:
    def test_cid_from_ats_slug(self):
        assert _adp_cid({"ats_slug": CID}) == CID

    def test_cid_from_ats_config(self):
        assert _adp_cid({"ats_config": {"cid": CID}}) == CID

    def test_cid_missing_returns_blank(self):
        assert _adp_cid({}) == ""

    def test_location_from_name_code(self):
        locs = ADP_FEED["jobRequisitions"][0]["requisitionLocations"]
        assert _adp_location(locs) == "New York, NY, US"

    def test_location_bad_input(self):
        assert _adp_location(None) == ""
        assert _adp_location([{"nope": 1}]) == ""

    def test_job_url_from_cid(self):
        url = _adp_job_url("", CID, "9201122105498_1")
        assert "recruitment.html" in url and CID in url and "jobId=9201122105498_1" in url

    def test_job_url_reuses_portal(self):
        portal = "https://workforcenow.adp.com/x/recruitment.html?cid=" + CID
        url = _adp_job_url(portal, CID, "item1")
        assert url == portal + "&jobId=item1"

    def test_snippet_includes_location_and_pay(self):
        snip = _adp_snippet(
            "New York, NY, US",
            {
                "minimumRate": {"amountValue": 475000.0, "currencyCode": "USD"},
                "maximumRate": {"amountValue": 525000.0},
            },
        )
        assert "New York" in snip and "475,000" in snip and "525,000" in snip


class TestFetchAdpJson:
    def test_happy_path_parses_requisitions(self, monkeypatch):
        fake = AdpFakeRequests(AdpFakeResponse(json_data=ADP_FEED))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_adp_json("Rockefeller", {"ats_slug": CID})
        assert len(jobs) == 2
        titles = {j["title"] for j in jobs}
        assert "Senior Vice President, Power" in titles
        j0 = next(j for j in jobs if j["title"] == "Senior Vice President, Power")
        assert j0["external_id"] == "9201122105498_1"
        assert j0["location"] == "New York, NY, US"
        assert CID in j0["url"]
        # cid was passed to the feed URL.
        assert any(f"cid={CID}" in u for u in fake.calls)

    def test_ccid_from_flat_config(self, monkeypatch):
        # Registry flattens ats_config into the top-level config (prod shape).
        fake = AdpFakeRequests(AdpFakeResponse(json_data=ADP_FEED))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_adp_json("Skoll", {"cid": CID, "ccId": "9201357733031_3"})
        assert len(jobs) == 2
        assert any(f"cid={CID}&ccId=9201357733031_3&lang=en_US" in u for u in fake.calls)

    def test_ccid_from_nested_ats_config(self, monkeypatch):
        fake = AdpFakeRequests(AdpFakeResponse(json_data=ADP_FEED))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_adp_json("Skoll", {"ats_config": {"cid": CID, "ccId": "9201357733031_3"}})
        assert len(jobs) == 2
        assert any(f"cid={CID}&ccId=9201357733031_3&lang=en_US" in u for u in fake.calls)

    def test_no_ccid_leaves_feed_url_unchanged(self, monkeypatch):
        fake = AdpFakeRequests(AdpFakeResponse(json_data=ADP_FEED))
        monkeypatch.setattr(fetchers, "requests", fake)
        fetch_adp_json("Rockefeller", {"ats_slug": CID})
        assert all("ccId" not in u for u in fake.calls)

    def test_empty_feed_returns_empty(self, monkeypatch):
        fake = AdpFakeRequests(AdpFakeResponse(json_data=ADP_EMPTY))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_adp_json("Rockefeller", {"ats_slug": CID}) == []

    def test_no_cid_returns_empty_without_network(self, monkeypatch):
        fake = AdpFakeRequests(AdpFakeResponse(json_data=ADP_FEED))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_adp_json("Rockefeller", {}) == []
        assert fake.calls == []

    def test_404_returns_empty_without_crash(self, monkeypatch):
        fake = AdpFakeRequests(AdpFakeResponse(json_data=None, status=404))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_adp_json("Rockefeller", {"ats_slug": CID}) == []

    def test_network_exception_returns_empty_without_crash(self, monkeypatch):
        fake = AdpFakeRequests(raise_exc=RuntimeError("timeout"))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_adp_json("Rockefeller", {"ats_slug": CID}) == []


# ---------------------------------------------------------------------------
# --- from test_fetch_pinpoint.py ---
# Pinpoint public postings feed.
#
# BLOCKER NOTE: "pinpoint" was an auto-assignable ATS strategy in discover_ats.py
# (WORKING_ATS_STRATEGIES + ATS_PATTERNS) with NO registered fetcher — every
# company on this strategy hit ``error: no fetcher registered for strategy
# 'pinpoint'`` forever. ARIA (Advanced Research and Invention Agency, an active
# S-tier company) sat dead on it since 2026-06-24. ``pinpoint_postings.json`` is a
# trimmed real capture of ARIA's public feed (4 live roles, descriptions clipped,
# no auth, no personal data — public job listings only).
# ---------------------------------------------------------------------------

PINPOINT_SLUG = "aria"

PINPOINT_POSTINGS = _load_json("pinpoint_postings.json")


class PinpointFakeResponse:
    def __init__(self, *, json_data=None, status=200):
        self._json = json_data
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class PinpointFakeRequests:
    def __init__(self, response=None, *, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(url)
        if self.raise_exc:
            raise self.raise_exc
        return self.response


class TestPinpointLocation:
    def test_dedupes_name_and_province(self):
        # First posting: location.name == location.province == "London".
        job = PINPOINT_POSTINGS["data"][0]
        assert _pinpoint_location(job) == "Hybrid — London"

    def test_prefixes_remote_workplace(self):
        job = {"location": {"name": "Berlin"}, "workplace_type_text": "Remote"}
        assert _pinpoint_location(job) == "Remote — Berlin"

    def test_onsite_workplace_not_prefixed(self):
        job = {"location": {"name": "Paris"}, "workplace_type_text": "On-site"}
        assert _pinpoint_location(job) == "Paris"

    def test_falls_back_to_city_when_no_name(self):
        job = {"location": {"city": "Lisbon"}, "workplace_type_text": ""}
        assert _pinpoint_location(job) == "Lisbon"

    def test_missing_location_returns_workplace_only(self):
        assert _pinpoint_location({"workplace_type_text": "Remote"}) == "Remote"
        assert _pinpoint_location({}) == ""


class TestFetchPinpoint:
    def test_happy_path_parses_postings(self, monkeypatch):
        fake = PinpointFakeRequests(PinpointFakeResponse(json_data=PINPOINT_POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_pinpoint("Advanced Research and Invention Agency", PINPOINT_SLUG)
        assert len(jobs) == 4
        titles = {j["title"] for j in jobs}
        assert "Science and Technology Lead - Multi-Agent Security" in titles

        j0 = next(j for j in jobs if j["external_id"] == "463161")
        assert j0["location"] == "Hybrid — London"
        assert j0["department"] == "Programmes"
        assert j0["compensation"] == "£70,000 - £105,000 / year"
        assert j0["url"].endswith("/en/postings/f9e555e3-ffdb-4587-9bcf-47961c2fbb9d")
        # description HTML is stripped to plain text
        assert "<div>" not in j0["full_description"]
        assert "Salary Levels" in j0["full_description"]
        # the slug was used to build the feed URL
        assert f"{PINPOINT_SLUG}.pinpointhq.com" in fake.calls[0]

    def test_no_pagination_single_request(self, monkeypatch):
        # The feed is one `data` array — exactly one GET, no page loop.
        fake = PinpointFakeRequests(PinpointFakeResponse(json_data=PINPOINT_POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        fetch_pinpoint("Advanced Research and Invention Agency", PINPOINT_SLUG)
        assert len(fake.calls) == 1

    def test_empty_board_returns_empty(self, monkeypatch):
        fake = PinpointFakeRequests(PinpointFakeResponse(json_data={"data": []}))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_pinpoint("Empty Org", "emptyorg") == []

    def test_missing_data_key_returns_empty(self, monkeypatch):
        fake = PinpointFakeRequests(PinpointFakeResponse(json_data={}))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_pinpoint("Weird Org", "weird") == []

    def test_missing_fields_do_not_crash(self, monkeypatch):
        # A posting with no location/job/compensation still yields a row.
        fake = PinpointFakeRequests(
            PinpointFakeResponse(json_data={"data": [{"id": "1", "title": "Role", "url": "u"}]})
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_pinpoint("Sparse Org", "sparse")
        assert len(jobs) == 1
        j = jobs[0]
        assert j["title"] == "Role"
        assert j["location"] == ""
        assert j["department"] == ""
        assert j["compensation"] == ""
        assert j["external_id"] == "1"

    def test_compensation_hidden_when_not_visible(self, monkeypatch):
        fake = PinpointFakeRequests(
            PinpointFakeResponse(
                json_data={
                    "data": [
                        {
                            "id": "2",
                            "title": "R",
                            "compensation": "£50k",
                            "compensation_visible": False,
                        }
                    ]
                }
            )
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_pinpoint("Hidden Comp Org", "hidden")
        assert jobs[0]["compensation"] == ""

    def test_missing_id_falls_back_to_hash(self, monkeypatch):
        fake = PinpointFakeRequests(
            PinpointFakeResponse(json_data={"data": [{"title": "No ID Role"}]})
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_pinpoint("NoId Org", "noid")
        assert len(jobs[0]["external_id"]) == 12  # md5[:12] fallback

    def test_404_returns_empty_without_crash(self, monkeypatch):
        fake = PinpointFakeRequests(PinpointFakeResponse(json_data=None, status=404))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_pinpoint("Advanced Research and Invention Agency", PINPOINT_SLUG) == []

    def test_network_exception_returns_empty_without_crash(self, monkeypatch):
        fake = PinpointFakeRequests(raise_exc=RuntimeError("timeout"))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_pinpoint("Advanced Research and Invention Agency", PINPOINT_SLUG) == []


class TestPinpointRegistered:
    def test_strategy_registered(self):
        from fetchers.registry import COMPANY_FETCHERS

        assert "pinpoint" in COMPANY_FETCHERS

    def test_entry_unpacks_slug_from_config(self, monkeypatch):
        from fetchers.registry import COMPANY_FETCHERS

        fake = PinpointFakeRequests(PinpointFakeResponse(json_data=PINPOINT_POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = COMPANY_FETCHERS["pinpoint"]("ARIA", {"slug": PINPOINT_SLUG})
        assert len(jobs) == 4


# ---------------------------------------------------------------------------
# --- from test_fetch_smartrecruiters.py ---
# SmartRecruiters public Postings API.
#
# DRIFT NOTE: "smartrecruiters" was already a recognized ATS strategy string
# (discover_ats.py detects it and marks it "working"), but no fetcher was ever
# registered for it — every company on this strategy silently returned zero
# vacancies. ``smartrecruiters_postings.json`` is a trimmed real capture of
# Global Development Incubator's public postings feed (3 live roles, no auth).
# ---------------------------------------------------------------------------

SR_POSTINGS = _load_json("smartrecruiters_postings.json")
SR_SLUG = "GlobalDevelopmentIncubator"


class SrFakeResponse:
    def __init__(self, *, json_data=None, status=200):
        self._json = json_data
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SrFakeRequests:
    def __init__(self, response=None, *, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(url)
        if self.raise_exc:
            raise self.raise_exc
        return self.response


class TestSmartrecruitersHelpers:
    def test_location_prefers_full_location(self):
        loc = SR_POSTINGS["content"][0]["location"]
        assert _sr_location(loc) == "Washington, DC, United States"

    def test_location_builds_from_parts_when_no_full_location(self):
        assert _sr_location({"city": "Berlin", "country": "de"}) == "Berlin, de"

    def test_location_bad_input_returns_blank(self):
        assert _sr_location(None) == ""
        assert _sr_location({}) == ""

    def test_department_falls_back_to_function_label(self):
        # first posting has an empty department dict, function.label = "Science"
        assert _sr_department(SR_POSTINGS["content"][0]) == "Science"

    def test_department_used_when_present(self):
        # third posting has a real department label
        assert _sr_department(SR_POSTINGS["content"][2]) == "Executive"


class TestFetchSmartrecruiters:
    def test_happy_path_parses_postings(self, monkeypatch):
        fake = SrFakeRequests(SrFakeResponse(json_data=SR_POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_smartrecruiters("Global Development Incubator", SR_SLUG)
        assert len(jobs) == 3
        titles = {j["title"] for j in jobs}
        assert "Deputy Executive Director, Institute for Law & Organizing" in titles
        j0 = next(j for j in jobs if j["external_id"] == "744000135044794")
        assert j0["location"] == "Washington, DC, United States"
        assert j0["url"] == f"https://jobs.smartrecruiters.com/{SR_SLUG}/744000135044794"
        assert SR_SLUG in fake.calls[0]

    def test_empty_feed_returns_empty(self, monkeypatch):
        fake = SrFakeRequests(SrFakeResponse(json_data={"totalFound": 0, "content": []}))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_smartrecruiters("Empty Org", "EmptyOrg") == []

    def test_no_slug_returns_empty_without_network(self, monkeypatch):
        fake = SrFakeRequests(SrFakeResponse(json_data=SR_POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_smartrecruiters("Nowhere", "") == []
        assert fake.calls == []

    def test_404_returns_empty_without_crash(self, monkeypatch):
        fake = SrFakeRequests(SrFakeResponse(json_data=None, status=404))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_smartrecruiters("Global Development Incubator", SR_SLUG) == []

    def test_network_exception_returns_empty_without_crash(self, monkeypatch):
        fake = SrFakeRequests(raise_exc=RuntimeError("timeout"))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_smartrecruiters("Global Development Incubator", SR_SLUG) == []

    def test_pagination_stops_at_total_found(self, monkeypatch):
        # A single page already covers totalFound (3) -> exactly one call.
        fake = SrFakeRequests(SrFakeResponse(json_data=SR_POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        fetch_smartrecruiters("Global Development Incubator", SR_SLUG)
        assert len(fake.calls) == 1


class PagingFakeRequests:
    """Serves postings paginated by the URL's ``offset``/``limit`` params, so a
    real multi-page traversal (totalFound > limit) can be exercised end-to-end."""

    def __init__(self, postings: list, total: int):
        self.postings = postings
        self.total = total
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(url)
        q = parse_qs(urlparse(url).query)
        offset = int(q.get("offset", ["0"])[0])
        limit = int(q.get("limit", ["100"])[0])
        page = self.postings[offset : offset + limit]
        return SrFakeResponse(json_data={"totalFound": self.total, "content": page})


class TestSmartrecruitersMultiPage:
    def test_traverses_all_pages_and_terminates(self, monkeypatch):
        # 150 roles over a 100-per-page feed → page @0 (100) then @100 (50);
        # offset 200 >= totalFound 150 stops the loop. Proves traversal both
        # collects every page AND terminates (no infinite loop, no early stop).
        postings = [{"id": str(1000 + i), "name": f"Role {i}"} for i in range(150)]
        fake = PagingFakeRequests(postings, total=150)
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_smartrecruiters("Big Org", "BigOrg")
        assert len(jobs) == 150
        assert len({j["external_id"] for j in jobs}) == 150  # all unique, no dupes
        offsets = [int(parse_qs(urlparse(u).query)["offset"][0]) for u in fake.calls]
        assert offsets == [0, 100]  # exactly two pages, then stops


# ---------------------------------------------------------------------------
# --- from test_fetch_successfactors.py ---
# SAP SuccessFactors CSB tile feed (U4).
#
# DRIFT NOTE: the plan assumed a "tile-search JSON" endpoint. The live
# Career Site Builder ``/tile-search-results/`` endpoint actually returns an
# HTML fragment of ``<li class="job-tile ...">`` tiles (verified against
# RobertBoschStiftung, whose 3 test postings are captured in
# ``fixtures/successfactors_tiles.html``). The fetcher parses HTML tiles; the
# saved fixture is that real HTML fragment (not JSON).
# ---------------------------------------------------------------------------

TILES_HTML = _load("successfactors_tiles.html")
EMPTY_HTML = _load("successfactors_empty.html")


def _tile(job_id: str, title: str, site: str = "Acme", loc: str = "") -> str:
    loc_html = f'<span class="jobLocation">{loc}</span>' if loc else ""
    slug = re.sub(r"\s+", "-", title)
    return (
        f'<li class="job-tile job-id-{job_id} job-row-index-1" '
        f'data-url="/{site}/job/{slug}-{job_id}/{job_id}/">'
        f'<a class="jobTitle-link" href="/{site}/job/{slug}/{job_id}/">{title}</a>'
        f"{loc_html}</li>"
    )


def _page(*tiles: str) -> str:
    return "<!DOCTYPE html>\n" + "\n".join(tiles)


class SfFakeResponse:
    def __init__(self, *, text="", status=200, cookies=None):
        self.text = text
        self.status_code = status
        self.cookies = cookies or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SfFakeRequests:
    """Routes SuccessFactors GETs; startrow → page body via ``pages`` map."""

    def __init__(self, pages: dict, *, bootstrap=None, raise_on=None):
        # pages: {startrow_int: html}; missing startrow → empty shell.
        self.pages = pages
        self.bootstrap = (
            bootstrap
            if bootstrap is not None
            else SfFakeResponse(text="<html>search</html>", cookies={"JSESSIONID": "abc"})
        )
        self.raise_on = raise_on or (lambda url: False)
        self.calls = []

    def get(self, url, headers=None, cookies=None, timeout=None, params=None):
        self.calls.append(url)
        if self.raise_on(url):
            raise RuntimeError("boom")
        if "tile-search-results" not in url:
            return self.bootstrap
        m = re.search(r"startrow=(\d+)", url)
        startrow = int(m.group(1)) if m else 0
        return SfFakeResponse(text=self.pages.get(startrow, EMPTY_HTML))


class TestSfBaseUrl:
    def test_from_explicit_url(self):
        assert _sf_base_url({"url": "https://jobs.ilo.org/"}) == "https://jobs.ilo.org"

    def test_from_ats_slug_default_host(self):
        assert (
            _sf_base_url({"ats_slug": "BertelsmannStiftung"})
            == "https://jobsearch.createyourowncareer.com/BertelsmannStiftung"
        )

    def test_strips_search_suffix_and_query(self):
        got = _sf_base_url(
            {
                "url": "https://jobsearch.createyourowncareer.com/BertelsmannStiftung/search/?locale=en_GB"
            }
        )
        assert got == "https://jobsearch.createyourowncareer.com/BertelsmannStiftung"

    def test_empty_config_returns_blank(self):
        assert _sf_base_url({}) == ""


class TestParseTiles:
    def test_parses_real_fixture_three_jobs(self):
        jobs = _parse_successfactors_tiles(
            TILES_HTML, "https://jobsearch.createyourowncareer.com/RobertBoschStiftung", "RBS"
        )
        assert len(jobs) == 3
        titles = {j["title"] for j in jobs}
        assert "Test Stepstone II" in titles
        # url absolutised against the host, external_id is the numeric job-id.
        j0 = next(j for j in jobs if j["title"] == "Test Stepstone II")
        assert j0["url"].startswith("https://jobsearch.createyourowncareer.com/")
        assert j0["external_id"] == "1410167633"

    def test_empty_fragment_returns_empty(self):
        assert _parse_successfactors_tiles(EMPTY_HTML, "https://x", "RBS") == []

    def test_extracts_location_when_present(self):
        html = _page(_tile("111", "Head of Strategy", loc="Berlin, Germany"))
        jobs = _parse_successfactors_tiles(html, "https://jobs.example.org", "Ex")
        assert len(jobs) == 1
        assert jobs[0]["location"] == "Berlin, Germany"

    def test_malformed_html_does_not_crash(self):
        assert _parse_successfactors_tiles("<li class='job-tile'>broken", "https://x", "RBS") == []


class TestFetchSuccessfactors:
    def test_happy_path_parses_fixture(self, monkeypatch):
        fake = SfFakeRequests({0: TILES_HTML})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_successfactors("RBS", {"ats_slug": "RobertBoschStiftung"})
        assert len(jobs) == 3
        assert all(j["title"] and j["url"] and j["external_id"] for j in jobs)

    def test_pagination_startrow_advances_and_dedups(self, monkeypatch):
        # page @0 has two tiles, page @25 one NEW tile, @50 empty → stop.
        pages = {
            0: _page(_tile("1", "Role A"), _tile("2", "Role B")),
            25: _page(_tile("3", "Role C")),
        }
        fake = SfFakeRequests(pages)
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_successfactors("Ex", {"url": "https://jobs.example.org/"})
        ids = [j["external_id"] for j in jobs]
        assert ids == ["1", "2", "3"]  # all pages, in order
        assert len(ids) == len(set(ids))  # no duplicates
        # startrow advanced 0 → 25 → 50 (50 empty triggers the stop).
        tile_calls = [u for u in fake.calls if "tile-search-results" in u]
        assert any("startrow=0" in u for u in tile_calls)
        assert any("startrow=25" in u for u in tile_calls)
        assert any("startrow=50" in u for u in tile_calls)

    def test_repeated_page_dedups_and_stops(self, monkeypatch):
        # Server ignores startrow and re-serves page 1 → all seen → stop at 3.
        fake = SfFakeRequests({0: TILES_HTML, 25: TILES_HTML, 50: TILES_HTML})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_successfactors("RBS", {"ats_slug": "RobertBoschStiftung"})
        assert len(jobs) == 3

    def test_empty_site_returns_empty_list(self, monkeypatch):
        fake = SfFakeRequests({})  # every startrow → empty shell
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_successfactors("ILO", {"url": "https://jobs.ilo.org/"}) == []

    def test_404_on_tiles_returns_empty_without_crash(self, monkeypatch):
        fake = SfFakeRequests({}, raise_on=lambda url: "tile-search-results" in url)
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_successfactors("ILO", {"url": "https://jobs.ilo.org/"}) == []

    def test_no_config_returns_empty(self, monkeypatch):
        # No network should be touched when there is nothing to fetch.
        fake = SfFakeRequests({})
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_successfactors("Nowhere", {}) == []
        assert fake.calls == []


# ---------------------------------------------------------------------------
# "sitemap" backend variant — for hosts whose CSB tile-search is a dead end
#
# DRIFT NOTE: ILO's jobs.ilo.org tile-search-results endpoint always returns
# the 16-byte empty shell (its live candidate-facing backend is the classic
# Recruiting/RCM UI, not CSB) even though its SEO sitemap.xml stays accurate.
# ``successfactors_sitemap.xml`` and ``successfactors_job_detail.html`` are
# trimmed real captures of that sitemap and one of its job detail pages.
# ---------------------------------------------------------------------------

SITEMAP_XML = _load("successfactors_sitemap.xml")
JOB_DETAIL_HTML = _load("successfactors_job_detail.html")

JOB_URL_1 = (
    "https://jobs.ilo.org/job/Cairo-Social-Health-Protection-Technical-Officer-P3/1399744233/"
)
JOB_URL_2 = "https://jobs.ilo.org/job/Windhoek-Technical-Officer-P2-%28DC%29/1409512033/"


class FakeSitemapRequests:
    """Routes GETs for ``sitemap.xml`` and per-job detail pages by URL."""

    def __init__(self, *, sitemap: str, details: dict, raise_on=None):
        self.sitemap = sitemap
        self.details = details  # {url: html}
        self.raise_on = raise_on or (lambda url: False)
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(url)
        if self.raise_on(url):
            raise RuntimeError("boom")
        if url.endswith("/sitemap.xml"):
            return SfFakeResponse(text=self.sitemap)
        return SfFakeResponse(text=self.details.get(url, ""))


class TestSfSitemapJobUrls:
    def test_parses_urls_via_fake_http(self, monkeypatch):
        fake = FakeSitemapRequests(sitemap=SITEMAP_XML, details={})
        monkeypatch.setattr(fetchers, "requests", fake)
        urls = _sf_sitemap_job_urls("https://jobs.ilo.org")
        assert urls == [JOB_URL_1, JOB_URL_2]

    def test_malformed_xml_returns_empty(self, monkeypatch):
        fake = FakeSitemapRequests(sitemap="<not><valid", details={})
        monkeypatch.setattr(fetchers, "requests", fake)
        assert _sf_sitemap_job_urls("https://jobs.ilo.org") == []


class TestSfJobDetail:
    def test_parses_title_and_full_description(self, monkeypatch):
        fake = FakeSitemapRequests(sitemap=SITEMAP_XML, details={JOB_URL_1: JOB_DETAIL_HTML})
        monkeypatch.setattr(fetchers, "requests", fake)
        job = _sf_job_detail(JOB_URL_1)
        assert job["title"] == "Social Health Protection Technical Officer - P3"
        assert job["external_id"] == "1399744233"
        assert job["url"] == JOB_URL_1
        assert "Grade: P3" in job["full_description"]
        assert "Conditions of employment" in job["full_description"]
        # <style>/<script> content must never leak into the saved description.
        assert "footerNoise" not in job["full_description"]
        assert "unify-apply-now:focus" not in job["full_description"]

    def test_missing_title_returns_none(self, monkeypatch):
        fake = FakeSitemapRequests(
            sitemap=SITEMAP_XML, details={JOB_URL_1: "<html>no job here</html>"}
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        assert _sf_job_detail(JOB_URL_1) is None

    def test_fetch_failure_returns_none_without_crash(self, monkeypatch):
        fake = FakeSitemapRequests(sitemap=SITEMAP_XML, details={}, raise_on=lambda url: True)
        monkeypatch.setattr(fetchers, "requests", fake)
        assert _sf_job_detail(JOB_URL_1) is None


class TestFetchSuccessfactorsSitemapBackend:
    def test_sitemap_backend_parses_both_jobs(self, monkeypatch):
        fake = FakeSitemapRequests(
            sitemap=SITEMAP_XML,
            details={JOB_URL_1: JOB_DETAIL_HTML, JOB_URL_2: JOB_DETAIL_HTML},
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_successfactors("ILO", {"url": "https://jobs.ilo.org", "sf_backend": "sitemap"})
        assert len(jobs) == 2
        assert {j["external_id"] for j in jobs} == {"1399744233", "1409512033"}
        assert all(j["title"] and j["full_description"] for j in jobs)

    def test_default_config_still_uses_tile_backend(self, monkeypatch):
        # No sf_backend set → unaffected by the new code path.
        fake = SfFakeRequests({0: TILES_HTML})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_successfactors("RBS", {"ats_slug": "RobertBoschStiftung"})
        assert len(jobs) == 3
        assert not any("sitemap.xml" in u for u in fake.calls)

    def test_sitemap_fetch_failure_returns_empty_without_crash(self, monkeypatch):
        fake = FakeSitemapRequests(sitemap=SITEMAP_XML, details={}, raise_on=lambda url: True)
        monkeypatch.setattr(fetchers, "requests", fake)
        assert (
            fetch_successfactors("ILO", {"url": "https://jobs.ilo.org", "sf_backend": "sitemap"})
            == []
        )

    def test_one_job_detail_failure_skips_only_that_job(self, monkeypatch):
        fake = FakeSitemapRequests(
            sitemap=SITEMAP_XML,
            details={
                JOB_URL_1: JOB_DETAIL_HTML
            },  # JOB_URL_2 has no entry -> empty text -> no title
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_successfactors("ILO", {"url": "https://jobs.ilo.org", "sf_backend": "sitemap"})
        assert len(jobs) == 1
        assert jobs[0]["external_id"] == "1399744233"


# ---------------------------------------------------------------------------
# sitemap backend — shape handling (<sitemapindex>) + N+1 detail-fetch cap
# ---------------------------------------------------------------------------

_SM_NS = 'xmlns="http://www.google.com/schemas/sitemap/0.9"'


def _urlset(urls: list[str]) -> str:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset {_SM_NS}>{body}</urlset>'


def _sitemapindex(locs: list[str]) -> str:
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in locs)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex {_SM_NS}>{body}</sitemapindex>'


def _detail_page(title: str) -> str:
    return (
        f'<html><span itemprop="title">{title}</span>'
        f'<div class="job"><span itemprop="description">Body</span></div></html>'
    )


class TestSfSitemapIndexShape:
    def test_sitemapindex_root_raises_fetch_error(self, monkeypatch):
        # A <sitemapindex> nests sub-sitemaps; findall("sm:url") would return 0.
        # The raw helper must fail loudly instead of silently yielding nothing.
        fake = FakeSitemapRequests(
            sitemap=_sitemapindex(["https://jobs.ilo.org/sitemap-1.xml"]), details={}
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        with pytest.raises(FetchError) as exc:
            _sf_sitemap_job_urls("https://jobs.ilo.org")
        assert exc.value.reason == "sitemap_index"

    def test_sitemapindex_backend_records_error_not_silent_zero(self, monkeypatch):
        fake = FakeSitemapRequests(
            sitemap=_sitemapindex(["https://jobs.ilo.org/sitemap-1.xml"]), details={}
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        fetchers._last_fetch_errors.pop("ILO", None)  # drop any cross-test residue
        jobs = fetch_successfactors("ILO", {"url": "https://jobs.ilo.org", "sf_backend": "sitemap"})
        assert jobs == []  # no jobs, but not a silent success:
        assert "sitemap_index" in fetchers.get_fetch_errors().get("ILO", "")


class TestSfSitemapDetailCap:
    def test_detail_fetches_are_capped(self, monkeypatch):
        # More job URLs than the cap → only the first N detail pages are fetched.
        # An uncapped one-GET-per-<loc> loop would stall the daily run.
        from fetchers.ats import successfactors as sf

        monkeypatch.setattr(sf, "_SITEMAP_MAX_DETAIL_FETCHES", 3)
        urls = [f"https://jobs.ilo.org/job/Role-{i}/{1000 + i}/" for i in range(5)]
        details = {u: _detail_page(f"Role {i}") for i, u in enumerate(urls)}
        fake = FakeSitemapRequests(sitemap=_urlset(urls), details=details)
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_successfactors("ILO", {"url": "https://jobs.ilo.org", "sf_backend": "sitemap"})
        assert len(jobs) == 3  # capped, not all 5
        detail_calls = [u for u in fake.calls if "/job/" in u]
        assert len(detail_calls) == 3  # the loop stopped at the cap

    def test_cap_constant_is_a_sane_positive_int(self):
        from fetchers.ats import successfactors as sf

        assert isinstance(sf._SITEMAP_MAX_DETAIL_FETCHES, int)
        assert sf._SITEMAP_MAX_DETAIL_FETCHES > 0


# ---------------------------------------------------------------------------
# --- from test_fetch_teamtailor_rss.py ---
# Teamtailor RSS feed.
#
# DRIFT NOTE: some Teamtailor customers publish their feed on a custom
# career-site domain instead of the default ``<slug>.teamtailor.com`` host —
# e.g. Chatham House's feed lives at ``careers.chathamhouse.org/jobs.rss``,
# not ``chathamhouse.teamtailor.com/jobs.rss`` (404). The fetcher now tries a
# configured ``careers_url`` host first and falls back to the default host,
# so neither a missing nor an unrelated ``careers_url`` regresses companies
# that already worked. ``teamtailor_chatham_house_jobs.rss`` is a trimmed real
# capture of that feed.
# ---------------------------------------------------------------------------

RSS_FEED = _load("teamtailor_chatham_house_jobs.rss")

# A custom career-site domain answering 200 with a marketing/SPA page instead
# of the feed — must NOT be mistaken for an empty feed (it has to fall through).
SPA_HTML = "<!DOCTYPE html>\n<html><head><title>Careers</title></head><body>Apply now</body></html>"

# A genuine but empty RSS feed (a real Teamtailor org with zero openings): a
# valid feed, so it must be accepted as [] and stop the host fallback.
EMPTY_RSS = '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Jobs</title></channel></rss>'


class TtFakeResponse:
    def __init__(self, *, text="", status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TtFakeRequests:
    """Routes GETs by host; an unlisted host 404s like a real broken feed."""

    def __init__(self, pages: dict):
        self.pages = pages  # {host: FakeResponse}
        self.calls = []

    def get(self, url, timeout=None, **kwargs):
        self.calls.append(url)
        host = urlparse(url).netloc
        if host not in self.pages:
            return TtFakeResponse(status=404)
        return self.pages[host]


class TestTeamtailorHosts:
    def test_no_careers_url_uses_default(self):
        assert _teamtailor_hosts("chathamhouse") == ["chathamhouse.teamtailor.com"]

    def test_custom_domain_tried_first(self):
        hosts = _teamtailor_hosts("chathamhouse", "https://careers.chathamhouse.org/jobs")
        assert hosts == ["careers.chathamhouse.org", "chathamhouse.teamtailor.com"]

    def test_careers_url_matching_default_host_not_duplicated(self):
        hosts = _teamtailor_hosts("acme", "https://acme.teamtailor.com/jobs")
        assert hosts == ["acme.teamtailor.com"]

    def test_blank_careers_url_uses_default(self):
        assert _teamtailor_hosts("acme", "") == ["acme.teamtailor.com"]


class TestFetchTeamtailorRss:
    def test_custom_domain_feed_parsed(self, monkeypatch):
        fake = TtFakeRequests({"careers.chathamhouse.org": TtFakeResponse(text=RSS_FEED)})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss(
            "Chatham House", "chathamhouse", careers_url="https://careers.chathamhouse.org/jobs"
        )
        assert len(jobs) == 2
        titles = {j["title"] for j in jobs}
        assert "Senior Research Fellow – International Security" in titles
        assert all(j["url"] and j["external_id"] for j in jobs)

    def test_custom_domain_tried_before_broken_default(self, monkeypatch):
        # chathamhouse.teamtailor.com 404s (unlisted); custom domain works and
        # is tried first, so the default host is never even called.
        fake = TtFakeRequests({"careers.chathamhouse.org": TtFakeResponse(text=RSS_FEED)})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss(
            "Chatham House", "chathamhouse", careers_url="https://careers.chathamhouse.org/jobs"
        )
        assert len(jobs) == 2
        assert fake.calls == ["https://careers.chathamhouse.org/jobs.rss"]

    def test_unrelated_careers_url_falls_back_to_default_host(self, monkeypatch):
        # careers_url points at a normal marketing page, not the RSS host;
        # the default <slug>.teamtailor.com host still works.
        fake = TtFakeRequests({"acme.teamtailor.com": TtFakeResponse(text=RSS_FEED)})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss("Acme", "acme", careers_url="https://www.acme.example/careers")
        assert len(jobs) == 2
        assert fake.calls == [
            "https://www.acme.example/jobs.rss",
            "https://acme.teamtailor.com/jobs.rss",
        ]

    def test_no_careers_url_uses_default_host_only(self, monkeypatch):
        fake = TtFakeRequests({"acme.teamtailor.com": TtFakeResponse(text=RSS_FEED)})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss("Acme", "acme")
        assert len(jobs) == 2
        assert fake.calls == ["https://acme.teamtailor.com/jobs.rss"]

    def test_both_hosts_broken_returns_empty_without_crash(self, monkeypatch):
        fake = TtFakeRequests({})  # every host 404s
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss("Acme", "acme", careers_url="https://www.acme.example/careers")
        assert jobs == []
        # both candidate hosts were tried before giving up.
        assert len(fake.calls) == 2

    def test_custom_domain_200_html_falls_through_to_default_host(self, monkeypatch):
        # The custom career-site domain answers 200 with a marketing/SPA page,
        # not the RSS feed. A plain 200 must NOT end the fallback: the fetcher
        # has to reject the non-feed body and try the default host, which works.
        fake = TtFakeRequests(
            {
                "careers.acme.example": TtFakeResponse(text=SPA_HTML),
                "acme.teamtailor.com": TtFakeResponse(text=RSS_FEED),
            }
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss("Acme", "acme", careers_url="https://careers.acme.example/jobs")
        assert len(jobs) == 2  # the working default host's feed, not the SPA
        assert fake.calls == [
            "https://careers.acme.example/jobs.rss",  # tried first, rejected
            "https://acme.teamtailor.com/jobs.rss",  # fell through to the real feed
        ]

    def test_valid_empty_feed_on_custom_domain_returns_empty_without_default(self, monkeypatch):
        # A real Teamtailor org with zero openings serves a valid but empty feed
        # on its custom domain. That is a legitimate feed, so it must be accepted
        # as [] and the default host must never be touched (no false failure).
        fake = TtFakeRequests({"careers.acme.example": TtFakeResponse(text=EMPTY_RSS)})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss("Acme", "acme", careers_url="https://careers.acme.example/jobs")
        assert jobs == []
        assert fake.calls == ["https://careers.acme.example/jobs.rss"]
