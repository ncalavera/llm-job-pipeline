"""Tests for the add-source persistence seam — save_vacancies +
update_source_tracking with their REAL current signatures.

This doubles as a SIGNATURE-DRIFT GUARD: the /jobs-add runbook calls these two
functions positionally, and a recently-fixed runbook bug came from their
signatures changing underneath it. These tests pin the exact signatures and
assert the persistence side effects, so any future arg reshuffle breaks here
loudly instead of silently in the runbook.

Real signatures (scripts/database_supabase.py):
    save_vacancies(org_name: str, tier, jobs: list[dict]) -> int
    update_source_tracking(org_name, tier, strategy, new_count,
                           fetch_status=FETCH_STATUS_OK)

Fully offline on the local SQLite backend.
"""

import importlib
import inspect
import sys

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry",
                "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend
    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db
    yield db
    db.close_conn()


def _job(title, city="Berlin, Germany"):
    return {
        "title": title,
        "snippet": f"{title} blurb.",
        "full_description": f"We are hiring a {title}. " * 12,
        "location": city,
        "url": f"https://newco.example/job/{title.lower().replace(' ', '-')}",
    }


def _company_row(db, canonical):
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT vacancy_count, fetch_status, last_fetched FROM company "
        "WHERE canonical_name = %s",
        (canonical,),
    )
    row = cur.fetchone()
    cur.close()
    return row


# ---------------------------------------------------------------------------
# Signature-drift guard — the positional contract the runbook relies on.
# ---------------------------------------------------------------------------

def test_merge_vacancies_signature(dal):
    sig = inspect.signature(dal.save_vacancies)
    assert list(sig.parameters) == ["org_name", "tier", "jobs"]


def test_update_source_tracking_signature(dal):
    sig = inspect.signature(dal.update_source_tracking)
    params = list(sig.parameters)
    assert params[:4] == ["org_name", "tier", "strategy", "new_count"]
    # fetch_status is the optional 5th positional with a default.
    assert sig.parameters["fetch_status"].default == dal.FETCH_STATUS_OK


# ---------------------------------------------------------------------------
# save_vacancies inserts and auto-creates the company (add-source happy path).
# ---------------------------------------------------------------------------

def test_merge_vacancies_inserts_and_creates_company(dal):
    """Calling save_vacancies for a brand-new org (as /jobs-add does) creates
    the company and inserts its vacancies."""
    new = dal.save_vacancies("NewCo", "B", [_job("Engineer"), _job("Designer")])
    dal.get_conn().commit()
    assert new == 2

    vacs = dal.load_vacancies(include_candidate_companies=True,
                              include_inactive_companies=True)
    titles = {v["title"] for v in vacs.values()}
    assert titles == {"Engineer", "Designer"}
    assert all(v["org"] == "NewCo" for v in vacs.values())
    # Company row now exists.
    assert dal.resolve_company_id("NewCo") is not None


# ---------------------------------------------------------------------------
# update_source_tracking updates company metadata.
# ---------------------------------------------------------------------------

def test_update_source_tracking_records_counts(dal):
    dal.save_vacancies("NewCo", "B", [_job("Engineer")])
    dal.get_conn().commit()

    dal.update_source_tracking("NewCo", "B", "greenhouse", new_count=1)
    dal.get_conn().commit()

    vacancy_count, fetch_status, last_fetched = _company_row(dal, "NewCo")
    assert vacancy_count == 1
    # One real vacancy + ok status → stays 'ok'.
    assert fetch_status == dal.FETCH_STATUS_OK
    assert last_fetched is not None


def test_update_source_tracking_ok_with_zero_becomes_no_data(dal):
    """A successful fetch that yields zero vacancies is recorded as no_data —
    the source-health signal /jobs-add and /jobs-fetch surface."""
    dal.ensure_company("EmptyCo", status="active")
    dal.get_conn().commit()

    dal.update_source_tracking("EmptyCo", "B", "greenhouse", new_count=0,
                               fetch_status=dal.FETCH_STATUS_OK)
    dal.get_conn().commit()

    vacancy_count, fetch_status, _ = _company_row(dal, "EmptyCo")
    assert vacancy_count == 0
    assert fetch_status == dal.FETCH_STATUS_NO_DATA


def test_add_source_full_round_trip(dal):
    """End-to-end add-source: merge then track, asserting both persisted."""
    new = dal.save_vacancies("NewCo", "A", [_job("Head of Ops")])
    dal.get_conn().commit()
    dal.update_source_tracking("NewCo", "A", "lever", new_count=new)
    dal.get_conn().commit()

    vacs = dal.load_vacancies(include_candidate_companies=True,
                              include_inactive_companies=True)
    assert any(v["title"] == "Head of Ops" for v in vacs.values())
    vacancy_count, fetch_status, _ = _company_row(dal, "NewCo")
    assert vacancy_count == 1
    assert fetch_status == dal.FETCH_STATUS_OK
