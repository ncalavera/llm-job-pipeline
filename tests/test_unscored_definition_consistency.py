"""'Unscored' must mean the same thing in the dashboard count and the scoring
load gate.

report.data_prep._count_unscored counts a vacancy as awaiting scoring when its
llm_score IS NULL *or* is negative (a failure sentinel). The DB load gate
(database_supabase.load_vacancies(unscored_only=True)) used to select only
IS NULL, so a -1 row was counted-but-never-offered — stranded forever. These
tests pin that both sides now agree on the same predicate, on a real SQLite
round-trip.
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


def _seed_scored_vacancy(dal, title, score):
    dal.ensure_company("Acme Labs", status="active")
    dal.get_conn().commit()
    dal.save_vacancies("Acme Labs", "A", [_job(title)])
    dal.get_conn().commit()
    h = dal.make_vacancy_id("Acme Labs", title)
    cur = dal.get_conn().cursor()
    cur.execute(
        "UPDATE vacancy SET llm_score = %s, status = 'unseen' WHERE dedup_hash = %s",
        (score, h),
    )
    dal.get_conn().commit()
    cur.close()


def test_negative_score_is_counted_and_offered(dal):
    """A -1 sentinel row is 'awaiting scoring' to the dashboard count, so the
    scoring load gate must ALSO offer it — no stranding."""
    from report.data_prep import _count_unscored

    _seed_scored_vacancy(dal, "Data Scientist", -1)

    all_vacs = dal.load_vacancies(include_inactive_companies=True, include_candidate_companies=True)
    assert _count_unscored(all_vacs) == 1  # the dashboard says: awaiting scoring

    unscored = dal.load_vacancies(unscored_only=True)
    assert len(unscored) == 1, "the load gate must offer the negative-score row too"
    vac = next(iter(unscored.values()))
    assert vac["org"] == "Acme Labs"
    assert vac["llm_score"] == -1


def test_zero_score_is_neither_counted_nor_offered(dal):
    """Boundary: score 0 is a real (lowest) score, not a sentinel — it must be
    absent from BOTH the count and the load gate, so the two stay consistent."""
    from report.data_prep import _count_unscored

    _seed_scored_vacancy(dal, "Junior Analyst", 0)

    all_vacs = dal.load_vacancies(include_inactive_companies=True, include_candidate_companies=True)
    assert _count_unscored(all_vacs) == 0
    assert dal.load_vacancies(unscored_only=True) == {}
