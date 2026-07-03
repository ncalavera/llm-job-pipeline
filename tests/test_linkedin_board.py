"""Characterization tests for the LinkedIn guest board fetcher.

Root cause these lock in: the guest listing + detail endpoints work, but under
LinkedIn's aggressive guest throttling the (best-effort) detail fetch fails, the
description fell back to the bare title (<50 chars), and the save-layer junk gate
dropped every such row as a nav snippet — a throttled run silently saved zero
while the board still reported "ok".

The fixtures below mirror the CURRENT guest markup (bare ``<li>`` cards,
``base-search-card__title`` / ``__subtitle`` / ``job-search-card__location``)
and are fully synthetic — no real person, org, or maintainer data. No test hits
the network; live verification runs through the real dispatch by hand.
"""

import sys
import time as _time
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import fetchers  # noqa: E402
import profile_targeting as pt  # noqa: E402


# ---------------------------------------------------------------------------
# Fake HTTP plumbing (records calls, routes by URL + params)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.content = text.encode()
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHTTP:
    def __init__(self, router):
        self.router = router
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        return self.router(url, params or {})

    def list_calls(self):
        return [c for c in self.calls if "seeMoreJobPostings" in c["url"]]


def _install(monkeypatch, router):
    fake = _FakeHTTP(router)
    monkeypatch.setattr(fetchers, "requests", fake)
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)
    fetchers._last_fetch_errors.clear()
    return fake


def _cfg(**over):
    base = {
        "name": "LinkedIn",
        "url": "https://www.linkedin.com/jobs",
        "board_blacklist": [],
        "pages": 1,
        "request_delay": 0,
        "fetch_detail": False,
        "queries": [{"keywords": "Programme Manager", "location": "London"}],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Fixtures — current guest markup, synthetic content
# ---------------------------------------------------------------------------

# Card 1 carries a company link (org_url); card 2 has a plain-text subtitle (an
# org with no LinkedIn page) — the parser must keep both. Hrefs carry the exact
# tracking junk LinkedIn appends (refId / trackingId / utm_*), which must be
# stripped so de-dup hashes stay stable.
SEARCH_HTML = """<!DOCTYPE html>
<ul class="jobs-search__results-list">
<li>
  <div class="base-card relative w-full base-search-card base-search-card--link job-search-card" data-entity-urn="urn:li:jobPosting:4400000001">
    <a class="base-card__full-link" href="https://uk.linkedin.com/jobs/view/programme-manager-at-acme-foundation-4400000001?position=1&amp;refId=AAA%3D%3D&amp;trackingId=BBB%3D%3D&amp;utm_source=share">link</a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">Programme Manager</h3>
      <h4 class="base-search-card__subtitle"><a class="hidden-nested-link" href="https://uk.linkedin.com/company/acme-foundation?trk=public_jobs_topcard-org-name">Acme Foundation</a></h4>
      <span class="job-search-card__location">London, England, United Kingdom</span>
    </div>
  </div>
</li>
<li>
  <div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:4400000002">
    <a class="base-card__full-link" href="https://uk.linkedin.com/jobs/view/policy-analyst-at-example-org-4400000002?refId=CCC&amp;trackingId=DDD">link</a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">Policy Analyst</h3>
      <h4 class="base-search-card__subtitle">Example Org</h4>
      <span class="job-search-card__location">Remote</span>
    </div>
  </div>
</li>
</ul>
"""

DETAIL_HTML = """<!DOCTYPE html>
<section class="show-more-less-html">
  <div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5">
    <p>We are hiring a Programme Manager to lead a portfolio of delivery workstreams across the organisation. You will own planning, reporting and stakeholder management end to end.</p>
  </div>
</section>
"""

# A real no-match guest page: tiny, no cards, no block marker → honest empty.
EMPTY_HTML = "<!DOCTYPE html>\n\n<!---->  "

# A bot-wall / challenge served with a 200 — must read as an error, not empty.
BLOCK_HTML = (
    "<!DOCTYPE html><html><head><title>LinkedIn</title></head><body>"
    "<div class='authwall'>Please sign in to continue</div>"
    "<form action='/uas/login'></form></body></html>"
)

# A substantial 200 with no parseable card = drifted/blocked markup, not empty.
UNPARSEABLE_HTML = (
    "<!DOCTYPE html><html><body><main class='drifted'>"
    + ("<div class='result'>content without any job card element</div>" * 12)
    + "</main></body></html>"
)


def _card(job_id, title="Analyst", org="Some Org", loc="Remote"):
    return (
        f"<li><div class='base-card job-search-card' "
        f'data-entity-urn="urn:li:jobPosting:{job_id}">'
        f'<a class="base-card__full-link" href="https://uk.linkedin.com/jobs/view/'
        f'role-{job_id}?refId=x">l</a>'
        f'<h3 class="base-search-card__title">{title}</h3>'
        f'<h4 class="base-search-card__subtitle"><a class="hidden-nested-link" '
        f'href="https://uk.linkedin.com/company/{org}">{org}</a></h4>'
        f'<span class="job-search-card__location">{loc}</span></div></li>'
    )


def _page(n_cards, start_id):
    lis = "".join(_card(start_id + i, title=f"Analyst {start_id + i}") for i in range(n_cards))
    return f"<!DOCTYPE html><ul class='jobs-search__results-list'>{lis}</ul>"


# ---------------------------------------------------------------------------
# 1. Current markup parses
# ---------------------------------------------------------------------------


def test_parses_current_markup(monkeypatch):
    _install(monkeypatch, lambda url, p: _Resp(text=SEARCH_HTML if p.get("start") == 0 else ""))
    out = fetchers.fetch_linkedin_board(_cfg())
    assert len(out) == 2

    pm = next(j for j in out if j["title"] == "Programme Manager")
    assert pm["org_override"] == "Acme Foundation"
    assert pm["external_id"] == "4400000001"
    assert pm["location"] == "London, England, United Kingdom"
    # Tracking junk stripped from BOTH the job URL and the org URL so de-dup
    # hashes are stable across runs.
    assert (
        pm["url"]
        == "https://uk.linkedin.com/jobs/view/programme-manager-at-acme-foundation-4400000001"
    )
    assert "?" not in pm["url"] and "utm_" not in pm["url"]
    assert pm["org_url"] == "https://uk.linkedin.com/company/acme-foundation"

    # Card 2's org has no company link — the plain-text subtitle is kept, not dropped.
    pa = next(j for j in out if j["title"] == "Policy Analyst")
    assert pa["org_override"] == "Example Org"
    assert pa["external_id"] == "4400000002"


# ---------------------------------------------------------------------------
# 2. The fatal bug: a description-less card must NOT be dropped as junk
# ---------------------------------------------------------------------------


def test_missing_detail_survives_content_gate(monkeypatch):
    """With detail off (the throttled reality) every card still passes the exact
    save-layer quality gate. The snippet carries an honest summary from the card
    fields and the description is left empty (an empty description is allowed
    through; a short one would trip the nav-snippet junk gate — the original bug).
    """
    from database_supabase import _gate_job

    _install(monkeypatch, lambda url, p: _Resp(text=SEARCH_HTML if p.get("start") == 0 else ""))
    out = fetchers.fetch_linkedin_board(_cfg(fetch_detail=False))
    assert len(out) == 2

    pm = next(j for j in out if j["title"] == "Programme Manager")
    assert pm["snippet"] == "Programme Manager at Acme Foundation — London, England, United Kingdom"
    assert pm["full_description"] == ""

    # The short-field card is exactly what used to vanish: "Policy Analyst at
    # Example Org — Remote" is under 50 chars, so it must NOT ride in the
    # description where the junk gate would kill it.
    pa = next(j for j in out if j["title"] == "Policy Analyst")
    assert pa["snippet"] == "Policy Analyst at Example Org — Remote"
    assert pa["full_description"] == ""

    for j in out:
        _title, skip_reason, _boiler = _gate_job(dict(j))
        assert skip_reason is None, f"{j['title']} was dropped by the save gate ({skip_reason})"


def test_detail_enriches_when_available(monkeypatch):
    def router(url, p):
        if "seeMoreJobPostings" in url:
            return _Resp(text=SEARCH_HTML if p.get("start") == 0 else "")
        return _Resp(text=DETAIL_HTML)

    _install(monkeypatch, router)
    out = fetchers.fetch_linkedin_board(_cfg(fetch_detail=True))
    pm = next(j for j in out if j["title"] == "Programme Manager")
    assert "portfolio of delivery workstreams" in pm["full_description"]


# ---------------------------------------------------------------------------
# 3. Honesty: block ≠ empty
# ---------------------------------------------------------------------------


def test_block_page_records_error_not_empty(monkeypatch):
    """A 200 auth-wall on every query → the fetcher raises FetchError, the
    registry guard records ``error: blocked`` and returns [] — never a silent
    empty-but-ok run."""
    _install(monkeypatch, lambda url, p: _Resp(text=BLOCK_HTML))
    out = fetchers.fetch_linkedin_board(_cfg())
    assert out == []
    assert fetchers.get_fetch_errors().get("LinkedIn") == "error: blocked"


def test_empty_result_is_honest_empty(monkeypatch):
    """A genuine no-match page (tiny, no cards, no block marker) → [] with NO
    recorded error. A real empty result must not masquerade as a failure."""
    _install(monkeypatch, lambda url, p: _Resp(text=EMPTY_HTML))
    out = fetchers.fetch_linkedin_board(_cfg())
    assert out == []
    assert fetchers.get_fetch_errors().get("LinkedIn") is None


def test_unparseable_page_records_error(monkeypatch):
    """A substantial 200 with no parseable card is a format break, not an empty
    result — it must be recorded so a drift can't hide as empty-but-ok."""
    _install(monkeypatch, lambda url, p: _Resp(text=UNPARSEABLE_HTML))
    out = fetchers.fetch_linkedin_board(_cfg())
    assert out == []
    assert fetchers.get_fetch_errors().get("LinkedIn") == "error: unparseable"


# ---------------------------------------------------------------------------
# 4. Pagination: per-query cap + stop on a short page
# ---------------------------------------------------------------------------


def test_pagination_respects_per_query_cap(monkeypatch):
    # Every page is full (25 cards) so nothing short-circuits — the `pages` cap
    # is the only thing that stops paging.
    fake = _install(
        monkeypatch, lambda url, p: _Resp(text=_page(25, start_id=int(p.get("start", 0)) + 1))
    )
    fetchers.fetch_linkedin_board(_cfg(pages=2))
    starts = [c["params"].get("start") for c in fake.list_calls()]
    assert starts == [0, 25]  # exactly `pages` requests, no more


def test_pagination_stops_on_short_page(monkeypatch):
    # A page shorter than 25 is the last one; paging stops even with pages=3.
    fake = _install(monkeypatch, lambda url, p: _Resp(text=_page(5, start_id=1)))
    fetchers.fetch_linkedin_board(_cfg(pages=3))
    assert len(fake.list_calls()) == 1


# ---------------------------------------------------------------------------
# 5. Dispatch: profile-derived queries reach the fetcher's requests
# ---------------------------------------------------------------------------


def test_profile_queries_reach_fetcher(monkeypatch):
    """No explicit queries in the board cfg → the fetcher resolves them from the
    profile's TARGET_ROLES and actually issues them as request params."""
    monkeypatch.setattr(pt, "_load_user_profile", lambda: {"TARGET_ROLES": "- Programme Manager\n"})
    fake = _install(monkeypatch, lambda url, p: _Resp(text=EMPTY_HTML))
    cfg = _cfg()
    cfg.pop("queries")  # force profile resolution
    fetchers.fetch_linkedin_board(cfg)
    assert fake.list_calls(), "the fetcher issued no listing request"
    assert any(c["params"].get("keywords") == "Programme Manager" for c in fake.list_calls()), (
        "the profile-derived query did not reach the fetcher"
    )
