"""Regression: an UNCHANGED firecrawl careers page must refresh last_seen on the
company's own live roles.

Bug: ``fetch_firecrawl_scrape`` returned ``[]`` when Firecrawl change-tracking
reported ``changeStatus == "same"``. An unchanged page means every previously
captured role is STILL listed, yet an empty result makes ``save_vacancies``
touch nothing — so ``last_seen`` freezes for the whole company and its roles
falsely age into Triage's "Expired" column (derived from
``last_seen >= STALE_SOURCE_DAYS``). This hit ``firecrawl_scrape`` — the single
largest strategy (113 companies in production).

Fix: "same" now returns a typed ``UnchangedListing`` sentinel (empty, but flagged
``unchanged``) instead of a bare list. The fetch driver recognises it and calls
``refresh_unchanged_company_last_seen`` — a narrow "still listed at source" touch
scoped to the company's non-archived rows. It never imports, resurrects or
rescores.

Provenance note: the vacancy table has no per-row source column, so ``company_id``
is the only provenance signal — the refresh is scoped to the firecrawl company's
own rows and leaves other companies' rows (where board-discovered postings live)
untouched.

Fully offline on the local SQLite backend; mirrors tests/test_gated_last_seen_refresh.py.
"""

import importlib
import sys
from datetime import date, datetime, timedelta

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
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


def _pipeline_today(db) -> date:
    import config

    return datetime.now(config.DASHBOARD_TZ).date()


def _insert_row(db, org, title, *, status, last_seen, first_seen="2026-02-22"):
    """Insert a vacancy row DIRECTLY for ``org`` (auto-creating the company),
    mirroring a role the source listed on a prior scrape."""
    cid = db.resolve_company_id(org) or db.ensure_company(org, status="active")
    dedup = db.make_vacancy_id(org, title)
    cur = db.get_conn().cursor()
    cur.execute(
        """INSERT INTO vacancy (dedup_hash, company_id, title, snippet, full_description,
                first_seen, last_seen, status, locations)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            dedup,
            cid,
            title,
            "a role the source still lists",
            "Full role description. " * 20,
            first_seen,
            last_seen,
            status,
            db.Json([{"work_mode": "remote", "url": "https://careers.example/roles/x"}]),
        ),
    )
    db.get_conn().commit()
    cur.close()
    return dedup


def _last_seen(db, dedup):
    cur = db.get_conn().cursor()
    cur.execute("SELECT last_seen, status FROM vacancy WHERE dedup_hash = %s", (dedup,))
    row = cur.fetchone()
    cur.close()
    return row  # (last_seen: date, status)


# ---------------------------------------------------------------------------
# refresh_unchanged_company_last_seen — the DAL touch the driver calls.
# ---------------------------------------------------------------------------


def test_unchanged_page_refreshes_own_live_rows(dal):
    """Every non-archived role of the firecrawl company refreshes to today, with
    its status untouched — the page is unchanged so all roles are still listed."""
    dal.ensure_company("Wikimedia Foundation", status="active")
    stale = (_pipeline_today(dal) - timedelta(days=21)).isoformat()
    d_unseen = _insert_row(dal, "Wikimedia Foundation", "Data Engineer", status="unseen", last_seen=stale)
    d_liked = _insert_row(dal, "Wikimedia Foundation", "Product Lead", status="liked", last_seen=stale)

    refreshed = dal.refresh_unchanged_company_last_seen("Wikimedia Foundation")
    dal.get_conn().commit()
    assert refreshed == 2

    for dedup, want_status in ((d_unseen, "unseen"), (d_liked, "liked")):
        after, after_status = _last_seen(dal, dedup)
        assert after == _pipeline_today(dal), "unchanged page should refresh last_seen to today"
        assert after_status == want_status, "refresh must not resurrect or change status"


def test_unchanged_refresh_leaves_archived_tombstone_untouched(dal):
    """An archived (gone-from-source) row is NOT refreshed — a tombstone stays a
    tombstone; only live rows get the 'still listed' touch."""
    dal.ensure_company("Wikimedia Foundation", status="active")
    stale = (_pipeline_today(dal) - timedelta(days=30)).isoformat()
    d_live = _insert_row(dal, "Wikimedia Foundation", "Data Engineer", status="unseen", last_seen=stale)
    d_dead = _insert_row(dal, "Wikimedia Foundation", "Old Role", status="archived", last_seen=stale)

    refreshed = dal.refresh_unchanged_company_last_seen("Wikimedia Foundation")
    dal.get_conn().commit()
    assert refreshed == 1, "only the one live row is refreshed"

    live_after, _ = _last_seen(dal, d_live)
    assert live_after == _pipeline_today(dal)
    dead_after, dead_status = _last_seen(dal, d_dead)
    assert str(dead_after) == stale, "archived tombstone last_seen must stay frozen"
    assert dead_status == "archived"


def test_unchanged_refresh_scoped_to_company(dal):
    """The refresh is scoped by company_id: another employer's rows — where a
    board-discovered posting lives — stay frozen (no per-row source column, so
    company_id is the provenance boundary)."""
    dal.ensure_company("Wikimedia Foundation", status="active")
    stale = (_pipeline_today(dal) - timedelta(days=21)).isoformat()
    d_own = _insert_row(dal, "Wikimedia Foundation", "Data Engineer", status="unseen", last_seen=stale)
    # A different employer, e.g. discovered via a job board.
    d_other = _insert_row(dal, "Some Board Employer", "Analyst", status="unseen", last_seen=stale)

    refreshed = dal.refresh_unchanged_company_last_seen("Wikimedia Foundation")
    dal.get_conn().commit()
    assert refreshed == 1

    own_after, _ = _last_seen(dal, d_own)
    assert own_after == _pipeline_today(dal)
    other_after, _ = _last_seen(dal, d_other)
    assert str(other_after) == stale, "another company's rows must stay untouched"


def test_unchanged_refresh_unknown_company_is_noop(dal):
    """An org we don't track updates zero rows — no crash, no phantom write."""
    assert dal.refresh_unchanged_company_last_seen("Never Heard Of Them") == 0


# ---------------------------------------------------------------------------
# UnchangedListing sentinel — how the fetcher surfaces "same" to the driver.
# ---------------------------------------------------------------------------


class TestUnchangedSentinel:
    def test_changestatus_same_returns_unchanged_sentinel(self, monkeypatch):
        """A byte-identical page returns an empty UnchangedListing the driver can
        recognise — NOT fabricated roles."""
        import fetchers

        org = "Wikimedia Foundation"
        monkeypatch.setattr(fetchers, "_firecrawl_credits_remaining", 100)

        class _CT:
            changeStatus = "same"

        class _SameResult:
            changeTracking = _CT()

        class _SameClient:
            def scrape(self, *a, **k):
                return _SameResult()

        monkeypatch.setattr(fetchers, "get_firecrawl_client", lambda: _SameClient())

        result = fetchers.fetch_firecrawl_scrape(org, "https://wikimedia.example/careers")
        assert getattr(result, "unchanged", False) is True
        assert list(result) == [], "an unchanged page fabricates no roles"
        assert fetchers.get_firecrawl_change_statuses().get(org) == "same"

    def test_driver_predicate_distinguishes_sentinel_from_normal_lists(self):
        """The driver's `getattr(jobs, "unchanged", False)` branch: the sentinel
        triggers the refresh; any plain list (empty or with roles from a genuinely
        changed page) flows through the normal path untouched."""
        from fetchers.firecrawl import UnchangedListing

        assert getattr(UnchangedListing(), "unchanged", False) is True
        assert getattr([], "unchanged", False) is False
        assert getattr([{"title": "Program Officer"}], "unchanged", False) is False
