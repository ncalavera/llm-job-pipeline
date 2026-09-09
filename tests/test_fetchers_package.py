"""fetchers package: registry dispatch + "failure is not emptiness".

Three different real-world outcomes must produce three different
fetch_status values instead of collapsing into an ambiguous empty list:

  * network timeout        -> "error: timeout"
  * HTTP 500 from the ATS  -> "error: http_500"
  * honest zero vacancies  -> "render_ok_zero"
"""

import requests as real_requests

import fetchers
from fetchers import FetchError
from fetchers.registry import BOARD_FETCHERS, COMPANY_FETCHERS, board_fetcher, company_fetcher
from fetch_vacancies import _resolve_fetch_status


# ---------------------------------------------------------------------------
# Fake HTTP plumbing
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, *, json_data=None, status=200):
        self._json = json_data
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHTTP:
    def __init__(self, *, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc

    def get(self, url, params=None, headers=None, timeout=None):
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _fresh_errors(monkeypatch):
    monkeypatch.setattr(fetchers, "_last_fetch_errors", {})


# ---------------------------------------------------------------------------
# Three outcomes, three distinct fetch_status values
# ---------------------------------------------------------------------------


class TestFailureIsNotEmptiness:
    def test_timeout_records_error_timeout(self, monkeypatch):
        _fresh_errors(monkeypatch)
        monkeypatch.setattr(
            fetchers, "requests", _FakeHTTP(raise_exc=real_requests.exceptions.Timeout("timed out"))
        )
        jobs = fetchers.fetch_greenhouse("OrgTimeout", "acme")
        assert jobs == []  # adapter still never crashes the run
        assert fetchers.get_fetch_errors()["OrgTimeout"] == "error: timeout"

    def test_http_500_records_error_http_500(self, monkeypatch):
        _fresh_errors(monkeypatch)
        monkeypatch.setattr(fetchers, "requests", _FakeHTTP(response=_Resp(status=500)))
        jobs = fetchers.fetch_greenhouse("OrgBroken", "acme")
        assert jobs == []
        assert fetchers.get_fetch_errors()["OrgBroken"] == "error: http_500"

    def test_honest_empty_records_nothing(self, monkeypatch):
        _fresh_errors(monkeypatch)
        monkeypatch.setattr(fetchers, "requests", _FakeHTTP(response=_Resp(json_data={"jobs": []})))
        jobs = fetchers.fetch_greenhouse("OrgEmpty", "acme")
        assert jobs == []
        assert "OrgEmpty" not in fetchers.get_fetch_errors()

    def test_three_outcomes_resolve_to_three_statuses(self, monkeypatch):
        """End-to-end: the same empty list resolves to three different statuses."""
        _fresh_errors(monkeypatch)
        for org, fake in [
            ("OrgTimeout", _FakeHTTP(raise_exc=real_requests.exceptions.Timeout("timed out"))),
            ("OrgBroken", _FakeHTTP(response=_Resp(status=500))),
            ("OrgEmpty", _FakeHTTP(response=_Resp(json_data={"jobs": []}))),
        ]:
            monkeypatch.setattr(fetchers, "requests", fake)
            assert fetchers.fetch_greenhouse(org, "acme") == []

        errors = fetchers.get_fetch_errors()
        statuses = {
            org: _resolve_fetch_status("ok", False, None, errors.get(org))
            for org in ("OrgTimeout", "OrgBroken", "OrgEmpty")
        }
        assert statuses == {
            "OrgTimeout": "error: timeout",
            "OrgBroken": "error: http_500",
            "OrgEmpty": "render_ok_zero",
        }
        assert len(set(statuses.values())) == 3

    def test_scrape_status_outranks_fetch_error(self):
        # js_required / credit_exhausted are more specific than a generic error.
        assert _resolve_fetch_status("ok", False, "js_required", "error: timeout") == "js_required"


# ---------------------------------------------------------------------------
# FetchError classification in the shared HTTP skeleton
# ---------------------------------------------------------------------------


class TestFetchErrorClassification:
    def test_timeout_reason(self, monkeypatch):
        monkeypatch.setattr(
            fetchers, "requests", _FakeHTTP(raise_exc=real_requests.exceptions.Timeout("boom"))
        )
        try:
            fetchers.http.get("https://example.test", timeout=1)
        except FetchError as e:
            assert e.reason == "timeout"
            assert e.status == "error: timeout"
        else:
            raise AssertionError("expected FetchError")

    def test_http_status_reason(self, monkeypatch):
        monkeypatch.setattr(fetchers, "requests", _FakeHTTP(response=_Resp(status=503)))
        try:
            fetchers.http.get("https://example.test", timeout=1)
        except FetchError as e:
            assert e.reason == "http_503"
        else:
            raise AssertionError("expected FetchError")

    def test_network_reason(self, monkeypatch):
        monkeypatch.setattr(fetchers, "requests", _FakeHTTP(raise_exc=OSError("conn refused")))
        try:
            fetchers.http.get("https://example.test", timeout=1)
        except FetchError as e:
            assert e.reason == "network"
        else:
            raise AssertionError("expected FetchError")


# ---------------------------------------------------------------------------
# Registry: every pipeline strategy is dispatchable; one file adds a source
# ---------------------------------------------------------------------------

_COMPANY_STRATEGIES = {
    "greenhouse",
    "firecrawl_scrape",
    "workday_api",
    "lever",
    "ashby",
    "workable",
    "unops_widget",
    "recruitee",
    "teamtailor_rss",
    "bamboohr",
    "amazon_jobs",
    "apple_jobs",
    "successfactors",
    "adp_json",
    "smartrecruiters",
}

_BOARD_STRATEGIES = {
    "algolia_api",
    "firecrawl_board",
    "reliefweb_api",
    "impactpool_html",
    "datadotorg_wp",
    "arbeitnow_api",
    "remotive_api",
    "wwr_rss",
    "hn_whoishiring",
    "idealist_algolia",
    "fastforward_board",
    "linkedin_guest",
}


class TestRegistry:
    def test_all_company_strategies_registered(self):
        assert _COMPANY_STRATEGIES <= set(COMPANY_FETCHERS)

    def test_all_board_strategies_registered(self):
        assert _BOARD_STRATEGIES <= set(BOARD_FETCHERS)

    def test_new_board_registers_and_dispatches(self, monkeypatch):
        """The one-file-board contract: decorate a function, dispatch by strategy."""
        _fresh_errors(monkeypatch)
        try:

            @board_fetcher("test_dummy_board")
            def fetch_dummy_board(board_cfg):
                return [{"title": "Test Role", "url": board_cfg["url"]}]

            assert "test_dummy_board" in BOARD_FETCHERS
            out = BOARD_FETCHERS["test_dummy_board"]({"name": "Dummy", "url": "https://d.test"})
            assert out == [{"title": "Test Role", "url": "https://d.test"}]
        finally:
            BOARD_FETCHERS.pop("test_dummy_board", None)

    def test_board_boundary_records_reason_and_returns_empty(self, monkeypatch):
        _fresh_errors(monkeypatch)
        try:

            @board_fetcher("test_broken_board")
            def fetch_broken_board(board_cfg):
                raise FetchError("http_500", "server exploded")

            out = BOARD_FETCHERS["test_broken_board"]({"name": "Broken Board", "url": "x"})
            assert out == []
            assert fetchers.get_fetch_errors()["Broken Board"] == "error: http_500"
        finally:
            BOARD_FETCHERS.pop("test_broken_board", None)

    def test_company_boundary_records_generic_exception(self, monkeypatch):
        _fresh_errors(monkeypatch)

        @company_fetcher
        def fetch_exploding(org_name):
            raise ValueError("unexpected payload")

        assert fetch_exploding("Fragile Org") == []
        assert fetchers.get_fetch_errors()["Fragile Org"].startswith("error: ")
