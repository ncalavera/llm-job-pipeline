"""Unit tests for the four opt-in board fetchers (arbeitnow, remotive,
weworkremotely, hn_whoishiring) with mocked HTTP responses, plus the JOB_BOARDS
opt-in selection and quality-gate junk rejection on the merge path.
"""

import importlib
import sys

import pytest

import fetchers
from fetchers import (
    fetch_arbeitnow_board,
    fetch_remotive_board,
    fetch_wwr_board,
    fetch_hn_whoishiring_board,
    _parse_hn_comment,
    _html_to_multiline,
)


# ---------------------------------------------------------------------------
# Fake requests plumbing
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, *, json_data=None, content=b"", status=200):
        self._json = json_data
        self.content = content
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    """Routes requests.get(url, params=...) to canned responses; records calls."""

    def __init__(self, router):
        self.router = router
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        return self.router(url, params or {})


# ---------------------------------------------------------------------------
# Arbeitnow
# ---------------------------------------------------------------------------

ARBEITNOW_PAGE = {
    "data": [
        {
            "slug": "head-of-product-berlin-1",
            "company_name": "Acme GmbH",
            "title": "Head of Product",
            "description": "<p>Drive the product roadmap.</p><p>Own discovery and delivery.</p>",
            "remote": True,
            "url": "https://www.arbeitnow.com/jobs/companies/acme-gmbh/head-of-product-1",
            "tags": ["Product"],
            "job_types": ["full time"],
            "location": "Berlin",
            "visa_sponsorship": True,
            "created_at": 1780000000,
        },
        {
            "slug": "talent-pool-2",
            "company_name": "Other GmbH",
            "title": "Talent Pool — General Application",  # UNIVERSAL_JUNK: no concrete role
            "description": "<p>Register your interest for future roles.</p>",
            "remote": False,
            "url": "https://example.test/2",
            "tags": [],
            "job_types": [],
            "location": "Munich",
            "visa_sponsorship": True,
            "created_at": 1780000001,
        },
        {
            "slug": "ops-manager-3",
            "company_name": "NoVisa AG",
            "title": "Operations Manager",
            "description": "<p>Run the office operations end to end.</p>",
            "remote": False,
            "url": "https://example.test/3",
            "tags": [],
            "job_types": [],
            "location": "Hamburg",
            "visa_sponsorship": False,
            "created_at": 1780000002,
        },
    ],
    "links": {"next": None},
    "meta": {},
}

ARBEITNOW_CFG = {
    "strategy": "arbeitnow_api",
    "name": "Arbeitnow",
    "url": "https://www.arbeitnow.com",
    "pages": 3,
    "board_blacklist": [],
}


def _arbeitnow_router(url, params):
    assert "arbeitnow.com/api/job-board-api" in url
    return FakeResponse(json_data=ARBEITNOW_PAGE)


def test_arbeitnow_parses_and_blacklists(monkeypatch):
    monkeypatch.delenv("ARBEITNOW_VISA_ONLY", raising=False)
    monkeypatch.setattr(fetchers, "requests", FakeRequests(_arbeitnow_router))

    jobs = fetch_arbeitnow_board(ARBEITNOW_CFG)
    titles = {j["title"] for j in jobs}
    assert "Head of Product" in titles
    assert "Operations Manager" in titles
    # Only the true non-job ("talent pool" → no concrete role) is dropped.
    assert "Talent Pool — General Application" not in titles

    head = next(j for j in jobs if j["title"] == "Head of Product")
    assert head["org_override"] == "Acme GmbH"
    assert head["url"].startswith("https://www.arbeitnow.com/jobs/")
    assert head["location"] == "Remote, Berlin"  # remote flag prefixes location
    assert "Visa sponsorship: yes" in head["full_description"]
    assert head["external_id"] == "head-of-product-berlin-1"
    # HTML stripped from description
    assert "<p>" not in head["full_description"]

    ops = next(j for j in jobs if j["title"] == "Operations Manager")
    assert ops["location"] == "Hamburg"  # not remote, no prefix


def test_arbeitnow_visa_only_env(monkeypatch):
    monkeypatch.setenv("ARBEITNOW_VISA_ONLY", "1")
    monkeypatch.setattr(fetchers, "requests", FakeRequests(_arbeitnow_router))

    jobs = fetch_arbeitnow_board(ARBEITNOW_CFG)
    # Only the visa_sponsorship=true non-blacklisted job survives.
    assert [j["title"] for j in jobs] == ["Head of Product"]


def test_arbeitnow_stops_when_no_next_page(monkeypatch):
    fake = FakeRequests(_arbeitnow_router)
    monkeypatch.delenv("ARBEITNOW_VISA_ONLY", raising=False)
    monkeypatch.setattr(fetchers, "requests", fake)
    fetch_arbeitnow_board(ARBEITNOW_CFG)
    assert len(fake.calls) == 1  # links.next is null -> single request


# ---------------------------------------------------------------------------
# Remotive
# ---------------------------------------------------------------------------


def _remotive_payload(jobs):
    return {"job-count": len(jobs), "jobs": jobs}


REMOTIVE_JOB = {
    "id": 1956001,
    "url": "https://remotive.com/remote-jobs/product/head-of-product-1956001",
    "title": "Head of Product",
    "company_name": "Remote Co",
    "category": "Product Management",
    "tags": ["product"],
    "job_type": "full_time",
    "publication_date": "2026-06-09T21:15:39",
    "candidate_required_location": "Europe",
    "salary": "$120k-$150k",
    "description": "<p>Lead our product org.</p>",
}


def test_remotive_single_request_and_parsing(monkeypatch):
    monkeypatch.delenv("REMOTIVE_CATEGORIES", raising=False)
    fake = FakeRequests(lambda url, p: FakeResponse(json_data=_remotive_payload([REMOTIVE_JOB])))
    monkeypatch.setattr(fetchers, "requests", fake)

    jobs = fetch_remotive_board(
        {
            "strategy": "remotive_api",
            "name": "Remotive",
            "url": "https://remotive.com",
            "board_blacklist": [],
        }
    )
    assert len(fake.calls) == 1  # respect their limits: one request per run
    assert fake.calls[0]["params"] == {}

    assert len(jobs) == 1
    j = jobs[0]
    assert j["org_override"] == "Remote Co"
    assert j["title"] == "Head of Product"
    assert j["url"].endswith("1956001")
    assert j["location"] == "Remote, Europe"
    assert j["compensation"] == "$120k-$150k"
    assert "Candidate location: Europe" in j["full_description"]


def test_remotive_one_request_per_category_and_dedup(monkeypatch):
    monkeypatch.setenv("REMOTIVE_CATEGORIES", "product, marketing")
    # Same job returned in both categories -> must dedup by id.
    fake = FakeRequests(lambda url, p: FakeResponse(json_data=_remotive_payload([REMOTIVE_JOB])))
    monkeypatch.setattr(fetchers, "requests", fake)

    jobs = fetch_remotive_board(
        {
            "strategy": "remotive_api",
            "name": "Remotive",
            "url": "https://remotive.com",
            "board_blacklist": [],
        }
    )
    assert len(fake.calls) == 2
    assert {c["params"].get("category") for c in fake.calls} == {"product", "marketing"}
    assert len(jobs) == 1  # deduped across the two requests


def test_remotive_blacklist_applied(monkeypatch):
    monkeypatch.delenv("REMOTIVE_CATEGORIES", raising=False)
    # Universal junk = a non-job pipeline entry, NOT a real role of any kind.
    junk = dict(REMOTIVE_JOB, id=2, title="Expression of Interest")
    fake = FakeRequests(
        lambda url, p: FakeResponse(json_data=_remotive_payload([REMOTIVE_JOB, junk]))
    )
    monkeypatch.setattr(fetchers, "requests", fake)

    jobs = fetch_remotive_board(
        {
            "strategy": "remotive_api",
            "name": "Remotive",
            "url": "https://remotive.com",
            "board_blacklist": [],
        }
    )
    assert [j["title"] for j in jobs] == ["Head of Product"]


def test_remotive_real_roles_survive_neutral_gate(monkeypatch):
    """Neutral default keeps every real role — discipline/format/career-stage
    are not dropped. A fellowship, an internship, a volunteer coordinator all
    survive (a student / career-changer wants to see them)."""
    monkeypatch.delenv("REMOTIVE_CATEGORIES", raising=False)
    survivors = [
        dict(REMOTIVE_JOB, id=10, title="Research Fellowship"),
        dict(REMOTIVE_JOB, id=11, title="Summer Internship"),
        dict(REMOTIVE_JOB, id=12, title="Volunteer Coordinator"),
        dict(REMOTIVE_JOB, id=13, title="Data Science Bootcamp Instructor"),
        dict(REMOTIVE_JOB, id=14, title="Software Engineer"),
    ]
    fake = FakeRequests(lambda url, p: FakeResponse(json_data=_remotive_payload(survivors)))
    monkeypatch.setattr(fetchers, "requests", fake)
    jobs = fetch_remotive_board(
        {
            "strategy": "remotive_api",
            "name": "Remotive",
            "url": "https://remotive.com",
            "board_blacklist": [],
        }
    )
    assert {j["title"] for j in jobs} == {
        "Research Fellowship",
        "Summer Internship",
        "Volunteer Coordinator",
        "Data Science Bootcamp Instructor",
        "Software Engineer",
    }


# ---------------------------------------------------------------------------
# We Work Remotely (RSS)
# ---------------------------------------------------------------------------

WWR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>We Work Remotely: Product Jobs</title>
    <item>
      <title>Nearcut: Product Manager</title>
      <region>Anywhere in the World</region>
      <category>Product</category>
      <type>Full-Time</type>
      <description>&lt;p&gt;Shape product direction in a client-focused team.&lt;/p&gt;</description>
      <pubDate>Fri, 12 Jun 2026 09:12:55 +0000</pubDate>
      <expires_at>Sun, 12 Jul 2026 09:12:55 +0000</expires_at>
      <guid>https://weworkremotely.com/remote-jobs/nearcut-product-manager</guid>
      <link>https://weworkremotely.com/remote-jobs/nearcut-product-manager</link>
    </item>
    <item>
      <title>HelpCorp: Talent Pool</title>
      <region>Europe Only</region>
      <category>Other</category>
      <description>&lt;p&gt;Register your interest for future roles.&lt;/p&gt;</description>
      <link>https://weworkremotely.com/remote-jobs/helpcorp-talent-pool</link>
      <guid>https://weworkremotely.com/remote-jobs/helpcorp-talent-pool</guid>
    </item>
  </channel>
</rss>
"""


def test_wwr_parses_company_role_and_blacklists(monkeypatch):
    monkeypatch.delenv("WWR_CATEGORIES", raising=False)
    fake = FakeRequests(lambda url, p: FakeResponse(content=WWR_RSS.encode()))
    monkeypatch.setattr(fetchers, "requests", fake)

    jobs = fetch_wwr_board(
        {
            "strategy": "wwr_rss",
            "name": "We Work Remotely",
            "url": "https://weworkremotely.com",
            "default_categories": ["product"],
            "board_blacklist": [],
        }
    )
    # "Talent Pool" is a non-job pipeline entry (universal junk); only the PM
    # survives. A real role of any discipline/format would NOT be dropped here.
    assert len(jobs) == 1
    j = jobs[0]
    assert j["org_override"] == "Nearcut"  # "Company: Role" split
    assert j["title"] == "Product Manager"
    assert j["url"] == "https://weworkremotely.com/remote-jobs/nearcut-product-manager"
    assert j["location"] == "Remote, Anywhere in the World"
    assert j["deadline"] == "Sun, 12 Jul 2026 09:12:55 +0000"
    assert "Shape product direction" in j["full_description"]


def test_wwr_categories_env_controls_feeds(monkeypatch):
    monkeypatch.setenv("WWR_CATEGORIES", "marketing")
    fake = FakeRequests(lambda url, p: FakeResponse(content=WWR_RSS.encode()))
    monkeypatch.setattr(fetchers, "requests", fake)

    fetch_wwr_board(
        {
            "strategy": "wwr_rss",
            "name": "We Work Remotely",
            "url": "https://weworkremotely.com",
            "default_categories": ["product", "management-and-finance"],
            "board_blacklist": [],
        }
    )
    assert len(fake.calls) == 1
    assert "remote-marketing-jobs.rss" in fake.calls[0]["url"]


# ---------------------------------------------------------------------------
# HN Who is hiring
# ---------------------------------------------------------------------------

HN_SEARCH = {
    "hits": [
        {"objectID": "48357725", "title": "Ask HN: Who is hiring? (June 2026)"},
        {"objectID": "47975571", "title": "Ask HN: Who is hiring? (May 2026)"},
    ]
}

HN_ITEM = {
    "id": 48357725,
    "children": [
        {
            "id": 1001,
            "text": "Acme Robotics | Head of Operations | Remote (EU) | Full-time"
            "<p>We build warehouse robots. You will run operations across"
            " three countries and own the supply chain end to end.</p>"
            "<p>Location: Remote within EU timezones</p>",
            "children": [
                {"id": 9999, "text": "Reply: is this still open?"},  # nested -> ignored
            ],
        },
        {
            "id": 1002,
            "text": "BetaCorp — Product Lead — Berlin onsite"
            "<p>Join our product team in Berlin.</p>",
            "children": [],
        },
        {
            "id": 1003,
            "text": "HelpCo | Talent Pool | Remote<p>Register interest for future roles.</p>",
            "children": [],
        },
        {"id": 1004, "text": None, "children": []},  # deleted comment
        {
            "id": 1005,
            "text": "Hey HN! Just wanted to say this thread is great.",
            "children": [],
        },
    ],
}


def _hn_router(url, params):
    if "search_by_date" in url:
        return FakeResponse(json_data=HN_SEARCH)
    if "/items/48357725" in url:
        return FakeResponse(json_data=HN_ITEM)
    raise AssertionError(f"unexpected URL {url}")


def test_hn_parses_top_level_comments_only(monkeypatch):
    fake = FakeRequests(_hn_router)
    monkeypatch.setattr(fetchers, "requests", fake)

    jobs = fetch_hn_whoishiring_board(
        {
            "strategy": "hn_whoishiring",
            "name": "HN Who is hiring",
            "url": "https://news.ycombinator.com/submitted?id=whoishiring",
            "board_blacklist": [],
        }
    )

    orgs = {j["org_override"] for j in jobs}
    assert "Acme Robotics" in orgs  # pipe-separated
    assert "BetaCorp" in orgs  # em-dash-separated
    assert "HelpCo" not in orgs  # universal-junk title (talent pool)
    # Nested reply (id 9999) must NOT appear — top-level only.
    assert all(j["external_id"] != "9999" for j in jobs)

    acme = next(j for j in jobs if j["org_override"] == "Acme Robotics")
    assert acme["title"] == "Head of Operations"
    assert acme["url"] == "https://news.ycombinator.com/item?id=1001"
    assert "Remote" in acme["location"]
    assert "warehouse robots" in acme["full_description"]


def test_hn_comment_without_separator_keeps_board_pseudo_org():
    parsed = _parse_hn_comment(
        {"id": 7, "text": "We are a stealth startup hiring a generalist operator."}
    )
    assert parsed is not None
    assert parsed["org_override"] == ""  # filled with [via ...] by the fetcher
    assert parsed["title"].startswith("We are a stealth startup")


def test_hn_deleted_comment_skipped():
    assert _parse_hn_comment({"id": 8, "text": None}) is None
    assert _parse_hn_comment({"id": 9, "text": "   "}) is None


def test_html_to_multiline_keeps_structure():
    out = _html_to_multiline("<p>One</p><p>Two</p><ul><li>A</li><li>B</li></ul>")
    assert "One" in out and "Two" in out
    assert "\n" in out
    assert "- A" in out and "- B" in out


# ---------------------------------------------------------------------------
# JOB_BOARDS env selects the new boards; default stays empty
# ---------------------------------------------------------------------------


def test_new_boards_registered_and_opt_in(monkeypatch):
    import config as cfg

    new_ids = {"arbeitnow", "remotive", "weworkremotely", "hn_whoishiring"}
    impact_ids = {"impactpool", "idealist", "fast_forward", "linkedin"}
    assert new_ids <= set(cfg._ALL_JOB_BOARDS)
    assert impact_ids <= set(cfg._ALL_JOB_BOARDS)

    # Default: still empty.
    monkeypatch.delenv("JOB_BOARDS", raising=False)
    assert cfg._select_enabled_boards() == {}

    # Selecting the new boards works.
    monkeypatch.setenv("JOB_BOARDS", "arbeitnow,remotive,weworkremotely,hn_whoishiring")
    assert set(cfg._select_enabled_boards()) == new_ids

    # The impact boards opt in the same way.
    monkeypatch.setenv("JOB_BOARDS", "impactpool,idealist,fast_forward,linkedin")
    assert set(cfg._select_enabled_boards()) == impact_ids

    # JOB_BOARDS=all includes every defined board.
    monkeypatch.setenv("JOB_BOARDS", "all")
    enabled = cfg._select_enabled_boards()
    assert new_ids | impact_ids | {"80k_hours", "reliefweb"} <= set(enabled)

    # The monthly HN thread carries the long TTL.
    assert cfg._ALL_JOB_BOARDS["hn_whoishiring"]["ttl_days"] == 30
    # LinkedIn ships a default query set (merged LinkedIn + LinkedIn Non-profits).
    assert len(cfg._ALL_JOB_BOARDS["linkedin"]["queries"]) >= 1


# ---------------------------------------------------------------------------
# Idealist (Algolia POST) / Fast Forward (Getro POST) / LinkedIn (guest GET)
# ---------------------------------------------------------------------------


class _Resp:
    """Minimal response exposing .json(), .text, .content, .status_code."""

    def __init__(self, *, json_data=None, text="", status=200):
        self._json = json_data
        self.text = text
        self.content = text.encode()
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHTTP:
    """Routes requests.post / requests.get to a single router callable."""

    def __init__(self, router):
        self.router = router
        self.calls = []

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.calls.append({"verb": "POST", "url": url, "json": json})
        return self.router("POST", url, json=json, params=None)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"verb": "GET", "url": url, "params": params or {}})
        return self.router("GET", url, json=None, params=params or {})


def test_fetch_idealist_board(monkeypatch):
    hit = {
        "name": "Head of Operations",
        "orgName": "Global Impact Fund",
        "objectID": "abc123",
        "url": {"en": "/en/nonprofit-job/abc123-head-of-operations"},
        "orgUrl": {"en": "/en/nonprofit/global-impact-fund"},
        "locationType": "REMOTE",
        "remoteZone": "WORLD",
        "functions": ["OPERATIONS"],
        "keywords": ["operations", "strategy"],
        "description": "Lead operations for a global grantmaker.",
        "salaryMinimum": 90000,
        "salaryMaximum": 110000,
        "salaryCurrency": "USD",
        "salaryPeriod": "YEAR",
        "professionalLevel": "DIRECTOR",
    }

    def router(verb, url, json=None, params=None):
        assert verb == "POST" and "algolia.net" in url
        return _Resp(json_data={"hits": [hit], "nbPages": 1})

    monkeypatch.setattr(fetchers, "requests", _FakeHTTP(router))
    out = fetchers.fetch_idealist_board(
        {"name": "Idealist", "url": "https://www.idealist.org/en/jobs"}
    )
    assert len(out) == 1
    job = out[0]
    assert job["title"] == "Head of Operations"
    assert job["org_override"] == "Global Impact Fund"
    assert job["external_id"] == "abc123"
    assert job["url"].endswith("/en/nonprofit-job/abc123-head-of-operations")
    assert job["location"] == "Remote (worldwide)"
    assert "USD" in job["compensation"]


def test_fetch_fastforward_board(monkeypatch):
    rec = {
        "id": 555,
        "slug": "head-of-programmes",
        "title": "Head of Programmes",
        "organization": {"name": "Noora Health", "slug": "noora-health"},
        "locations": ["London, United Kingdom"],
        "work_mode": "hybrid",
        "has_description": True,
        "compensation_public": True,
        "compensation_amount_min_cents": 7000000,
        "compensation_amount_max_cents": 9000000,
        "compensation_currency": "GBP",
        "compensation_period": "year",
    }

    def router(verb, url, json=None, params=None):
        assert verb == "POST" and "getro.com" in url
        jobs = [rec] if (json or {}).get("page", 0) == 0 else []
        return _Resp(json_data={"results": {"jobs": jobs, "count": 1}})

    monkeypatch.setattr(fetchers, "requests", _FakeHTTP(router))
    out = fetchers.fetch_fastforward_board(
        {"name": "Fast Forward", "url": "https://jobs.ffwd.org/jobs", "fetch_descriptions": False}
    )
    assert len(out) == 1
    job = out[0]
    assert job["title"] == "Head of Programmes"
    assert job["org_override"] == "Noora Health"
    assert job["external_id"] == "555"
    assert "GBP" in job["compensation"]


LINKEDIN_CARD_HTML = """
<ul>
<li>
  <div class="base-card job-search-card" data-entity-urn="urn:li:jobPosting:4403552549">
    <a class="base-card__full-link" href="https://uk.linkedin.com/jobs/view/chief-of-staff-at-governr-4403552549?refId=x">x</a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">Chief of Staff</h3>
      <h4 class="base-search-card__subtitle"><a class="hidden-nested-link" href="https://uk.linkedin.com/company/governr?trk=y">governr</a></h4>
      <span class="job-search-card__location">London, United Kingdom</span>
    </div>
  </div>
</li>
</ul>
"""


def test_fetch_linkedin_board(monkeypatch):
    def router(verb, url, json=None, params=None):
        assert verb == "GET"
        if "seeMoreJobPostings" in url and (params or {}).get("start", 0) == 0:
            return _Resp(text=LINKEDIN_CARD_HTML, status=200)
        return _Resp(text="", status=200)

    monkeypatch.setattr(fetchers, "requests", _FakeHTTP(router))
    monkeypatch.setattr(fetchers.time, "sleep", lambda *a, **k: None)

    out = fetchers.fetch_linkedin_board(
        {
            "name": "LinkedIn",
            "url": "https://www.linkedin.com/jobs",
            "queries": [{"keywords": "Chief of Staff", "location": "London"}],
            "pages": 1,
            "request_delay": 0,
            "fetch_detail": False,
        }
    )
    assert len(out) == 1
    job = out[0]
    assert job["title"] == "Chief of Staff"
    assert job["org_override"] == "governr"
    assert job["external_id"] == "4403552549"
    assert job["url"] == "https://uk.linkedin.com/jobs/view/chief-of-staff-at-governr-4403552549"
    assert job["location"] == "London, United Kingdom"


# ---------------------------------------------------------------------------
# Quality gate on the merge path (board jobs included)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db

    yield db
    db.close_conn()


def test_board_merge_rejects_quality_gate_junk(sqlite_dal):
    """Fetched board descriptions go through the same quality gate as ATS
    sources. Two junk flavors, two outcomes locked by the existing design:

    - error-page boilerplate -> the gate BLANKS the description (the vacancy
      may survive as a blind URL-only row, but the junk text never lands);
    - non-vacancy content (donation widget) -> the row is rejected entirely.
    """
    db = sqlite_dal
    board_cfg = {"name": "Arbeitnow", "url": "https://www.arbeitnow.com", "tier": "B"}

    good = {
        "title": "Head of Product",
        "location": "Remote, Berlin",
        "department": "",
        "url": "https://example.test/good",
        "external_id": "good-1",
        "snippet": "Drive the product roadmap end to end for our B2B platform.",
        "full_description": "Drive the product roadmap end to end. " * 10,
        "compensation": "",
        "org_override": "Acme GmbH",
        "org_url": "https://www.arbeitnow.com",
    }
    error_page = {
        "title": "Head of Community",
        "location": "Berlin",
        "department": "",
        "url": "https://example.test/junk",
        "external_id": "junk-1",
        "snippet": "",
        "full_description": "404 not found — the page you requested does not exist on this server anymore.",
        "compensation": "",
        "org_override": "Ghost GmbH",
        "org_url": "https://www.arbeitnow.com",
    }
    donation_widget = {
        "title": "Head of Partnerships",
        "location": "Berlin",
        "department": "",
        "url": "https://example.test/donate",
        "external_id": "junk-2",
        "snippet": "",
        "full_description": "Support our mission! Donate to a fund today and help us"
        " grow. Every contribution counts towards the campaign.",
        "compensation": "",
        "org_override": "Widget e.V.",
        "org_url": "https://www.arbeitnow.com",
    }

    new_count = db.save_board_vacancies(board_cfg, [good, error_page, donation_widget])
    db.get_conn().commit()

    vacs = db.load_vacancies(include_candidate_companies=True)
    by_title = {v["title"]: v for v in vacs.values()}

    # Donation widget rejected entirely.
    assert "Head of Partnerships" not in by_title
    assert new_count == 2

    # Error-page text blanked by the gate — never stored as a description.
    assert "404 not found" not in (by_title["Head of Community"].get("full_description") or "")

    # The good one is intact. In simple mode (SQLite) a board-discovered org
    # lands ACTIVE — there is no dashboard review step, so the candidate gate
    # would otherwise blackhole every board company.
    assert "Head of Product" in by_title
    cur = db.get_conn().cursor()
    cur.execute("SELECT status FROM company WHERE canonical_name = %s", ("Acme GmbH",))
    assert cur.fetchone()[0] == "active"
    cur.close()


def test_board_merge_sets_remote_work_mode(sqlite_dal):
    """'Remote, X' locations from the new boards parse into work_mode=remote."""
    db = sqlite_dal
    board_cfg = {"name": "Remotive", "url": "https://remotive.com", "tier": "B"}
    job = {
        "title": "Head of Product",
        "location": "Remote, Europe",
        "department": "Product",
        "url": "https://example.test/r1",
        "external_id": "r1",
        "snippet": "Lead the product organisation across our European markets.",
        "full_description": "Lead the product organisation across Europe. " * 10,
        "compensation": "$120k",
        "org_override": "Remote Co",
        "org_url": "https://remotive.com",
    }
    assert db.save_board_vacancies(board_cfg, [job]) == 1
    db.get_conn().commit()
    v = next(iter(db.load_vacancies(include_candidate_companies=True).values()))
    assert v["locations"][0]["work_mode"] == "remote"
