"""Tests for archive_stale_board_vacancies — the board-row gone-from-source sweep.

Board-sourced vacancies have no ATS to reconcile against (archive_gone_vacancies
only runs on a direct re-fetch), so an unseen board row that drops off its board
lingers forever and wastes scoring budget. archive_stale_board_vacancies archives
board rows (source_board IS NOT NULL) whose last_seen predates a board_stale_days
cutoff — but ONLY for boards successfully fetched this run (positive evidence),
never on wall-clock alone. High-fit rows (llm_score >= PROTECT_SCORE) flip to
'expiring' instead; archived rows are tombstoned 'gone_from_source'; decided
statuses and company-fetched rows are never touched; 0 disables the sweep.

Every call passes stale_days explicitly so a BOARD_STALE_DAYS env override on
the host can never change the assertions (the fixture also clears it before the
config reload, mirroring the SUPABASE_DB_URL hygiene).

SQLite-backed harness mirrors tests/test_score_tombstone_no_resurrect.py.
"""

import importlib
import sys
from datetime import date, timedelta

import pytest

BOARD = "80,000 Hours"
STALE = (date.today() - timedelta(days=30)).isoformat()
FRESH = (date.today() - timedelta(days=1)).isoformat()


def _force_sqlite(monkeypatch, db_file):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.delenv("BOARD_STALE_DAYS", raising=False)
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


def _insert(
    dal,
    *,
    dedup_hash,
    title,
    last_seen,
    source_board,
    status="unseen",
    llm_score=None,
    status_reason=None,
):
    """Insert one vacancy row with explicit provenance/age for the sweep."""
    company_id = dal.resolve_company_id("Acme Labs")
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO vacancy (dedup_hash, company_id, title, first_seen, last_seen, "
        "status, source_board, llm_score, status_reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (dedup_hash, company_id, title, last_seen, last_seen, status, source_board,
         llm_score, status_reason),
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


def _tombstone(dal, dedup_hash):
    cur = dal.get_conn().cursor()
    cur.execute("SELECT reason FROM archived_hash WHERE dedup_hash = %s", (dedup_hash,))
    r = cur.fetchone()
    cur.close()
    return r[0] if r else None


def test_stale_board_row_is_archived_with_token_and_tombstone(company):
    """A board-sourced unseen row older than the threshold, whose board WAS
    fetched this run, is archived with the machine token and a gone_from_source
    tombstone (so a stale board snapshot cannot resurrect it)."""
    dal = company
    _insert(dal, dedup_hash="h_stale", title="Stale Board Role", last_seen=STALE,
            source_board=BOARD)

    n = dal.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal.get_conn().commit()

    assert n == 1
    row = _row(dal, "h_stale")
    assert row["status"] == "archived"
    assert row["status_reason"] == "board_stale"
    assert _tombstone(dal, "h_stale") == "gone_from_source"


def test_high_fit_row_flips_to_expiring_not_archived(company):
    """Latency protection (KTD1/KTD2): a stale board row scoring >= PROTECT_SCORE
    is flipped to 'expiring' (visible, alerted), never archived or tombstoned."""
    dal = company
    _insert(dal, dedup_hash="h_fit", title="High Fit Role", last_seen=STALE,
            source_board=BOARD, llm_score=84)

    n = dal.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal.get_conn().commit()

    assert n == 0 and n.protected == 1
    row = _row(dal, "h_fit")
    assert row["status"] == "expiring"
    assert row["status_reason"] is None
    assert _tombstone(dal, "h_fit") is None


def test_unfetched_board_is_never_swept(company):
    """Positive-evidence precondition: a stale row whose board was NOT
    successfully fetched this run (skipped via --no-boards / TTL, or a broken
    fetcher) is untouched — wall-clock staleness alone is not evidence."""
    dal = company
    _insert(dal, dedup_hash="h_skip", title="TTL Skipped Role", last_seen=STALE,
            source_board=BOARD)

    assert dal.archive_stale_board_vacancies(set(), stale_days=14) == 0
    assert dal.archive_stale_board_vacancies({"Other Board"}, stale_days=14) == 0
    dal.get_conn().commit()
    assert _row(dal, "h_skip")["status"] == "unseen"


def test_company_sourced_row_is_not_touched(company):
    """A company-fetched row (source_board NULL) is reconciled against its own ATS,
    never by this rule — even when equally stale."""
    dal = company
    _insert(dal, dedup_hash="h_company", title="Direct ATS Role", last_seen=STALE,
            source_board=None)

    n = dal.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal.get_conn().commit()

    assert n == 0
    assert _row(dal, "h_company")["status"] == "unseen"


def test_fresh_board_row_stays(company):
    """A board-sourced row still re-seen within the window is left untouched."""
    dal = company
    _insert(dal, dedup_hash="h_fresh", title="Fresh Board Role", last_seen=FRESH,
            source_board=BOARD)

    n = dal.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal.get_conn().commit()

    assert n == 0
    assert _row(dal, "h_fresh")["status"] == "unseen"


def test_decided_statuses_survive_the_sweep(company):
    """Only status='unseen' is swept: stale board rows the user already decided
    on (liked/to_apply) or that are held visible (expiring) are never revisited."""
    dal = company
    for i, status in enumerate(("liked", "to_apply", "expiring")):
        _insert(dal, dedup_hash=f"h_dec{i}", title=f"Decided Role {i}",
                last_seen=STALE, source_board=BOARD, status=status)

    n = dal.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal.get_conn().commit()

    assert n == 0 and n.protected == 0
    for i, status in enumerate(("liked", "to_apply", "expiring")):
        assert _row(dal, f"h_dec{i}")["status"] == status


def test_zero_stale_days_disables_the_sweep(company):
    """board_stale_days = 0 means DISABLED (llm_score_threshold convention) —
    NOT 'archive everything older than today'."""
    dal = company
    _insert(dal, dedup_hash="h_zero", title="Would Be Swept", last_seen=STALE,
            source_board=BOARD)

    n = dal.archive_stale_board_vacancies({BOARD}, stale_days=0)
    dal.get_conn().commit()

    assert n == 0
    assert _row(dal, "h_zero")["status"] == "unseen"


def test_premigration_schema_is_a_noop(dal):
    """On a schema predating migrations 0013/0014 (base test schema: no
    source_board / status_reason column) the sweep degrades to a clean no-op."""
    assert dal.archive_stale_board_vacancies({BOARD}, stale_days=14) == 0


def test_board_resurrect_clears_status_reason(company):
    """A row archived by the sweep and later re-listed by its board resurrects to
    'unseen' with status_reason cleared — a machine-archival reason must never
    sit on a live row."""
    dal = company
    org, title = "Acme Labs", "Reopened Role"
    h = dal.make_vacancy_id(org, title)
    _insert(dal, dedup_hash=h, title=title, last_seen=STALE, source_board=BOARD,
            status="archived", status_reason="board_stale")

    # No tombstone in archived_hash here: the board genuinely re-lists the role.
    board_cfg = {"name": BOARD, "url": "https://board.test/feed", "tier": "B"}
    job = {
        "title": title,
        "org_override": org,
        "location": "Berlin, Germany",
        "snippet": "A genuine snippet long enough to clear the content gate here.",
        "full_description": "Real long job description body here. " * 6,
    }
    dal.save_board_vacancies(board_cfg, [job])
    dal.get_conn().commit()

    row = _row(dal, h)
    assert row["status"] == "unseen"
    assert row["status_reason"] is None
