"""Regression: a role the source STILL lists must refresh last_seen even when our
import gate drops it.

Bug: a "talent pool" posting the user had already liked/decided kept a frozen
last_seen because ``_gate_job`` blacklists the title and ``save_vacancies`` /
``save_board_vacancies`` ``continue`` before touching the existing row. The
Triage "Expired" column derives from ``last_seen >= STALE_SOURCE_DAYS``, so the
still-open role rendered as expired (GiveWell "Talent pool" was the real case).

Fix: when a gated job's exact org+title matches a stored row, bump its last_seen
(``_refresh_gated_last_seen``) — a "still open at source" touch that never
imports, resurrects or rescores.

Fully offline on the local SQLite backend; mirrors tests/test_archive_restore.py.
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
    """Insert a vacancy row DIRECTLY (the gate would block save_vacancies from
    importing a junk title), mirroring a role the source listed before it became
    junk-blacklisted / that the user has already decided on."""
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
            "join our pool",
            "We keep your CV on file for future roles. " * 20,
            first_seen,
            last_seen,
            status,
            db.Json([{"work_mode": "remote", "url": "https://boards.example/talent-pool"}]),
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


def _talent_pool_job():
    """A greenhouse-style row for a 'Talent pool' posting the source still lists."""
    return {
        "title": "Talent pool",
        "location": "Remote",
        "url": "https://boards.example/talent-pool",
        "external_id": "4053379008",
        "snippet": "join our pool",
        "full_description": "We keep your CV on file for future roles. " * 20,
    }


# ---------------------------------------------------------------------------
# Company (direct-ATS) path — the GiveWell greenhouse case.
# ---------------------------------------------------------------------------


def test_gated_still_listed_role_refreshes_last_seen(dal):
    dal.ensure_company("GiveWell", status="active")
    stale = (_pipeline_today(dal) - timedelta(days=21)).isoformat()
    dedup = _insert_row(dal, "GiveWell", "Talent pool", status="skipped", last_seen=stale)

    before, before_status = _last_seen(dal, dedup)
    assert str(before) == stale
    assert before_status == "skipped"

    # The junk title is gated, so nothing is imported (new_count 0) — but the
    # already-known, still-listed role must have its last_seen refreshed.
    new_count = dal.save_vacancies("GiveWell", "S", [_talent_pool_job()])
    dal.get_conn().commit()
    assert new_count == 0

    after, after_status = _last_seen(dal, dedup)
    assert after == _pipeline_today(dal), "still-listed gated role should refresh to today"
    assert after_status == "skipped", "refresh must not resurrect or change status"

    # No phantom / duplicate row was created for the gated title.
    cur = dal.get_conn().cursor()
    cur.execute("SELECT count(*) FROM vacancy WHERE title = %s", ("Talent pool",))
    assert cur.fetchone()[0] == 1
    cur.close()


def test_gated_unknown_title_creates_no_row(dal):
    """A brand-new junk posting we do NOT already track updates zero rows — the
    gate still blocks the import, no phantom row appears."""
    dal.ensure_company("GiveWell", status="active")
    new_count = dal.save_vacancies("GiveWell", "S", [_talent_pool_job()])
    dal.get_conn().commit()
    assert new_count == 0
    cur = dal.get_conn().cursor()
    cur.execute("SELECT count(*) FROM vacancy")
    assert cur.fetchone()[0] == 0
    cur.close()


def test_gated_refresh_leaves_archived_tombstone_untouched(dal):
    """An archived (gone-from-source) junk row is NOT refreshed — a tombstone
    stays a tombstone; only live rows get the 'still listed' touch."""
    dal.ensure_company("GiveWell", status="active")
    stale = (_pipeline_today(dal) - timedelta(days=30)).isoformat()
    dedup = _insert_row(dal, "GiveWell", "Talent pool", status="archived", last_seen=stale)

    dal.save_vacancies("GiveWell", "S", [_talent_pool_job()])
    dal.get_conn().commit()

    after, after_status = _last_seen(dal, dedup)
    assert str(after) == stale, "archived tombstone last_seen must stay frozen"
    assert after_status == "archived"


# ---------------------------------------------------------------------------
# Board path — same fix in save_board_vacancies.
# ---------------------------------------------------------------------------


def test_board_gated_still_listed_role_refreshes_last_seen(dal):
    dal.ensure_company("GiveWell", status="active")
    stale = (_pipeline_today(dal) - timedelta(days=18)).isoformat()
    dedup = _insert_row(dal, "GiveWell", "Talent pool", status="liked", last_seen=stale)

    board_cfg = {"name": "GiveWell", "url": "https://boards.example", "tier": "C"}
    job = _talent_pool_job()
    dal.save_board_vacancies(board_cfg, [job])
    dal.get_conn().commit()

    after, after_status = _last_seen(dal, dedup)
    assert after == _pipeline_today(dal)
    assert after_status == "liked"
