"""Unit tests for fetch_successfactors (SAP SuccessFactors CSB tile feed, U4).

DRIFT NOTE: the plan assumed a "tile-search JSON" endpoint. The live
Career Site Builder ``/tile-search-results/`` endpoint actually returns an
HTML fragment of ``<li class="job-tile ...">`` tiles (verified against
RobertBoschStiftung, whose 3 test postings are captured in
``fixtures/successfactors_tiles.html``). The fetcher parses HTML tiles; the
saved fixture is that real HTML fragment (not JSON).
"""

import os
import re


import fetchers
from fetchers import fetch_successfactors, _parse_successfactors_tiles, _sf_base_url

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


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


class FakeResponse:
    def __init__(self, *, text="", status=200, cookies=None):
        self.text = text
        self.status_code = status
        self.cookies = cookies or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    """Routes SuccessFactors GETs; startrow → page body via ``pages`` map."""

    def __init__(self, pages: dict, *, bootstrap=None, raise_on=None):
        # pages: {startrow_int: html}; missing startrow → empty shell.
        self.pages = pages
        self.bootstrap = (
            bootstrap
            if bootstrap is not None
            else FakeResponse(text="<html>search</html>", cookies={"JSESSIONID": "abc"})
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
        return FakeResponse(text=self.pages.get(startrow, EMPTY_HTML))


# ---------------------------------------------------------------------------
# base-url resolution
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# tile parsing (pure)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# fetch_successfactors (end-to-end with mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchSuccessfactors:
    def test_happy_path_parses_fixture(self, monkeypatch):
        fake = FakeRequests({0: TILES_HTML})
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
        fake = FakeRequests(pages)
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
        fake = FakeRequests({0: TILES_HTML, 25: TILES_HTML, 50: TILES_HTML})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_successfactors("RBS", {"ats_slug": "RobertBoschStiftung"})
        assert len(jobs) == 3

    def test_empty_site_returns_empty_list(self, monkeypatch):
        fake = FakeRequests({})  # every startrow → empty shell
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_successfactors("ILO", {"url": "https://jobs.ilo.org/"}) == []

    def test_404_on_tiles_returns_empty_without_crash(self, monkeypatch):
        fake = FakeRequests({}, raise_on=lambda url: "tile-search-results" in url)
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_successfactors("ILO", {"url": "https://jobs.ilo.org/"}) == []

    def test_no_config_returns_empty(self, monkeypatch):
        # No network should be touched when there is nothing to fetch.
        fake = FakeRequests({})
        monkeypatch.setattr(fetchers, "requests", fake)
        assert fetch_successfactors("Nowhere", {}) == []
        assert fake.calls == []
