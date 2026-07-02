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

import pytest

import fetchers
from fetchers import (
    fetch_successfactors,
    _parse_successfactors_tiles,
    _sf_base_url,
    _sf_sitemap_job_urls,
    _sf_job_detail,
)
from fetchers.http import FetchError

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
            return FakeResponse(text=self.sitemap)
        return FakeResponse(text=self.details.get(url, ""))


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
        fake = FakeSitemapRequests(sitemap=SITEMAP_XML, details={JOB_URL_1: "<html>no job here</html>"})
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
        jobs = fetch_successfactors(
            "ILO", {"url": "https://jobs.ilo.org", "sf_backend": "sitemap"}
        )
        assert len(jobs) == 2
        assert {j["external_id"] for j in jobs} == {"1399744233", "1409512033"}
        assert all(j["title"] and j["full_description"] for j in jobs)

    def test_default_config_still_uses_tile_backend(self, monkeypatch):
        # No sf_backend set → unaffected by the new code path.
        fake = FakeRequests({0: TILES_HTML})
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
            details={JOB_URL_1: JOB_DETAIL_HTML},  # JOB_URL_2 has no entry -> empty text -> no title
        )
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_successfactors(
            "ILO", {"url": "https://jobs.ilo.org", "sf_backend": "sitemap"}
        )
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
        jobs = fetch_successfactors(
            "ILO", {"url": "https://jobs.ilo.org", "sf_backend": "sitemap"}
        )
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
        jobs = fetch_successfactors(
            "ILO", {"url": "https://jobs.ilo.org", "sf_backend": "sitemap"}
        )
        assert len(jobs) == 3  # capped, not all 5
        detail_calls = [u for u in fake.calls if "/job/" in u]
        assert len(detail_calls) == 3  # the loop stopped at the cap

    def test_cap_constant_is_a_sane_positive_int(self):
        from fetchers.ats import successfactors as sf

        assert isinstance(sf._SITEMAP_MAX_DETAIL_FETCHES, int)
        assert sf._SITEMAP_MAX_DETAIL_FETCHES > 0
