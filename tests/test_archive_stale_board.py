"""Tests for archive_stale_board_vacancies — the board-row gone-from-source sweep.

Board-sourced vacancies have no ATS to reconcile against (archive_gone_vacancies
only runs on a direct re-fetch), so an unseen board row that drops off its board
lingers forever and wastes scoring budget. archive_stale_board_vacancies archives
board rows (source_board IS NOT NULL) whose last_seen predates a board_stale_days
cutoff, and must leave company-fetched rows (source_board NULL) and fresh board
rows alone.

SQLite-backed harness mirrors tests/test_score_tombstone_no_resurrect.py.
"""

import importlib
import sys
from datetime import date, timedelta

import pytest


def _force_sqlite(monkeypatch, db_file):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db

    return db


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db = _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    yield db
    db.close_conn()


def _insert(dal, *, dedup_hash, title, last_seen, source_board, status="unseen"):
    """Insert one vacancy row with explicit provenance/age for the sweep."""
    company_id = dal.resolve_company_id("Acme Labs")
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO vacancy (dedup_hash, company_id, title, first_seen, last_seen, "
        "status, source_board) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (dedup_hash, company_id, title, last_seen, last_seen, status, source_board),
    )
    dal.get_conn().commit()
    cur.close()


def _row(dal, dedup_hash):
    cur = dal.get_conn().cursor()
    cur.execute(
        "SELECT status, status_reason FROM vacancy WHERE dedup_hash = %s", (dedup_hash,)
    )
    r = cur.fetchone()
    cur.close()
    return {"status": r[0], "status_reason": r[1]}


def _add_migration_columns(dal):
    """Simulate a migrated DB (migrations 0013 + 0014) where vacancy carries
    source_board and status_reason. The base test schema predates both."""
    cur = dal.get_conn().cursor()
    cur.execute("ALTER TABLE vacancy ADD COLUMN source_board TEXT")
    cur.execute("ALTER TABLE vacancy ADD COLUMN status_reason TEXT")
    dal.get_conn().commit()
    cur.close()


@pytest.fixture()
def company(dal):
    _add_migration_columns(dal)
    dal.ensure_company("Acme Labs", status="active")
    dal.get_conn().commit()
    return dal


def test_stale_board_row_is_archived_with_reason(company):
    """A board-sourced unseen row older than board_stale_days is archived with the
    gone-from-source board reason and a fresh status_updated_at."""
    dal = company
    old = (date.today() - timedelta(days=30)).isoformat()
    _insert(dal, dedup_hash="h_stale", title="Stale Board Role", last_seen=old,
            source_board="80,000 Hours")

    n = dal.archive_stale_board_vacancies()  # default 14-day threshold
    dal.get_conn().commit()

    assert n == 1
    row = _row(dal, "h_stale")
    assert row["status"] == "archived"
    assert row["status_reason"] == (
        "gone_from_source — board-sourced, not re-seen for 14 days"
    )


def test_company_sourced_row_is_not_touched(company):
    """A company-fetched row (source_board NULL) is reconciled against its own ATS,
    never by this rule — even when equally stale."""
    dal = company
    old = (date.today() - timedelta(days=30)).isoformat()
    _insert(dal, dedup_hash="h_company", title="Direct ATS Role", last_seen=old,
            source_board=None)

    n = dal.archive_stale_board_vacancies()
    dal.get_conn().commit()

    assert n == 0
    assert _row(dal, "h_company")["status"] == "unseen"


def test_fresh_board_row_stays(company):
    """A board-sourced row still re-seen within the window is left untouched."""
    dal = company
    fresh = (date.today() - timedelta(days=1)).isoformat()
    _insert(dal, dedup_hash="h_fresh", title="Fresh Board Role", last_seen=fresh,
            source_board="80,000 Hours")

    n = dal.archive_stale_board_vacancies()
    dal.get_conn().commit()

    assert n == 0
    assert _row(dal, "h_fresh")["status"] == "unseen"
