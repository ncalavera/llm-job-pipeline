"""U11 — decision-SLA health metrics (stuck + weekly leakage).

compute_latency_metrics() flags high-fit roles that haven't moved within
SLA_DAYS and counts high-fit roles that leaked to archived/passed in the last
SLA-week. Offline on the local SQLite backend.
"""

import importlib
import sys
from datetime import date, timedelta

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry",
                "db_conn", "db_backend", "report.data_prep"):
        sys.modules.pop(mod, None)
    import db_backend
    importlib.reload(db_backend)
    import database_supabase as db
    yield db
    db.close_conn()


def _job(title):
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
    cur.execute(f"UPDATE vacancy SET {sets} WHERE id = %s",
                list(cols.values()) + [vid])
    cur.close()
    db.get_conn().commit()


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def test_stuck_and_leakage(dal):
    import report.data_prep as dp
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [
        _job("Stuck Liked"), _job("Fresh Liked"),
        _job("Never Touched"), _job("Leaked Role"), _job("Low Stuck"),
    ])
    dal.get_conn().commit()

    # 65, liked, untouched 8 days → STUCK
    _set(dal, "Stuck Liked", llm_score=65, status="liked",
         status_updated_at=_days_ago(8))
    # 65, liked, touched 2 days ago → not stuck
    _set(dal, "Fresh Liked", llm_score=65, status="liked",
         status_updated_at=_days_ago(2))
    # 80, unseen, never status-touched but first seen 10 days ago → STUCK (fallback)
    _set(dal, "Never Touched", llm_score=80, status="unseen",
         status_updated_at=None, first_seen=_days_ago(10))
    # 70, archived 3 days ago → leakage
    _set(dal, "Leaked Role", llm_score=70, status="archived",
         status_updated_at=_days_ago(3))
    # 30 (below SLA_SCORE), liked, untouched 9 days → NOT stuck (too low)
    _set(dal, "Low Stuck", llm_score=30, status="liked",
         status_updated_at=_days_ago(9))

    m = dp.compute_latency_metrics(dal.get_conn())
    stuck_titles = {s["title"] for s in m["stuck"]}
    assert stuck_titles == {"Stuck Liked", "Never Touched"}
    assert m["stuck_count"] == 2
    assert m["leakage_count"] == 1
    assert m["sla_score"] == 60 and m["sla_days"] == 7
    # Stuck list is sorted by age, longest-waiting first.
    assert m["stuck"][0]["title"] == "Never Touched"
