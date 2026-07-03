"""Unit tests for fetch_pinpoint (Pinpoint public postings feed).

BLOCKER NOTE: "pinpoint" was an auto-assignable ATS strategy in discover_ats.py
(WORKING_ATS_STRATEGIES + ATS_PATTERNS) with NO registered fetcher — every
company on this strategy hit ``error: no fetcher registered for strategy
'pinpoint'`` forever. ARIA (Advanced Research and Invention Agency, an active
S-tier company) sat dead on it since 2026-06-24. ``pinpoint_postings.json`` is a
trimmed real capture of ARIA's public feed (4 live roles, descriptions clipped,
no auth, no personal data — public job listings only).
"""

import json
import os

import fetchers
from fetchers import fetch_pinpoint, _pinpoint_location

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
SLUG = "aria"


def _load_json(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


POSTINGS = _load_json("pinpoint_postings.json")


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


class TestPinpointLocation:
    def test_dedupes_name_and_province(self):
        # First posting: location.name == location.province == "London".
        job = POSTINGS["data"][0]
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


# ---------------------------------------------------------------------------
# fetch_pinpoint (end-to-end with mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchPinpoint:
    def test_happy_path_parses_postings(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_pinpoint("Advanced Research and Invention Agency", SLUG)
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
        assert f"{SLUG}.pinpointhq.com" in fake.calls[0]

    def test_no_pagination_single_request(self, monkeypatch):
        # The feed is one `data` array — exactly one GET, no page loop.
        fake = FakeRequests(FakeResponse(json_data=POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        fetch_pinpoint("Advanced Research and Invention Agency", SLUG)
        assert len(fake.calls) == 1

    def test_empty_board_returns_empty(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data={"data": []}))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_pinpoint("Empty Org", "emptyorg") == []

    def test_missing_data_key_returns_empty(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data={}))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_pinpoint("Weird Org", "weird") == []

    def test_missing_fields_do_not_crash(self, monkeypatch):
        # A posting with no location/job/compensation still yields a row.
        fake = FakeRequests(
            FakeResponse(json_data={"data": [{"id": "1", "title": "Role", "url": "u"}]})
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
        fake = FakeRequests(
            FakeResponse(
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
        fake = FakeRequests(FakeResponse(json_data={"data": [{"title": "No ID Role"}]}))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_pinpoint("NoId Org", "noid")
        assert len(jobs[0]["external_id"]) == 12  # md5[:12] fallback

    def test_404_returns_empty_without_crash(self, monkeypatch):
        fake = FakeRequests(FakeResponse(json_data=None, status=404))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_pinpoint("Advanced Research and Invention Agency", SLUG) == []

    def test_network_exception_returns_empty_without_crash(self, monkeypatch):
        fake = FakeRequests(raise_exc=RuntimeError("timeout"))
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_pinpoint("Advanced Research and Invention Agency", SLUG) == []


class TestPinpointRegistered:
    def test_strategy_registered(self):
        from fetchers.registry import COMPANY_FETCHERS

        assert "pinpoint" in COMPANY_FETCHERS

    def test_entry_unpacks_slug_from_config(self, monkeypatch):
        from fetchers.registry import COMPANY_FETCHERS

        fake = FakeRequests(FakeResponse(json_data=POSTINGS))
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = COMPANY_FETCHERS["pinpoint"]("ARIA", {"slug": SLUG})
        assert len(jobs) == 4
