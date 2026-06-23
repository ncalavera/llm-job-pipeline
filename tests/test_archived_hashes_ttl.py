"""Regression test: get_archived_hashes(ttl_days) TTL window.

Context
-------
A plan refactor initially suspected that the TTL window was broken because
``interval '%s days'`` looks like an improperly-parameterised SQL literal.
Empirically it works correctly on both backends:

* SQLite — the translator in db_backend._translate_sql converts
  ``now() - interval '%s days'`` → ``datetime('now', /*INTERVALDAYS*/)`` and
  then _SqliteCursor._prepare rewrites the bound int param into ``'-N days'``.
* psycopg2 — executes ``interval '%s days'`` with a plain ``%s`` substitution
  inside the string literal, which Postgres handles correctly.

This test is the regression guard: it proves that get_archived_hashes(ttl_days)
correctly excludes rows older than ttl_days and includes rows within the window.
The function is NOT modified.

Date format note
----------------
SQLite stores ``archived_at`` as TEXT with DEFAULT CURRENT_TIMESTAMP, which
emits ``'YYYY-MM-DD HH:MM:SS'`` (no 'T', no timezone). The TTL filter compares
``archived_at > datetime('now', '-N days')`` — a pure lexicographic comparison.
Python's ``datetime.isoformat()`` produces ``'YYYY-MM-DDTHH:MM:SS+HH:MM'``,
which is lexicographically incompatible. We therefore format seeded dates with
``strftime('%Y-%m-%d %H:%M:%S', ...)`` to match the SQLite format.
"""

import importlib
import sys
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Helpers — isolated temp SQLite DB (copied from test_e2e_pipeline.py)
# ---------------------------------------------------------------------------

def _force_sqlite(monkeypatch, db_file):
    """Point the whole chain at a fresh temp SQLite file and reload it."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry",
                "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend
    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "TTL test must run on the SQLite backend"
    import database_supabase as db
    return db


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    """DAL bound to an isolated temp SQLite database, seeded with two rows."""
    db = _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")

    now = datetime.now(tz=timezone.utc)
    # Format must match SQLite CURRENT_TIMESTAMP: 'YYYY-MM-DD HH:MM:SS'
    old_iso = (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")
    recent_iso = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason, archived_at) VALUES (%s, %s, %s)",
        ("h_old", "low_score", old_iso),
    )
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason, archived_at) VALUES (%s, %s, %s)",
        ("h_recent", "low_score", recent_iso),
    )
    conn.commit()
    cur.close()

    yield db
    db.close_conn()


# ---------------------------------------------------------------------------
# TTL assertions
# ---------------------------------------------------------------------------

def test_default_ttl_excludes_old_includes_recent(dal):
    """Default 90-day window: h_recent in, h_old out."""
    result = dal.get_archived_hashes()  # ttl_days=90 by default
    assert "h_recent" in result, "h_recent (10 days old) must be within 90-day TTL"
    assert "h_old" not in result, "h_old (100 days old) must be outside 90-day TTL"


def test_large_ttl_includes_both(dal):
    """Very large TTL (99999 days): both hashes returned."""
    result = dal.get_archived_hashes(99999)
    assert "h_recent" in result
    assert "h_old" in result


def test_small_ttl_excludes_both(dal):
    """Very small TTL (5 days): neither hash is within 5 days."""
    result = dal.get_archived_hashes(5)
    assert "h_recent" not in result, "h_recent (10 days old) must be outside 5-day TTL"
    assert "h_old" not in result, "h_old (100 days old) must be outside 5-day TTL"
