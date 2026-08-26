"""Tests for the scripts/fetch_vacancies.py driver.

Covers the real dispatch chain end-to-end (network mocked), fair fetch
rotation ordering, fetch_status reason codes, and fetch_stats.json writes.
Absorbed test_fetch_vacancies_dispatch.py, test_fetch_rotation.py,
test_fetch_status_reason_codes.py, test_fetch_stats_write.py.
"""

from __future__ import annotations

import importlib
import os
import sys
from urllib.parse import urlparse

import fetchers
import fetch_vacancies as fv
from fetch_vacancies import _resolve_fetch_status
from database_supabase import is_fetch_error

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


# ---------------------------------------------------------------------------
# --- from test_fetch_vacancies_dispatch.py ---
# The REAL fetch_vacancies dispatch chain, mocked only at the HTTP layer.
#
# These are the regression guards the unit tests could not give: they call
# ``fetch_vacancies.main()`` end-to-end on a throwaway SQLite DB and mock the
# network, so they exercise the actual dispatch branch a production run takes —
# not the fetcher functions in isolation.
#
# Two bugs the isolated tests missed:
#
# 1. The ``teamtailor_rss`` explicit branch ran BEFORE the ``COMPANY_FETCHERS``
#    registry fallback and called the fetcher WITHOUT ``careers_url`` — so the
#    registry entry point that wires the custom domain was dead code, and Chatham
#    House kept hitting the 404 default host. This proves the custom domain is
#    actually reached through the dispatch.
#
# 2. A DB strategy with no explicit branch and no registered fetcher left
#    ``jobs = []`` and reported a successful-but-empty fetch (the mechanism that
#    hid smartrecruiters for weeks). This proves such a gap now records an error.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# --- from test_fetch_rotation.py ---
# Fair fetch rotation: engaged/overdue tracked orgs must not starve.
#
# The old rotation sorted the due set by ``(last_fetched_epoch, name)`` with
# never-fetched pinned to ``0.0`` — so every never-fetched candidate sorted AHEAD
# of any previously-fetched org. As new candidate companies kept arriving, an
# overdue tracked org with live liked/decided roles was permanently crowded out
# below the per-run volume cap and never refreshed.
#
# ``_order_due_companies`` fixes the ordering with guaranteed cohorts:
# overdue+engaged, then overdue tracked, then never-fetched — most-overdue first
# inside each. These tests pin the acceptance behaviour on invented records
# (pure function, no DB, no network).
# ---------------------------------------------------------------------------

DAY = 86400.0
NOW = 1_000_000_000.0  # arbitrary fixed epoch


def _rec(name, *, age_days=None, ttl_days=7, engaged=False):
    """Build a priority record. ``age_days=None`` means never fetched."""
    last = None if age_days is None else NOW - age_days * DAY
    return {"name": name, "last_fetched": last, "ttl_days": ttl_days, "engaged": engaged}


def test_engaged_overdue_beats_never_fetched_when_cap_tight():
    """The IRC case: an engaged, overdue tracked org outranks fresh newcomers.

    Cap of 1 would, under the old rule, go to a never-fetched candidate; now the
    engaged overdue org wins the single slot."""
    records = [
        _rec("NewCandidateA"),  # never fetched
        _rec("NewCandidateB"),  # never fetched
        _rec("IRC", age_days=18, ttl_days=7, engaged=True),  # overdue + engaged
    ]
    order = fv._order_due_companies(records, NOW)
    assert order[0] == "IRC"

    kept, deferred = fv._apply_company_cap(order, {n: {} for n in order}, cap=1)
    assert set(kept) == {"IRC"}
    assert deferred == 2


def test_cohort_precedence_engaged_then_tracked_then_never():
    """Full cohort order: overdue+engaged, then overdue tracked, then newcomers."""
    records = [
        _rec("Never1"),
        _rec("TrackedOverdue", age_days=10, ttl_days=7),
        _rec("EngagedOverdue", age_days=8, ttl_days=7, engaged=True),
        _rec("Never2"),
    ]
    order = fv._order_due_companies(records, NOW)
    assert order == ["EngagedOverdue", "TrackedOverdue", "Never1", "Never2"]


def test_most_overdue_wins_inside_cohort_by_ratio():
    """Within a cohort the overdue RATIO (age/ttl) ranks, not raw age.

    An S-tier (ttl=3) fetched 4 days ago (ratio 1.33) is more overdue than an
    A-tier (ttl=5) fetched 4 days ago (ratio 0.8), despite equal age."""
    records = [
        _rec("A_tier", age_days=4, ttl_days=5),  # ratio 0.8
        _rec("S_tier", age_days=4, ttl_days=3),  # ratio 1.33
    ]
    order = fv._order_due_companies(records, NOW)
    assert order == ["S_tier", "A_tier"]


def test_never_fetched_get_slots_when_nothing_overdue():
    """With no overdue tracked orgs in the due set, newcomers fill the run.

    (The stale-set builder only puts a tracked org in ``records`` once it is
    past its TTL, so "no overdue tracked" == only never-fetched records here.)"""
    records = [_rec("Beta"), _rec("Alpha"), _rec("Gamma")]
    order = fv._order_due_companies(records, NOW)
    assert order == ["Alpha", "Beta", "Gamma"]  # deterministic by name

    kept, deferred = fv._apply_company_cap(order, {n: {} for n in order}, cap=2)
    assert set(kept) == {"Alpha", "Beta"}
    assert deferred == 1


def test_ordering_is_deterministic_and_total():
    """Same input ⇒ same order; every input name appears exactly once."""
    records = [
        _rec("Zeta", age_days=9, ttl_days=7, engaged=True),
        _rec("Yield", age_days=9, ttl_days=7, engaged=True),  # tie on ratio → name
        _rec("Newbie"),
        _rec("Overdue", age_days=20, ttl_days=7),
    ]
    order1 = fv._order_due_companies(records, NOW)
    order2 = fv._order_due_companies(list(reversed(records)), NOW)
    assert order1 == order2
    assert sorted(order1) == sorted(r["name"] for r in records)
    # Tie between two engaged, equally-overdue orgs breaks by name.
    assert order1[:2] == ["Yield", "Zeta"]


def test_engaged_statuses_exclude_negative_decisions():
    """Engagement = positive interest only; passed/skipped are not engagement."""
    assert set(fv.ENGAGED_STATUSES) == {
        "liked",
        "to_apply",
        "to_research",
        "to_network",
        "applied",
    }
    assert "passed" not in fv.ENGAGED_STATUSES
    assert "skipped" not in fv.ENGAGED_STATUSES


# ---------------------------------------------------------------------------
# --- from test_fetch_status_reason_codes.py ---
# fetch_status reason codes: distinguish empty from broken (U9 / WS6).
#
# Covers the three outcomes that used to collapse into the ambiguous ``no_data``:
#   * ``render_ok_zero``   — a real successful fetch that is genuinely empty
#   * ``credit_exhausted`` — a firecrawl_scrape aborted because credits hit 0
#   * ``js_required``      — a JS-rendered shell (UNCHANGED regression guard)
# ---------------------------------------------------------------------------


class TestResolveFetchStatus:
    def test_successful_empty_becomes_render_ok_zero(self):
        assert _resolve_fetch_status("ok", False, None) == "render_ok_zero"

    def test_js_required_shell_unchanged(self):
        # Regression guard: the pre-U9 behaviour was ok + no jobs + js_required.
        assert _resolve_fetch_status("ok", False, "js_required") == "js_required"

    def test_credit_exhausted_recorded(self):
        assert _resolve_fetch_status("ok", False, "credit_exhausted") == "credit_exhausted"

    def test_jobs_present_stays_ok(self):
        # Non-empty fetch is never downgraded, whatever the scrape override says.
        assert _resolve_fetch_status("ok", True, "js_required") == "ok"
        assert _resolve_fetch_status("ok", True, None) == "ok"

    def test_error_status_passes_through(self):
        assert _resolve_fetch_status("error: boom", False, None) == "error: boom"
        assert _resolve_fetch_status("error: boom", False, "credit_exhausted") == "error: boom"


# ---------------------------------------------------------------------------
# is_fetch_error — render_ok_zero is healthy, the rest are broken
# ---------------------------------------------------------------------------


class TestIsFetchErrorClassification:
    def test_render_ok_zero_is_not_an_error(self):
        assert is_fetch_error("render_ok_zero") is False

    def test_ok_and_no_data_not_errors(self):
        assert is_fetch_error("ok") is False
        assert is_fetch_error("no_data") is False

    def test_broken_codes_are_errors(self):
        assert is_fetch_error("credit_exhausted") is True
        assert is_fetch_error("js_required") is True
        assert is_fetch_error("error: HTTP 500") is True


# ---------------------------------------------------------------------------
# fetch_firecrawl_scrape — records credit_exhausted when credits == 0
# ---------------------------------------------------------------------------


class TestCreditExhaustedOverride:
    def test_credit_gate_records_credit_exhausted(self, monkeypatch):
        org = "British International Investment"
        # Force credits to 0 (short-circuits the balance check, no network) and
        # stub the local fallback so no real HTTP happens.
        monkeypatch.setattr(fetchers, "_firecrawl_credits_remaining", 0)
        monkeypatch.setitem(fetchers._last_scrape_status, org, None)
        monkeypatch.setattr(fetchers, "_fetch_local_scrape", lambda *a, **k: [])

        jobs = fetchers.fetch_firecrawl_scrape(org, "https://isw.example/careers")
        assert jobs == []
        assert fetchers.get_scrape_statuses().get(org) == "credit_exhausted"

        # End-to-end: the status assignment turns that into credit_exhausted.
        resolved = _resolve_fetch_status("ok", bool(jobs), fetchers.get_scrape_statuses().get(org))
        assert resolved == "credit_exhausted"

    def test_quota_error_marks_credit_exhausted(self, monkeypatch):
        org = "CTG"
        # Credits look available, but the SDK raises a quota error mid-scrape.
        monkeypatch.setattr(fetchers, "_firecrawl_credits_remaining", 100)
        monkeypatch.setitem(fetchers._last_scrape_status, org, None)

        class _QuotaClient:
            def scrape(self, *a, **k):
                raise RuntimeError("402 payment required: insufficient credit")

        monkeypatch.setattr(fetchers, "get_firecrawl_client", lambda: _QuotaClient())
        monkeypatch.setattr(fetchers, "_fetch_local_scrape", lambda *a, **k: [])

        jobs = fetchers.fetch_firecrawl_scrape(org, "https://ctg.example/jobs")
        assert jobs == []
        assert fetchers.get_scrape_statuses().get(org) == "credit_exhausted"


# ---------------------------------------------------------------------------
# --- from test_fetch_stats_write.py ---
# _write_fetch_stats must not lie to the publish gate.
#
# The gate reads vacancies/fetch_stats.json to decide whether a truncated fetch
# mass-archived an org's live roles. A silently-swallowed write failure would
# leave a PRIOR run's fetch_stats.json on disk, and the gate would evaluate this
# run against last run's numbers — masking this run's truncation. So a failed
# write logs loudly AND removes any stale file (honest "absent", not stale).
# ---------------------------------------------------------------------------


def test_write_fetch_stats_happy_path_persists(monkeypatch, tmp_path):
    import fetch_vacancies as fv

    stats_path = tmp_path / "fetch_stats.json"
    monkeypatch.setattr(fv, "FETCH_STATS_PATH", stats_path)

    fv._write_fetch_stats({"orgs": {"NewCo": {"gone": 1, "live": 9}}})

    assert stats_path.exists()
    import json

    assert json.loads(stats_path.read_text(encoding="utf-8"))["orgs"]["NewCo"]["gone"] == 1


def test_write_fetch_stats_failure_removes_stale_and_warns(monkeypatch, tmp_path, capsys):
    import fetch_vacancies as fv

    stats_path = tmp_path / "fetch_stats.json"
    monkeypatch.setattr(fv, "FETCH_STATS_PATH", stats_path)

    # A prior run's telemetry sits on disk (benign numbers)...
    stats_path.write_text('{"orgs": {"OldCo": {"gone": 40, "live": 5}}}', encoding="utf-8")

    # ...and this run's write fails: a set is not JSON-serializable, so
    # json.dumps raises inside _write_fetch_stats.
    fv._write_fetch_stats({"orgs": {"NewCo": {1, 2, 3}}})

    # The stale file is gone — the gate now reads "absent" (no signal), never a
    # previous run's numbers dressed up as this run's.
    assert not stats_path.exists()

    # And the failure was announced loudly on stderr, not swallowed.
    err = capsys.readouterr().err.lower()
    assert "fetch telemetry" in err
    assert "stale" in err


def test_write_fetch_stats_failure_without_stale_file_still_warns(monkeypatch, tmp_path, capsys):
    import fetch_vacancies as fv

    stats_path = tmp_path / "fetch_stats.json"
    monkeypatch.setattr(fv, "FETCH_STATS_PATH", stats_path)

    # No prior file on disk; a failed write must warn without crashing.
    fv._write_fetch_stats({"orgs": {"NewCo": {1, 2, 3}}})

    assert not stats_path.exists()
    assert "fetch telemetry" in capsys.readouterr().err.lower()
