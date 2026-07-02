"""The REAL fetch_vacancies dispatch chain, mocked only at the HTTP layer.

These are the regression guards the unit tests could not give: they call
``fetch_vacancies.main()`` end-to-end on a throwaway SQLite DB and mock the
network, so they exercise the actual dispatch branch a production run takes —
not the fetcher functions in isolation.

Two bugs the isolated tests missed:

1. The ``teamtailor_rss`` explicit branch ran BEFORE the ``COMPANY_FETCHERS``
   registry fallback and called the fetcher WITHOUT ``careers_url`` — so the
   registry entry point that wires the custom domain was dead code, and Chatham
   House kept hitting the 404 default host. This proves the custom domain is
   actually reached through the dispatch.

2. A DB strategy with no explicit branch and no registered fetcher left
   ``jobs = []`` and reported a successful-but-empty fetch (the mechanism that
   hid smartrecruiters for weeks). This proves such a gap now records an error.
"""

import importlib
import os
import sys
from urllib.parse import urlparse

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

# Same module chain the report-only test rebinds to a fresh temp SQLite backend.
_CHAIN_PREFIXES = {
    "database_supabase",
    "config",
    "company_registry",
    "db_conn",
    "db_backend",
    "report",
    "fetchers",
    "fetch_vacancies",
    "run_status",
}


def _reset_backend(monkeypatch, db_file):
    """Point the whole backend chain at a fresh temp SQLite DB (no Supabase).

    Returns ``(db, saved_modules)``. Reloading the chain replaces the cached
    ``fetchers``/``fetch_vacancies`` module objects in ``sys.modules``; other
    already-imported test modules hold references to the ORIGINAL objects, so
    the caller MUST ``_restore_modules(saved)`` in teardown to avoid leaking a
    reloaded ``fetchers`` into tests that sort after this one.
    """
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    saved = {n: m for n, m in sys.modules.items() if n.split(".")[0] in _CHAIN_PREFIXES}
    for name in saved:
        sys.modules.pop(name, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "these tests must run on the SQLite backend"

    import database_supabase as db

    return db, saved


def _restore_modules(saved: dict) -> None:
    """Undo the chain reload: drop the temp-SQLite-bound modules and reinstate
    the original module objects the rest of the suite already imported."""
    for name in list(sys.modules):
        if name.split(".")[0] in _CHAIN_PREFIXES:
            sys.modules.pop(name, None)
    sys.modules.update(saved)


def _quiet_disk_writes(monkeypatch, fv, run_status, tmp_path):
    """Redirect the run's gitignored runtime-state files into tmp_path so a
    dispatch run leaves nothing under the repo's vacancies/ tree."""
    monkeypatch.setattr(run_status, "STATUS_PATH", tmp_path / "run_status.json", raising=False)
    monkeypatch.setattr(fv, "FETCH_STATS_PATH", tmp_path / "fetch_stats.json", raising=False)
    monkeypatch.setattr(fv, "FETCH_LOG_DIR", tmp_path / "fetch_log", raising=False)


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
        return self.pages.get(host) or FakeResponse(status=404)


def _fetch_status(db, canonical: str):
    cur = db.get_conn().cursor()
    cur.execute("SELECT fetch_status FROM company WHERE canonical_name = %s", (canonical,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _run_main(fv, monkeypatch):
    """Fetch one company through the real dispatch — no boards, no enrich, no
    dashboard/publish (just the fetch → save → source-tracking path)."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch_vacancies.py", "--force-all", "--no-boards", "--no-auto-enrich", "--no-dashboard"],
    )
    fv.main()


# ---------------------------------------------------------------------------
# Finding #1 — the teamtailor_rss explicit branch must forward careers_url
# ---------------------------------------------------------------------------


def test_teamtailor_careers_url_reaches_the_custom_domain(tmp_path, monkeypatch):
    db, saved = _reset_backend(monkeypatch, tmp_path / "jobsearch.db")
    try:
        db.ensure_company("Chatham House", status="active")
        db.get_conn().commit()

        import fetch_vacancies as fv
        import fetchers
        import run_status

        _quiet_disk_writes(monkeypatch, fv, run_status, tmp_path)

        # RSS lives ONLY on the custom career-site domain; the default
        # <slug>.teamtailor.com host 404s (Chatham House's real situation).
        # Before the fix the explicit branch called the fetcher WITHOUT
        # careers_url → only the 404 host is tried → zero jobs.
        fake = FakeRequests({"careers.chathamhouse.org": FakeResponse(text=RSS_FEED)})
        monkeypatch.setattr(fetchers, "requests", fake)
        monkeypatch.setattr(
            fv,
            "COMPANIES",
            {
                "Chatham House": {
                    "strategy": "teamtailor_rss",
                    "slug": "chathamhouse",
                    "careers_url": "https://careers.chathamhouse.org/jobs",
                    "tier": "S",
                }
            },
            raising=False,
        )

        _run_main(fv, monkeypatch)

        # The custom domain was actually requested through the dispatch...
        assert "https://careers.chathamhouse.org/jobs.rss" in fake.calls
        # ...and its feed flowed all the way to saved vacancies (2 in the feed).
        vac = db.load_vacancies(include_inactive_companies=True)
        titles = {v["title"] for v in vac.values()}
        assert "Senior Research Fellow – International Security" in titles
        assert len(vac) == 2
        assert _fetch_status(db, "Chatham House") == "ok"
    finally:
        db.close_conn()
        _restore_modules(saved)


# ---------------------------------------------------------------------------
# Finding #2 — an unregistered strategy must record an error, not empty-success
# ---------------------------------------------------------------------------


def test_unregistered_strategy_records_error_not_silent_empty(tmp_path, monkeypatch):
    db, saved = _reset_backend(monkeypatch, tmp_path / "jobsearch.db")
    try:
        db.ensure_company("Bogus Co", status="active")
        db.get_conn().commit()

        import fetch_vacancies as fv
        import fetchers
        import run_status

        _quiet_disk_writes(monkeypatch, fv, run_status, tmp_path)

        # A strategy with no explicit elif branch AND absent from COMPANY_FETCHERS.
        fake = FakeRequests({})  # no host should ever be hit
        monkeypatch.setattr(fetchers, "requests", fake)
        monkeypatch.setattr(
            fv,
            "COMPANIES",
            {"Bogus Co": {"strategy": "no_such_strategy", "slug": "bogus", "tier": "B"}},
            raising=False,
        )

        _run_main(fv, monkeypatch)

        # No network was attempted (nothing dispatched)...
        assert fake.calls == []
        # ...and the run recorded WHY it produced nothing — an honest error, not
        # a silent zero-vacancy success.
        status = _fetch_status(db, "Bogus Co")
        assert status is not None
        assert "no fetcher registered" in status
        assert status.startswith("error:")
        # database_supabase.is_fetch_error must agree it is broken, not empty.
        assert db.is_fetch_error(status) is True
    finally:
        db.close_conn()
        _restore_modules(saved)
