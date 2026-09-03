"""Unit tests for fetch_oracle_hcm (Oracle HCM Recruiting Cloud REST API).

Fixtures are the real UNDP responses (``estm.fa.em2.oraclecloud.com``, site
``CX_1``, keyword "accelerator lab"), trimmed: the search feed is split into two
pages of a 3-job listing so paging is exercised, and the detail record keeps one
job's description head plus its full flex-field block.
"""

import json
import os
import re

import fetchers
from fetchers import (
    _oracle_config,
    _oracle_description,
    _oracle_location,
    fetch_oracle_hcm,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CAREERS_URL = (
    "https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs"
    "?keyword=accelerator%20lab"
)
HOST = "https://estm.fa.em2.oraclecloud.com"


def _load(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


SEARCH_PAGE1 = _load("oracle_hcm_search_page1.json")
SEARCH_PAGE2 = _load("oracle_hcm_search_page2.json")
DETAIL = _load("oracle_hcm_detail.json")
EMPTY_SEARCH = {"items": [{"TotalJobsCount": 0, "requisitionList": []}]}
# Only the first job has a captured detail record; the other two get an empty
# one, which is also how a site behaves when a requisition is pulled mid-run.
DETAILS = {"36148": DETAIL}


class FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    """Routes search vs detail by URL, and search pages by offset."""

    def __init__(self, pages=None, details=DETAILS, detail_status=200):
        self.pages = pages if pages is not None else [SEARCH_PAGE1, SEARCH_PAGE2]
        self.details = details
        self.detail_status = detail_status
        self.calls = []

    def get(self, url, headers=None, timeout=None, params=None):
        self.calls.append(url)
        if "JobRequisitionDetails" in url:
            job_id = re.search(r"Id=%22(\d+)%22", url).group(1)
            return FakeResponse(self.details.get(job_id, {"items": [{}]}), self.detail_status)
        offset = int(re.search(r"offset=%22(\d+)%22", url).group(1))
        page = offset // 50
        return FakeResponse(self.pages[page] if page < len(self.pages) else EMPTY_SEARCH)


# ---------------------------------------------------------------------------
# helpers (pure)
# ---------------------------------------------------------------------------


class TestOracleHelpers:
    def test_config_parses_host_site_and_keyword_from_url(self):
        assert _oracle_config({"url": CAREERS_URL}) == (HOST, "CX_1", "accelerator lab")

    def test_explicit_keyword_overrides_the_url_query(self):
        _, _, keyword = _oracle_config({"url": CAREERS_URL, "keyword": "innovation"})
        assert keyword == "innovation"

    def test_config_falls_back_to_careers_url(self):
        assert _oracle_config({"careers_url": CAREERS_URL})[1] == "CX_1"

    def test_config_without_keyword_is_blank(self):
        url = f"{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/jobs"
        assert _oracle_config({"url": url}) == (HOST, "CX_1", "")

    def test_config_missing_url_returns_blanks(self):
        assert _oracle_config({}) == ("", "", "")

    def test_location_joins_secondary_locations(self):
        loc = _oracle_location(
            {
                "PrimaryLocation": "Bangkok, Thailand",
                "secondaryLocations": [{"Name": "Hanoi, Viet Nam"}, {"Name": "Bangkok, Thailand"}],
            }
        )
        assert loc == "Bangkok, Thailand | Hanoi, Viet Nam"

    def test_location_appends_workplace_type(self):
        loc = _oracle_location({"PrimaryLocation": "Madrid, Spain", "WorkplaceType": "Remote"})
        assert loc == "Madrid, Spain (Remote)"

    def test_location_empty_input(self):
        assert _oracle_location({}) == ""

    def test_description_appends_flex_fields(self):
        text = _oracle_description(
            {
                "ExternalDescriptionStr": "<p>Body text</p>",
                "ExternalResponsibilitiesStr": "<p>Do the work</p>",
                "requisitionFlexFields": [
                    {"Prompt": "Grade", "Value": "NPSA-10"},
                    {"Prompt": "Required Languages", "Value": "English"},
                    {"Prompt": "Empty", "Value": ""},
                ],
            }
        )
        assert "Body text" in text and "Do the work" in text
        assert "Grade: NPSA-10" in text
        assert "Required Languages: English" in text
        assert "Empty" not in text


# ---------------------------------------------------------------------------
# fetch_oracle_hcm (end-to-end with mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchOracleHcm:
    def test_pages_through_the_listing(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_oracle_hcm("UNDP", {"url": CAREERS_URL})
        assert [j["external_id"] for j in jobs] == ["36148", "36505", "36541"]
        searches = [u for u in fake.calls if "JobRequisitionDetails" not in u]
        assert len(searches) == 2  # stopped once TotalJobsCount was reached

    def test_keyword_and_site_reach_the_finder(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(fetchers, "requests", fake)
        fetch_oracle_hcm("UNDP", {"url": CAREERS_URL})
        search = next(u for u in fake.calls if "JobRequisitionDetails" not in u)
        assert "siteNumber=%22CX_1%22" in search
        assert "keyword=%22accelerator%20lab%22" in search

    def test_no_keyword_omits_the_filter(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(fetchers, "requests", fake)
        fetch_oracle_hcm("UNDP", {"url": f"{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/jobs"})
        assert all("keyword" not in u for u in fake.calls)

    def test_detail_merge_fills_description_and_deadline(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(fetchers, "requests", fake)
        job = fetch_oracle_hcm("UNDP", {"url": CAREERS_URL})[0]
        assert job["title"].startswith("Research Analyst")
        assert len(job["full_description"]) > 500
        assert "Agency: UNDP" in job["full_description"]
        assert "Grade:" in job["full_description"]
        assert job["deadline"] == "2026-09-11T03:59:00+00:00"
        assert job["snippet"] and len(job["snippet"]) <= 401

    def test_location_and_url_come_from_the_requisition(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_oracle_hcm("UNDP", {"url": CAREERS_URL})
        assert jobs[0]["location"] == "Bangkok, Thailand"
        assert jobs[1]["location"] == "Madrid, Spain"
        assert jobs[0]["url"] == (
            f"{HOST}/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/job/36148"
        )

    def test_detail_nulls_do_not_blank_search_fields(self, monkeypatch):
        blank = {"36148": {"items": [{"Title": None, "PrimaryLocation": None}]}}
        fake = FakeRequests(details=blank)
        monkeypatch.setattr(fetchers, "requests", fake)
        job = fetch_oracle_hcm("UNDP", {"url": CAREERS_URL})[0]
        assert job["title"].startswith("Research Analyst")
        assert job["location"] == "Bangkok, Thailand"

    def test_detail_failure_keeps_the_job(self, monkeypatch):
        fake = FakeRequests(detail_status=500)
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_oracle_hcm("UNDP", {"url": CAREERS_URL})
        assert len(jobs) == 3
        assert jobs[0]["full_description"] == ""
        assert jobs[0]["snippet"] == "Duties and Responsibilities"

    def test_empty_listing_returns_empty(self, monkeypatch):
        fake = FakeRequests(pages=[EMPTY_SEARCH])
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_oracle_hcm("UNDP", {"url": CAREERS_URL}) == []

    def test_bad_config_returns_empty_without_network(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_oracle_hcm("UNDP", {"url": "https://example.com/careers"}) == []
        assert fake.calls == []

    def test_network_exception_returns_empty_without_crash(self, monkeypatch):
        class Boom:
            def get(self, *a, **kw):
                raise RuntimeError("timeout")

        monkeypatch.setattr(fetchers, "requests", Boom())
        assert fetch_oracle_hcm("UNDP", {"url": CAREERS_URL}) == []

    def test_registered_under_oracle_hcm(self):
        from fetchers.registry import COMPANY_FETCHERS

        assert "oracle_hcm" in COMPANY_FETCHERS
