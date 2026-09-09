"""Offline audit helpers: the weekly low-score audit (U12) and the stale
company.vacancy_count reconciler (U8 / WS6). Both prepare/verify data without
ever mutating live state through anything but their own explicit write path.

Absorbed tests/test_audit_low_scores.py and
tests/test_audit_vacancy_count_reconcile.py.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


# ===========================================================================
# --- from test_audit_low_scores.py ---
#
# U12 — weekly low-score audit.
#
# The audit prepares payloads for a sample of recently buried (<40) undecided
# roles, then renders a markdown report of suspected false negatives from the
# subagent verdicts. It never mutates the DB.
# ===========================================================================


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
    p = audit.build_audit_payload(rows[0])
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


# ===========================================================================
# --- from test_audit_vacancy_count_reconcile.py ---
#
# Reconcile stale company.vacancy_count against the real row count (U8 / WS6).
#
# Runs against a throwaway SQLite database (no Supabase, no psycopg2),
# mirroring tests/test_sqlite_backend.py: force the SQLite backend, point
# JOBSEARCH_DB_PATH at a temp file, reload the backend/registry/DAL chain,
# then import audit_companies so its reconcile helper binds to the same temp
# connection.
#
# Its ``env`` fixture is NOT the same as ``dal`` above (it also reloads and
# yields ``audit_companies``, and pops a different module list), so both
# fixtures are kept.
# ===========================================================================


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """(db, audit) both bound to the same isolated temp SQLite database."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    for mod in (
        "audit_companies",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"

    import database_supabase as db
    import audit_companies as audit

    yield db, audit
    db.close_conn()


def _recon_job(title):
    return {
        "title": title,
        "snippet": "A role.",
        "full_description": "Do the work. " * 8,
        "location": "Berlin, Germany",
        "url": "https://example.org/job/" + title.lower().replace(" ", "-"),
    }


def _set_stored_count(db, name, value):
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE company SET vacancy_count = %s WHERE canonical_name = %s", (value, name))
    conn.commit()
    cur.close()


def _stored_count(db, name):
    cur = db.get_conn().cursor()
    cur.execute("SELECT vacancy_count FROM company WHERE canonical_name = %s", (name,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _seed(db):
    # Stale Co: stored says 4, has 0 real rows (the Omidyar-type lie).
    db.ensure_company("Stale Co", status="active")
    _set_stored_count(db, "Stale Co", 4)
    # Correct Co: stored 1, has exactly 1 real row → must be left untouched.
    db.save_vacancies("Correct Co", "A", [_recon_job("Only Role")])
    _set_stored_count(db, "Correct Co", 1)
    # Null Co: stored NULL, has 2 real rows → reconcile writes the real count.
    db.save_vacancies("Null Co", "B", [_recon_job("Role One"), _recon_job("Role Two")])
    db.get_conn().commit()


def test_stale_count_corrected_and_correct_untouched(env):
    db, audit = env
    _seed(db)

    drift = audit.reconcile_vacancy_counts()
    names = {d["name"] for d in drift}

    assert "Stale Co" in names  # 4 → 0
    assert "Null Co" in names  # NULL → 2
    assert "Correct Co" not in names  # 1 == 1, no write

    stale = next(d for d in drift if d["name"] == "Stale Co")
    assert stale["stored"] == 4 and stale["real"] == 0

    assert _stored_count(db, "Stale Co") == 0
    assert _stored_count(db, "Null Co") == 2
    assert _stored_count(db, "Correct Co") == 1


def test_reconcile_is_idempotent(env):
    db, audit = env
    _seed(db)

    first = audit.reconcile_vacancy_counts()
    assert first, "first pass should correct at least one company"

    second = audit.reconcile_vacancy_counts()
    assert second == [], "second pass must be a no-op (idempotent)"


def test_dry_run_reports_without_writing(env):
    db, audit = env
    _seed(db)

    drift = audit.reconcile_vacancy_counts(dry_run=True)
    assert {d["name"] for d in drift} >= {"Stale Co", "Null Co"}
    # Nothing was written — the stale value is still on disk.
    assert _stored_count(db, "Stale Co") == 4
    assert _stored_count(db, "Null Co") is None
