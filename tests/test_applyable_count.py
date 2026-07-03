"""U6 — per-company "applyable roles" count (data_prep.prepare_company_data).

A company's applyable count = its open vacancies where ALL hold:
  * llm_score >= APPLYABLE_SCORE (60),
  * status NOT in (archived, passed, expiring),
  * deadline is null OR not in the past.

The donor-facing exclusion is deferred (the scorer emits no clean donor tag),
so these tests lock the score+status+deadline contract only.

Fully offline on the local SQLite backend (conftest clears Supabase env; the
fixture points JOBSEARCH_DB_PATH at a fresh temp file per test).
"""

import datetime
import importlib
import sys

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "report",
        "report.data_prep",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db

    yield db
    db.close_conn()


def _job(title):
    return {
        "title": title,
        "snippet": f"{title} blurb.",
        "full_description": f"We are hiring a {title}. " * 12,
        "location": "Berlin, Germany",
        "url": f"https://acme.example/job/{title.lower().replace(' ', '-')}",
    }


def _commit(db):
    db.get_conn().commit()


def _id_by_title(db, title):
    for vid, v in db.load_vacancies(include_inactive_companies=True).items():
        if v["title"] == title:
            return vid
    raise AssertionError(f"vacancy {title!r} not found")


def _applyable_for(name):
    """Run prepare_company_data and return the applyable_count for one company."""
    import report.data_prep as dp

    companies = dp.prepare_company_data()
    for c in companies:
        if c["name"] == name:
            return c["applyable_count"]
    raise AssertionError(f"company {name!r} not in company data")


def _today():
    """Production's notion of 'today' for the deadline check.

    ``data_prep._is_applyable_vacancy`` compares a deadline against
    ``datetime.now(DASHBOARD_TZ).date()`` (DASHBOARD_TZ defaults to UTC), NOT the
    runner's local date. Building fixture deadlines from ``date.today()`` instead
    makes "yesterday" collide with production's "today" whenever the runner's
    zone is ahead of DASHBOARD_TZ (e.g. TZ=+04 after 20:00 UTC) — the past
    deadline then reads as still-open and the role stays applyable. Reading the
    same clock production reads keeps these tests deterministic in every zone.
    """
    from config import DASHBOARD_TZ

    return datetime.datetime.now(DASHBOARD_TZ).date()


# ---------------------------------------------------------------------------
# 21 vacancies, 2 clear the bar → applyable_count == 2
# ---------------------------------------------------------------------------


def test_two_of_twentyone_are_applyable(dal):
    dal.ensure_company("Acme Robotics", status="active")
    jobs = [_job(f"Role {i:02d}") for i in range(21)]
    dal.save_vacancies("Acme Robotics", "A", jobs)
    _commit(dal)

    # 19 stay below the bar (low score); 2 clear it with no deadline.
    for i in range(21):
        vid = _id_by_title(dal, f"Role {i:02d}")
        score = 70 if i < 2 else 20
        dal.update_vacancy_fields(vid, llm_score=score)
    _commit(dal)

    assert _applyable_for("Acme Robotics") == 2


# ---------------------------------------------------------------------------
# The only >=60 roles are all expiring (past deadline) → applyable_count == 0.
#
# "expiring" is not a real vacancy status in the schema (CHECK constraint), so
# expiry manifests as a deadline in the past — which is exactly what
# disqualifies these roles here.
# ---------------------------------------------------------------------------


def test_expiring_high_scores_are_not_applyable(dal):
    dal.ensure_company("Expire Inc", status="active")
    dal.save_vacancies(
        "Expire Inc",
        "A",
        [
            _job("Hot Lead"),
            _job("Hot Manager"),
            _job("Cold Role"),
        ],
    )
    _commit(dal)

    yesterday = (_today() - datetime.timedelta(days=1)).isoformat()
    for title in ("Hot Lead", "Hot Manager"):
        vid = _id_by_title(dal, title)
        dal.update_vacancy_fields(vid, llm_score=80, deadline=yesterday)
    # A low-score role left unseen — also not applyable.
    dal.update_vacancy_fields(_id_by_title(dal, "Cold Role"), llm_score=10)
    _commit(dal)

    assert _applyable_for("Expire Inc") == 0


# ---------------------------------------------------------------------------
# Decided statuses (passed / archived) are excluded even at a top score.
# ---------------------------------------------------------------------------


def test_decided_statuses_are_not_applyable(dal):
    dal.ensure_company("Decided Co", status="active")
    dal.save_vacancies(
        "Decided Co",
        "A",
        [
            _job("Passed Role"),
            _job("Open Role"),
        ],
    )
    _commit(dal)

    dal.update_vacancy_fields(_id_by_title(dal, "Passed Role"), llm_score=95, status="passed")
    dal.update_vacancy_fields(_id_by_title(dal, "Open Role"), llm_score=95)
    _commit(dal)

    # Only the open role counts (passed is excluded; archived rows are dropped
    # from the active-company load entirely).
    assert _applyable_for("Decided Co") == 1


# ---------------------------------------------------------------------------
# Deadline in the past disqualifies; future deadline / no deadline count
# ---------------------------------------------------------------------------


def test_past_deadline_excluded_future_included(dal):
    dal.ensure_company("Deadline Co", status="active")
    dal.save_vacancies(
        "Deadline Co",
        "A",
        [
            _job("Past Role"),
            _job("Future Role"),
        ],
    )
    _commit(dal)

    yesterday = (_today() - datetime.timedelta(days=1)).isoformat()
    tomorrow = (_today() + datetime.timedelta(days=30)).isoformat()
    dal.update_vacancy_fields(_id_by_title(dal, "Past Role"), llm_score=90, deadline=yesterday)
    dal.update_vacancy_fields(_id_by_title(dal, "Future Role"), llm_score=90, deadline=tomorrow)
    _commit(dal)

    assert _applyable_for("Deadline Co") == 1
