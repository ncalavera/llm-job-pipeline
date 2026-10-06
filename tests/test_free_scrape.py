"""The free careers-page scraper: plain download, then the local
headless browser, with job cards read from the HTML. Firecrawl is opt-in."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetchers
from fetchers import firecrawl
from fetchers.http import FetchError
from fetchers.parsing import _parse_json_jobs, extract_job_cards

BASE = "https://example.org/careers"

LINK_CARDS = """
<html><body>
<nav><a href="/team/programme-manager">Programme Manager</a></nav>
<main>
  <a href="/careers/director-infrastructure">
    <div>Director, Infrastructure</div><div>Washington, D.C.</div><div>Posted: 07.23.2026</div>
  </a>
  <a href="/careers/admin-officer"><h3>Example Org is hiring for an Admin and Finance Officer</h3>
    <p>Jobs in the sector. A longer text about the role.</p></a>
  <a href="/our-services/development-program">Nonprofit Development Program (Africa)</a>
  <a href="/careers/pre-doctoral-program">Pre-Doctoral Program</a>
  <a href="/invited-researchers">Invited Researchers</a>
  <a href="https://jobs.example.com/1">Postdoctoral Research Associate (Fixed Term) | Example University</a>
</main>
<footer><a href="/privacy/officer">Data Protection Officer</a></footer>
</body></html>
"""

HEADING_CARDS = """
<html><body><ul>
  <li><div><h3>Head of Middle Office</h3></div>
      <p>The Head of Middle Office is a senior leadership role at the centre of the firm.</p>
      <div><a href="https://ats.example.com/apply/abc" class="btn">Apply now</a></div></li>
  <li><h3>Investment Officer</h3><p>Contract: permanent. A pivotal role in the team.</p>
      <a href="https://ats.example.com/apply/def">Apply now</a></li>
</ul></body></html>
"""


def _titles(html):
    return {j["title"]: j for j in _parse_json_jobs(extract_job_cards(html, BASE), "Example", BASE)}


def test_link_cards_keep_jobs_and_drop_site_chrome_and_non_jobs():
    jobs = _titles(LINK_CARDS)
    assert set(jobs) == {
        "Director, Infrastructure",  # first line of the card, not the whole link text
        "Admin and Finance Officer",  # "<Org> is hiring for an" stripped
        "Pre-Doctoral Program",  # a weak word, but under /careers/
        "Postdoctoral Research Associate (Fixed Term)",  # "| Organisation" stripped
    }
    assert jobs["Director, Infrastructure"]["url"] == (
        "https://example.org/careers/director-infrastructure"
    )
    assert "Washington" in jobs["Director, Infrastructure"]["snippet"]


def test_heading_cards_take_the_link_of_their_own_block():
    jobs = _titles(HEADING_CARDS)
    assert jobs["Head of Middle Office"]["url"] == "https://ats.example.com/apply/abc"
    assert jobs["Investment Officer"]["url"] == "https://ats.example.com/apply/def"
    assert "senior leadership role" in jobs["Head of Middle Office"]["snippet"]


def test_a_header_that_wraps_the_whole_page_is_not_stripped():
    html = f"<html><body><header>{HEADING_CARDS}</header></body></html>"
    assert set(_titles(html)) == {"Head of Middle Office", "Investment Officer"}


@pytest.fixture
def scraper(monkeypatch, tmp_path):
    """The free scraper with the network, the browser and enrichment stubbed."""
    calls = SimpleNamespace(get=[], render=[], firecrawl=0)
    pages = {}

    def _get(url, **kwargs):
        calls.get.append(kwargs.get("headers"))
        page = pages["plain"].pop(0) if isinstance(pages["plain"], list) else pages["plain"]
        if isinstance(page, Exception):
            raise page
        return SimpleNamespace(text=page)

    def _render(url, **kwargs):
        calls.render.append(url)
        return pages.get("browser", "")

    def _client():
        calls.firecrawl += 1

    monkeypatch.delenv("VACANCY_FETCH_ENGINE", raising=False)
    monkeypatch.setattr(firecrawl.http, "get", _get)
    monkeypatch.setattr(fetchers, "render_html", _render)
    monkeypatch.setattr(fetchers, "get_firecrawl_client", _client)
    monkeypatch.setattr(firecrawl, "FIRECRAWL_CACHE", tmp_path)
    monkeypatch.setattr(firecrawl, "_enrich_blind_jobs", lambda jobs, org: jobs)
    monkeypatch.setattr(fetchers, "_last_scrape_status", {})
    monkeypatch.setattr(fetchers, "_last_fetch_errors", {})
    return SimpleNamespace(
        pages=pages, calls=calls, run=lambda: fetchers.fetch_firecrawl_scrape("Example", BASE)
    )


def test_default_engine_is_free_and_never_touches_firecrawl(scraper):
    scraper.pages["plain"] = HEADING_CARDS
    jobs = scraper.run()
    assert [j["title"] for j in jobs] == ["Head of Middle Office", "Investment Officer"]
    assert scraper.calls.firecrawl == 0
    assert scraper.calls.render == [], "jobs in the plain HTML: no browser needed"


def test_javascript_page_is_rendered_in_the_browser(scraper):
    scraper.pages["plain"] = "<html><body><div id='root'></div></body></html>"
    scraper.pages["browser"] = HEADING_CARDS
    assert len(scraper.run()) == 2
    assert scraper.calls.render == [BASE]
    assert "Example" not in fetchers.get_scrape_statuses()


def test_rendered_page_with_no_job_is_an_honest_empty_listing(scraper):
    scraper.pages["plain"] = "<html><body><div id='root'></div></body></html>"
    scraper.pages["browser"] = "<html><body><h2>No vacancies open right now.</h2></body></html>"
    assert scraper.run() == []
    assert "Example" not in fetchers.get_scrape_statuses()
    assert "Example" not in fetchers.get_fetch_errors()


def test_no_browser_marks_the_source_js_required(scraper):
    scraper.pages["plain"] = "<html><body><div id='root'></div></body></html>"
    assert scraper.run() == []
    assert fetchers.get_scrape_statuses()["Example"] == "js_required"


def test_refused_browser_user_agent_is_retried_with_an_honest_one(scraper):
    scraper.pages["plain"] = [FetchError("http_403", "403 Forbidden"), HEADING_CARDS]
    assert len(scraper.run()) == 2
    first, second = (h["User-Agent"] for h in scraper.calls.get)
    assert "Mozilla" in first and "llm-job-pipeline" in second


def test_a_refused_download_stays_an_error_even_if_the_browser_drew_a_page(scraper):
    scraper.pages["plain"] = FetchError("http_500", "boom")
    scraper.pages["browser"] = "<html><body><h1>Checking your browser</h1></body></html>"
    assert scraper.run() == []
    assert fetchers.get_fetch_errors()["Example"] == "error: http_500"


def test_job_page_reader_renders_only_a_javascript_shell(monkeypatch):
    import enrich_blind_vacancies as ebv

    long_text = "Responsibilities and requirements of the role. " * 60
    rendered = []
    monkeypatch.setattr(ebv, "_render_job_page", lambda url: rendered.append(url) or long_text)

    monkeypatch.setattr(ebv, "_fetch_plain_page_text", lambda url: (long_text, {"status": 200}))
    assert ebv._scrape_job_page(None, "https://example.org/job/1") == long_text
    assert rendered == []

    monkeypatch.setattr(ebv, "_fetch_plain_page_text", lambda url: ("Loading…", {"status": 200}))
    assert ebv._scrape_job_page(None, "https://example.org/job/2") == long_text
    assert rendered == ["https://example.org/job/2"]

    monkeypatch.setattr(ebv, "_fetch_plain_page_text", lambda url: ("", {"status": 404}))
    assert ebv._scrape_job_page(None, "https://example.org/job/3") == ""
    assert len(rendered) == 1, "a 404 page is gone, not a JavaScript shell"
