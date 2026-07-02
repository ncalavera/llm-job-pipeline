"""Unit tests for fetch_smartrecruiters (SmartRecruiters public Postings API).

DRIFT NOTE: "smartrecruiters" was already a recognized ATS strategy string
(discover_ats.py detects it and marks it "working"), but no fetcher was ever
registered for it — every company on this strategy silently returned zero
vacancies. ``smartrecruiters_postings.json`` is a trimmed real capture of
Global Development Incubator's public postings feed (3 live roles, no auth).
"""

import json
import os
from urllib.parse import parse_qs, urlparse

import fetchers
from fetchers import fetch_smartrecruiters, _sr_location, _sr_department

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_json(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


POSTINGS = _load_json("smartrecruiters_postings.json")
SLUG = "GlobalDevelopmentIncubator"


class FakeResponse:
    def __init__(self, *, json_data=None, status=200):
        self._json = json_data
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    def __init__(self, response=None, *, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(url)
        if self.raise_exc:
            raise self.raise_exc
        return self.response


# ---------------------------------------------------------------------------
# helpers (pure)
# ---------------------------------------------------------------------------


class TestSmartrecruitersHelpers:
    def test_location_prefers_full_location(self):
        loc = POSTINGS["content"][0]["location"]
        assert _sr_location(loc) == "Washington, DC, United States"

    def test_location_builds_from_parts_when_no_full_location(self):
        assert _sr_location({"city": "Berlin", "country": "de"}) == "Berlin, de"

    def test_location_bad_input_returns_blank(self):
        assert _sr_location(None) == ""
        assert _sr_location({}) == ""

    def test_department_falls_back_to_function_label(self):
        # first posting has an empty department dict, function.label = "Science"
        assert _sr_department(POSTINGS["content"][0]) == "Science"

    def test_department_used_when_present(self):
        # third posting has a real department label
        assert _sr_department(POSTINGS["content"][2]) == "Executive"


# ---------------------------------------------------------------------------
# fetch_smartrecruiters (end-to-end with mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchSmartrecruiters:
    def test_happy_path_parses_postings(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_smartrecruiters("Global Development Incubator", SLUG)
        assert len(jobs) == 3
        titles = {j["title"] for j in jobs}
        assert "Deputy Executive Director, Institute for Law & Organizing" in titles
        j0 = next(j for j in jobs if j["external_id"] == "744000135044794")
        assert j0["location"] == "Washington, DC, United States"
        assert j0["url"] == f"https://jobs.smartrecruiters.com/{SLUG}/744000135044794"
        assert SLUG in fake.calls[0]

    def test_empty_feed_returns_empty(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data={"totalFound": 0, "content": []}))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_smartrecruiters("Empty Org", "EmptyOrg") == []

    def test_no_slug_returns_empty_without_network(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_smartrecruiters("Nowhere", "") == []
        assert fake.calls == []

    def test_404_returns_empty_without_crash(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=None, status=404))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_smartrecruiters("Global Development Incubator", SLUG) == []

    def test_network_exception_returns_empty_without_crash(self, monkeypatch):
        fake = FakeRequests(raise_exc=RuntimeError("timeout"))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_smartrecruiters("Global Development Incubator", SLUG) == []

    def test_pagination_stops_at_total_found(self, monkeypatch):
        # A single page already covers totalFound (3) -> exactly one call.
        fake = FakeRequests(FakeResponse(json_data=POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        fetch_smartrecruiters("Global Development Incubator", SLUG)
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
        return FakeResponse(json_data={"totalFound": self.total, "content": page})


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
