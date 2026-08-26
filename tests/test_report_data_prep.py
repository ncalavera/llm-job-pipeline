"""Report data-prep coverage: the dashboard's unscored-count hint, its
consistency with the DB scoring load gate, decision-SLA latency metrics, and
the Companies payload's raw-rollup-free shape.

Absorbed: test_unscored_count.py, test_unscored_definition_consistency.py,
test_latency_metrics.py, test_company_rollups_derived.py.
"""

import importlib
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from report.data_prep import _count_unscored  # noqa: E402


# ---------------------------------------------------------------------------
# --- from test_unscored_count.py ---
#
# The dashboard's 'N fetched, none scored yet' hint must count only vacancies
# genuinely awaiting scoring — not already-triaged rows that happen to lack a
# score. Regression for the inflated-count bug.
# ---------------------------------------------------------------------------


def _vac(status="unseen", score=None):
    return {"status": status, "llm_score": score}


def test_counts_unseen_unscored():
    vacs = {"a": _vac("unseen", None), "b": _vac("unseen", None)}
    assert _count_unscored(vacs) == 2


def test_excludes_scored():
    vacs = {"a": _vac("unseen", 80), "b": _vac("unseen", 0)}
    assert _count_unscored(vacs) == 0


def test_excludes_triaged_without_score():
    """A passed/skipped/liked vacancy with no score is NOT awaiting scoring."""
    vacs = {
        "passed": _vac("passed", None),
        "skipped": _vac("skipped", None),
        "liked": _vac("liked", None),
        "fresh": _vac("unseen", None),
    }
    assert _count_unscored(vacs) == 1  # only the fresh unseen one


def test_missing_status_treated_as_unseen():
    vacs = {"a": {"llm_score": None}}  # no status key
    assert _count_unscored(vacs) == 1


def test_empty():
    assert _count_unscored({}) == 0


# ---------------------------------------------------------------------------
# --- from test_unscored_definition_consistency.py ---
#
# 'Unscored' must mean the same thing in the dashboard count and the scoring
# load gate.
#
# report.data_prep._count_unscored counts a vacancy as awaiting scoring when
# its llm_score IS NULL *or* is negative (a failure sentinel). The DB load
# gate (database_supabase.load_vacancies(unscored_only=True)) used to select
# only IS NULL, so a -1 row was counted-but-never-offered — stranded forever.
# These tests pin that both sides now agree on the same predicate, on a real
# SQLite round-trip.
#
# NOTE: this is NOT redundant with test_unscored_count.py above — that file
# proves the Python predicate in isolation; this one proves the SQL WHERE
# clause agrees with it (the historical -1 sentinel bug). Both are kept.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# --- from test_latency_metrics.py ---
#
# U11 — decision-SLA health metrics (stuck + weekly leakage).
#
# compute_latency_metrics() flags high-fit roles that haven't moved within
# SLA_DAYS and counts high-fit roles that leaked to archived/passed in the
# last SLA-week. Offline on the local SQLite backend.
# ---------------------------------------------------------------------------


@pytest.fixture()
def dal_latency(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "report.data_prep",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    import database_supabase as db

    yield db
    db.close_conn()


def _job_latency(title):
    return {
        "title": title,
        "snippet": f"{title} blurb.",
        "full_description": f"We are hiring a {title}. " * 12,
        "location": "Berlin, Germany",
        "url": f"https://acme.example/{title.lower().replace(' ', '-')}",
    }


def _set(db, title, **cols):
    rows = db.load_vacancies(include_inactive_companies=True)
    vid = next(v_id for v_id, v in rows.items() if v["title"] == title)
    cur = db.get_conn().cursor()
    sets = ", ".join(f"{k} = %s" for k in cols)
    cur.execute(f"UPDATE vacancy SET {sets} WHERE id = %s", list(cols.values()) + [vid])
    cur.close()
    db.get_conn().commit()


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def test_stuck_and_leakage(dal_latency):
    import report.data_prep as dp

    dal_latency.ensure_company("Acme Robotics", status="active")
    dal_latency.save_vacancies(
        "Acme Robotics",
        "A",
        [
            _job_latency("Stuck Liked"),
            _job_latency("Fresh Liked"),
            _job_latency("Never Touched"),
            _job_latency("Leaked Role"),
            _job_latency("Low Stuck"),
        ],
    )
    dal_latency.get_conn().commit()

    # 65, liked, untouched 8 days → STUCK
    _set(dal_latency, "Stuck Liked", llm_score=65, status="liked", status_updated_at=_days_ago(8))
    # 65, liked, touched 2 days ago → not stuck
    _set(dal_latency, "Fresh Liked", llm_score=65, status="liked", status_updated_at=_days_ago(2))
    # 80, unseen, never status-touched but first seen 10 days ago → STUCK (fallback)
    _set(
        dal_latency,
        "Never Touched",
        llm_score=80,
        status="unseen",
        status_updated_at=None,
        first_seen=_days_ago(10),
    )
    # 70, archived 3 days ago → leakage
    _set(
        dal_latency, "Leaked Role", llm_score=70, status="archived", status_updated_at=_days_ago(3)
    )
    # 30 (below SLA_SCORE), liked, untouched 9 days → NOT stuck (too low)
    _set(dal_latency, "Low Stuck", llm_score=30, status="liked", status_updated_at=_days_ago(9))

    m = dp.compute_latency_metrics(dal_latency.get_conn())
    stuck_titles = {s["title"] for s in m["stuck"]}
    assert stuck_titles == {"Stuck Liked", "Never Touched"}
    assert m["stuck_count"] == 2
    assert m["leakage_count"] == 1
    assert m["sla_score"] == 60 and m["sla_days"] == 7
    # Stuck list is sorted by age, longest-waiting first.
    assert m["stuck"][0]["title"] == "Never Touched"


# ---------------------------------------------------------------------------
# --- from test_company_rollups_derived.py ---
#
# KISS derivation, phase 2 — the Companies payload ships raw rows, not
# rollups.
#
# Every per-company NUMBER (vacancy_count, applyable_count, the average LLM
# score, the hot-vacancy signal, the region breakdown) is derived in the
# browser now from the company's roles (public/modules/derive.js
# ``companyRollup``). The Python payload must therefore carry ONLY the raw
# ``vacancy_ids`` join key — none of the aggregates may ride along (STRATEGY
# guardrail 9, the deferred exception now closed). The unit tests for the
# derivation math itself live in ``public/modules/derive.test.js``.
#
# Fully offline on the local SQLite backend (conftest clears Supabase env;
# the fixture points JOBSEARCH_DB_PATH at a fresh temp file per test).
# ---------------------------------------------------------------------------


@pytest.fixture()
def dal_rollups(tmp_path, monkeypatch):
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


def _job_rollups(title):
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


def test_company_row_ships_raw_role_ids_not_baked_rollups(dal_rollups):
    """The raw ``vacancy_ids`` join key ships; every baked rollup is gone."""
    dal_rollups.ensure_company("Acme Robotics", status="active")
    jobs = [_job_rollups(f"Role {i:02d}") for i in range(5)]
    dal_rollups.save_vacancies("Acme Robotics", "A", jobs)
    _commit(dal_rollups)
    for i in range(5):
        vid = _id_by_title(dal_rollups, f"Role {i:02d}")
        dal_rollups.update_vacancy_fields(vid, llm_score=70 if i < 2 else 20)
    _commit(dal_rollups)

    c = _company_row("Acme Robotics")
    # The one raw fact the browser can't reconstruct still ships, in full...
    assert len(c["vacancy_ids"]) == 5
    # ...and not one browser-derivable aggregate creeps back in.
    for key in _BAKED_ROLLUP_KEYS:
        assert key not in c, f"{key} must be derived in the browser, not baked"


def test_zero_vacancy_company_still_ships_and_stays_rollup_free(dal_rollups):
    """A configured company with no vacancies keeps an empty id list and no
    rollups — the browser derives all-zeros from the empty role set."""
    dal_rollups.ensure_company("Empty Co", status="active")
    _commit(dal_rollups)

    c = _company_row("Empty Co")
    assert c["vacancy_ids"] == []
    for key in _BAKED_ROLLUP_KEYS:
        assert key not in c
