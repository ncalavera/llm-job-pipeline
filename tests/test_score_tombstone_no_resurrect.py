"""Regression: a 'score_below_threshold' tombstone is not resurrected.

A vacancy we buried for a low score must NOT loop through
resurrection -> re-score -> re-archive every run when the company's own ATS keeps
listing it. The direct-ATS save path loads get_archived_hashes(include_gone=False),
which drops ONLY 'gone_from_source' tombstones (so a role the source merely
dropped can reopen) while keeping every other reason — crucially
'score_below_threshold' — in the blocking set.

Contrast pinned here: a 'gone_from_source' tombstone does NOT block the direct
ATS path (the company re-listing is ground truth the role reopened).

Harness mirrors tests/test_save_board_vacancies_characterization.py.
"""

import importlib
import sys

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


def _job(title):
    return {
        "title": title,
        "location": "Berlin, Germany",
        "snippet": "A genuine snippet long enough to clear the content gate here.",
        "full_description": "Real long job description body here. " * 6,
    }


def _rows(dal, dedup_hash):
    cur = dal.get_conn().cursor()
    cur.execute("SELECT COUNT(*) FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
    n = cur.fetchone()[0]
    cur.close()
    return n


def test_score_below_threshold_tombstone_blocks_reimport(dal):
    """Buried-for-low-score role: archived (row deleted + tombstone), then the ATS
    lists it again -> save_vacancies must skip it (no re-insert, no re-score)."""
    dal.ensure_company("Acme Labs", status="active")
    dal.get_conn().commit()
    dal.save_vacancies("Acme Labs", "A", [_job("Junior Data Entry")])
    dal.get_conn().commit()
    h = dal.make_vacancy_id("Acme Labs", "Junior Data Entry")

    cur = dal.get_conn().cursor()
    cur.execute("UPDATE vacancy SET llm_score = 8, status = 'unseen' WHERE dedup_hash = %s", (h,))
    dal.get_conn().commit()
    cur.close()

    dal.archive_vacancies(force=True)  # deletes row + records 'score_below_threshold'
    assert _rows(dal, h) == 0

    cur = dal.get_conn().cursor()
    cur.execute("SELECT reason FROM archived_hash WHERE dedup_hash = %s", (h,))
    assert cur.fetchone()[0] == "score_below_threshold"
    cur.close()

    # The company's ATS still lists the same role next run.
    new = dal.save_vacancies("Acme Labs", "A", [_job("Junior Data Entry")])
    dal.get_conn().commit()

    assert new == 0, "a score_below_threshold role must not be re-imported"
    assert _rows(dal, h) == 0, "no resurrected row -> nothing to re-score/re-archive"


def test_gone_from_source_tombstone_does_not_block_reimport(dal):
    """Contrast: a 'gone_from_source' tombstone is dropped on the direct-ATS path,
    so the company re-listing the role resurrects it (new row inserted)."""
    dal.ensure_company("Acme Labs", status="active")
    dal.get_conn().commit()
    h = dal.make_vacancy_id("Acme Labs", "Senior Engineer")
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason) VALUES (%s, %s)",
        (h, "gone_from_source"),
    )
    dal.get_conn().commit()
    cur.close()

    new = dal.save_vacancies("Acme Labs", "A", [_job("Senior Engineer")])
    dal.get_conn().commit()

    assert new == 1, "gone_from_source must not block a direct-ATS re-listing"
    assert _rows(dal, h) == 1


def test_include_gone_sets_partition_reasons(dal):
    """Pin the set semantics both paths rely on: include_gone=False keeps
    score_below_threshold but drops gone_from_source; include_gone=True keeps both."""
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason) VALUES ('h_low', 'score_below_threshold')"
    )
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason) VALUES ('h_gone', 'gone_from_source')"
    )
    dal.get_conn().commit()
    cur.close()

    ats = dal.get_archived_hashes(include_gone=False)  # direct ATS path
    board = dal.get_archived_hashes(include_gone=True)  # board path

    assert "h_low" in ats and "h_gone" not in ats
    assert "h_low" in board and "h_gone" in board
