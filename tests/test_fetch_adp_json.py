"""Unit tests for fetch_adp_json (ADP Workforce Now public feed, U5).

Fixture ``fixtures/adp_job_requisitions.json`` is the real public feed for
Rockefeller Foundation (cid 24726181-…), captured live; ``adp_empty.json`` is
the same shape with an empty ``jobRequisitions`` array.
"""

import json
import os


import fetchers
from fetchers import fetch_adp_json, _adp_location, _adp_job_url, _adp_snippet, _adp_cid

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CID = "24726181-f57f-46a1-824d-3c8a89c3328a"


def _load_json(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


ADP_FEED = _load_json("adp_job_requisitions.json")
ADP_EMPTY = _load_json("adp_empty.json")


class FakeResponse:
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


class FakeRequests:
    def __init__(self, response=None, *, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, url, headers=None, timeout=None, params=None):
        self.calls.append(url)
        if self.raise_exc:
            raise self.raise_exc
        return self.response


# ---------------------------------------------------------------------------
# helpers (pure)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# fetch_adp_json (end-to-end with mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchAdpJson:
    def test_happy_path_parses_requisitions(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=ADP_FEED))
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

    def test_ccid_appended_to_feed_url(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=ADP_FEED))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_adp_json("Skoll", {"ats_config": {"cid": CID, "ccId": "9201357733031_3"}})
        assert len(jobs) == 2
        assert any(f"cid={CID}&ccId=9201357733031_3&lang=en_US" in u for u in fake.calls)

    def test_no_ccid_leaves_feed_url_unchanged(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=ADP_FEED))
        monkeypatch.setattr(fetchers, "requests", fake)
        fetch_adp_json("Rockefeller", {"ats_slug": CID})
        assert all("ccId" not in u for u in fake.calls)

    def test_empty_feed_returns_empty(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=ADP_EMPTY))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_adp_json("Rockefeller", {"ats_slug": CID}) == []

    def test_no_cid_returns_empty_without_network(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=ADP_FEED))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_adp_json("Rockefeller", {}) == []
        assert fake.calls == []

    def test_404_returns_empty_without_crash(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=None, status=404))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_adp_json("Rockefeller", {"ats_slug": CID}) == []

    def test_network_exception_returns_empty_without_crash(self, monkeypatch):
        fake = FakeRequests(raise_exc=RuntimeError("timeout"))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_adp_json("Rockefeller", {"ats_slug": CID}) == []
