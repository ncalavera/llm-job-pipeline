"""U12 — weekly low-score audit.

The audit prepares payloads for a sample of recently buried (<40) undecided
roles, then renders a markdown report of suspected false negatives from the
subagent verdicts. It never mutates the DB.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
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
    cur.execute(f"UPDATE vacancy SET {sets} WHERE id = %s", list(cols.values()) + [vid])
    cur.close()
    db.get_conn().commit()


def test_select_samples_only_low_undecided(dal):
    import audit_low_scores as audit

    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies(
        "Acme Robotics",
        "A",
        [
            _job("Buried Low"),
            _job("Strong Fit"),
            _job("Low But Liked"),
        ],
    )
    dal.get_conn().commit()
    _set(dal, "Buried Low", llm_score=22)
    _set(dal, "Strong Fit", llm_score=80)
    _set(dal, "Low But Liked", llm_score=18, status="liked")

    rows, total = audit.select_low_scored(dal.get_conn(), 20)
    titles = {r["title"] for r in rows}
    assert titles == {"Buried Low"}  # high-score and decided are excluded
    assert total == 1


def test_build_payload_carries_audit_framing(dal):
    import audit_low_scores as audit

    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Buried Low")])
    dal.get_conn().commit()
    _set(dal, "Buried Low", llm_score=22)
    rows, _ = audit.select_low_scored(dal.get_conn(), 20)
    p = audit.build_payload = audit.build_audit_payload(rows[0])
    assert p["payload_kind"] == "audit"
    assert "22/100" in p["user_msg"]
    assert "wrongly_buried" in p["user_msg"]
    assert "auditor" in p["system_prompt"].lower()


def test_report_flags_misses_with_reason():
    import audit_low_scores as audit

    verdicts = [
        {
            "org": "GiveWell",
            "title": "Senior Researcher",
            "old_score": 32,
            "wrongly_buried": True,
            "suggested_score": 71,
            "reason": "Strong programme fit the scorer missed.",
        },
        {
            "org": "Acme",
            "title": "Junior Clerk",
            "old_score": 12,
            "wrongly_buried": False,
            "suggested_score": 12,
            "reason": "Correctly low.",
        },
    ]
    report = audit.render_report(verdicts, sampled=2, total=40)
    assert "Sampled **2** of **40**" in report  # honest sampling
    assert "wrongly buried: **1**" in report.lower()
    assert "GiveWell — Senior Researcher" in report
    assert "Strong programme fit" in report
    assert "Junior Clerk" not in report  # correct lows are not listed


def test_report_empty_when_no_misses():
    import audit_low_scores as audit

    report = audit.render_report(
        [{"org": "A", "title": "X", "old_score": 10, "wrongly_buried": False}],
        sampled=1,
        total=10,
    )
    assert "No suspected misses" in report
