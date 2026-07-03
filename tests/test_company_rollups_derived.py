"""KISS derivation, phase 2 — the Companies payload ships raw rows, not rollups.

Every per-company NUMBER (vacancy_count, applyable_count, the average LLM score,
the hot-vacancy signal, the region breakdown) is derived in the browser now from
the company's roles (public/modules/derive.js ``companyRollup``). The Python
payload must therefore carry ONLY the raw ``vacancy_ids`` join key — none of the
aggregates may ride along (STRATEGY guardrail 9, the deferred exception now
closed). The unit tests for the derivation math itself live in
``public/modules/derive.test.js``.

Fully offline on the local SQLite backend (conftest clears Supabase env; the
fixture points JOBSEARCH_DB_PATH at a fresh temp file per test).
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


def _company_row(name):
    import report.data_prep as dp

    for c in dp.prepare_company_data():
        if c["name"] == name:
            return c
    raise AssertionError(f"company {name!r} not in company data")


# Every per-company aggregate the pipeline used to bake — none may survive.
_BAKED_ROLLUP_KEYS = (
    "vacancy_count",
    "applyable_count",
    "scored_count",
    "avg_llm_score",
    "max_llm_score",
    "region_breakdown",
    "has_compensation",
    "hot_vacancy",
    "first_seen",
    "last_seen",
)


def test_company_row_ships_raw_role_ids_not_baked_rollups(dal):
    """The raw ``vacancy_ids`` join key ships; every baked rollup is gone."""
    dal.ensure_company("Acme Robotics", status="active")
    jobs = [_job(f"Role {i:02d}") for i in range(5)]
    dal.save_vacancies("Acme Robotics", "A", jobs)
    _commit(dal)
    for i in range(5):
        vid = _id_by_title(dal, f"Role {i:02d}")
        dal.update_vacancy_fields(vid, llm_score=70 if i < 2 else 20)
    _commit(dal)

    c = _company_row("Acme Robotics")
    # The one raw fact the browser can't reconstruct still ships, in full...
    assert len(c["vacancy_ids"]) == 5
    # ...and not one browser-derivable aggregate creeps back in.
    for key in _BAKED_ROLLUP_KEYS:
        assert key not in c, f"{key} must be derived in the browser, not baked"


def test_zero_vacancy_company_still_ships_and_stays_rollup_free(dal):
    """A configured company with no vacancies keeps an empty id list and no
    rollups — the browser derives all-zeros from the empty role set."""
    dal.ensure_company("Empty Co", status="active")
    _commit(dal)

    c = _company_row("Empty Co")
    assert c["vacancy_ids"] == []
    for key in _BAKED_ROLLUP_KEYS:
        assert key not in c
