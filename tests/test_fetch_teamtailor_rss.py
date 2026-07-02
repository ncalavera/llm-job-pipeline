"""Unit tests for fetch_teamtailor_rss (Teamtailor RSS feed).

DRIFT NOTE: some Teamtailor customers publish their feed on a custom
career-site domain instead of the default ``<slug>.teamtailor.com`` host —
e.g. Chatham House's feed lives at ``careers.chathamhouse.org/jobs.rss``,
not ``chathamhouse.teamtailor.com/jobs.rss`` (404). The fetcher now tries a
configured ``careers_url`` host first and falls back to the default host,
so neither a missing nor an unrelated ``careers_url`` regresses companies
that already worked. ``teamtailor_chatham_house_jobs.rss`` is a trimmed real
capture of that feed.
"""

import os
from urllib.parse import urlparse

import fetchers
from fetchers import fetch_teamtailor_rss
from fetchers.ats.teamtailor import _teamtailor_hosts

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


RSS_FEED = _load("teamtailor_chatham_house_jobs.rss")


class FakeResponse:
    def __init__(self, *, text="", status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    """Routes GETs by host; an unlisted host 404s like a real broken feed."""

    def __init__(self, pages: dict):
        self.pages = pages  # {host: FakeResponse}
        self.calls = []

    def get(self, url, timeout=None, **kwargs):
        self.calls.append(url)
        host = urlparse(url).netloc
        if host not in self.pages:
            return FakeResponse(status=404)
        return self.pages[host]


# ---------------------------------------------------------------------------
# host resolution (pure)
# ---------------------------------------------------------------------------


class TestTeamtailorHosts:
    def test_no_careers_url_uses_default(self):
        assert _teamtailor_hosts("chathamhouse") == ["chathamhouse.teamtailor.com"]

    def test_custom_domain_tried_first(self):
        hosts = _teamtailor_hosts("chathamhouse", "https://careers.chathamhouse.org/jobs")
        assert hosts == ["careers.chathamhouse.org", "chathamhouse.teamtailor.com"]

    def test_careers_url_matching_default_host_not_duplicated(self):
        hosts = _teamtailor_hosts("acme", "https://acme.teamtailor.com/jobs")
        assert hosts == ["acme.teamtailor.com"]

    def test_blank_careers_url_uses_default(self):
        assert _teamtailor_hosts("acme", "") == ["acme.teamtailor.com"]


# ---------------------------------------------------------------------------
# fetch_teamtailor_rss (end-to-end with mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchTeamtailorRss:
    def test_custom_domain_feed_parsed(self, monkeypatch):
        fake = FakeRequests({"careers.chathamhouse.org": FakeResponse(text=RSS_FEED)})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss(
            "Chatham House", "chathamhouse", careers_url="https://careers.chathamhouse.org/jobs"
        )
        assert len(jobs) == 2
        titles = {j["title"] for j in jobs}
        assert "Senior Research Fellow – International Security" in titles
        assert all(j["url"] and j["external_id"] for j in jobs)

    def test_custom_domain_tried_before_broken_default(self, monkeypatch):
        # chathamhouse.teamtailor.com 404s (unlisted); custom domain works and
        # is tried first, so the default host is never even called.
        fake = FakeRequests({"careers.chathamhouse.org": FakeResponse(text=RSS_FEED)})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss(
            "Chatham House", "chathamhouse", careers_url="https://careers.chathamhouse.org/jobs"
        )
        assert len(jobs) == 2
        assert fake.calls == ["https://careers.chathamhouse.org/jobs.rss"]

    def test_unrelated_careers_url_falls_back_to_default_host(self, monkeypatch):
        # careers_url points at a normal marketing page, not the RSS host;
        # the default <slug>.teamtailor.com host still works.
        fake = FakeRequests({"acme.teamtailor.com": FakeResponse(text=RSS_FEED)})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss("Acme", "acme", careers_url="https://www.acme.example/careers")
        assert len(jobs) == 2
        assert fake.calls == [
            "https://www.acme.example/jobs.rss",
            "https://acme.teamtailor.com/jobs.rss",
        ]

    def test_no_careers_url_uses_default_host_only(self, monkeypatch):
        fake = FakeRequests({"acme.teamtailor.com": FakeResponse(text=RSS_FEED)})
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss("Acme", "acme")
        assert len(jobs) == 2
        assert fake.calls == ["https://acme.teamtailor.com/jobs.rss"]

    def test_both_hosts_broken_returns_empty_without_crash(self, monkeypatch):
        fake = FakeRequests({})  # every host 404s
        monkeypatch.setattr(fetchers, "requests", fake)
        jobs = fetch_teamtailor_rss(
            "Acme", "acme", careers_url="https://www.acme.example/careers"
        )
        assert jobs == []
        # both candidate hosts were tried before giving up.
        assert len(fake.calls) == 2
