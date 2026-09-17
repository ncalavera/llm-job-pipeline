"""Cross-company posting-URL dedup on the direct-ATS save path.

``save_board_vacancies`` (job boards) already folds the same posting arriving
under two employer spellings into one row via a cross-company apply-URL index
(2026-09-14 audit, 5bb1370). ``save_vacancies`` (direct-ATS: a company's own
career page) never got that index — it was skipped because building it per
company call cost ~25s over a full run. This file pins the fix: the index is
built ONCE (``get_cross_company_url_index``) and threaded into ``save_vacancies``
the same way ``archived_hashes`` already is, so the same posting URL saved
under a board-created company row (e.g. "SASH Foundation") and a tracked
company row ("SASH") folds into one vacancy instead of two.

Same SQLite harness as tests/test_dedup_titles.py.
"""

import importlib
import sys

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
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"
    import database_supabase as db

    yield db
    db.close_conn()


def _job(title, *, url, city="Berlin, Germany"):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role, long enough to clear the content gate.",
        "full_description": f"We are hiring a {title}. " * 12 + "Own the work end to end.",
        "location": city,
        "url": url,
    }


def _vacancy_count(db):
    cur = db.get_conn().cursor()
    cur.execute("SELECT COUNT(*) FROM vacancy")
    n = cur.fetchone()[0]
    cur.close()
    return n


_URL = "https://sash.org/careers/program-officer/?utm_source=board"


def test_same_apply_url_different_company_rows_folds_to_one(dal):
    """The same posting saved under two employer spellings ("SASH Foundation"
    vs "SASH") merges onto one row when the URL and title match, mirroring
    the board-side fix (test_same_apply_url_across_boards_folds_to_one_row).
    """
    dal.ensure_company("SASH Foundation", status="active")
    dal.get_conn().commit()
    dal.save_vacancies(
        "SASH Foundation", "B", [_job("Program Officer", url=_URL)]
    )
    dal.get_conn().commit()

    dal.ensure_company("SASH", status="active")
    dal.get_conn().commit()
    new = dal.save_vacancies(
        "SASH", "B", [_job("Program Officer", url="https://sash.org/careers/program-officer")]
    )
    dal.get_conn().commit()

    assert new == 0
    assert _vacancy_count(dal) == 1


def test_different_title_same_url_stays_two_rows(dal):
    """A generic careers URL shared by two distinct roles must not fold them
    into one row — the title guard applies here exactly as it does cross-board.
    """
    dal.ensure_company("GenericCo A", status="active")
    dal.get_conn().commit()
    dal.save_vacancies("GenericCo A", "B", [_job("Program Officer", url=_URL)])
    dal.get_conn().commit()

    dal.ensure_company("GenericCo B", status="active")
    dal.get_conn().commit()
    new = dal.save_vacancies("GenericCo B", "B", [_job("Finance Manager", url=_URL)])
    dal.get_conn().commit()

    assert new == 1
    assert _vacancy_count(dal) == 2


def test_precomputed_cross_company_index_is_honored(dal):
    """fetch_vacancies.main's hoist pattern: build the index ONCE, pass it into
    every save_vacancies call. A precomputed index must be used as-is (no
    silent rebuild) so the run-wide cost stays a single query.
    """
    dal.ensure_company("SASH Foundation", status="active")
    dal.get_conn().commit()
    dal.save_vacancies("SASH Foundation", "B", [_job("Program Officer", url=_URL)])
    dal.get_conn().commit()

    dal.ensure_company("SASH", status="active")
    dal.get_conn().commit()
    prebuilt = dal.get_cross_company_url_index()
    new = dal.save_vacancies(
        "SASH",
        "B",
        [_job("Program Officer", url="https://sash.org/careers/program-officer")],
        None,
        prebuilt,
    )
    dal.get_conn().commit()

    assert new == 0
    assert _vacancy_count(dal) == 1
