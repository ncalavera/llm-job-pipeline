"""Reconcile stale company.vacancy_count against the real row count (U8 / WS6).

Runs against a throwaway SQLite database (no Supabase, no psycopg2), mirroring
tests/test_sqlite_backend.py: force the SQLite backend, point JOBSEARCH_DB_PATH
at a temp file, reload the backend/registry/DAL chain, then import
audit_companies so its reconcile helper binds to the same temp connection.
"""

import importlib
import sys

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """(db, audit) both bound to the same isolated temp SQLite database."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    for mod in ("audit_companies", "database_supabase", "config",
                "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)

    import db_backend
    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"

    import database_supabase as db
    import audit_companies as audit
    yield db, audit
    db.close_conn()


def _job(title):
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
    cur.execute("UPDATE company SET vacancy_count = %s WHERE canonical_name = %s",
                (value, name))
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
    db.save_vacancies("Correct Co", "A", [_job("Only Role")])
    _set_stored_count(db, "Correct Co", 1)
    # Null Co: stored NULL, has 2 real rows → reconcile writes the real count.
    db.save_vacancies("Null Co", "B", [_job("Role One"), _job("Role Two")])
    db.get_conn().commit()


def test_stale_count_corrected_and_correct_untouched(env):
    db, audit = env
    _seed(db)

    drift = audit.reconcile_vacancy_counts()
    names = {d["name"] for d in drift}

    assert "Stale Co" in names           # 4 → 0
    assert "Null Co" in names            # NULL → 2
    assert "Correct Co" not in names     # 1 == 1, no write

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
