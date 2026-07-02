"""Tests for pass_expired_vacancies — auto-pass timestamp invariant.

Ensures pass_expired_vacancies() sets status='passed' AND updates
status_updated_at, making it possible to distinguish system auto-pass
from user-driven status changes.

These tests mock psycopg2 — no live DB required.
"""

from unittest.mock import MagicMock, patch


def _mock_conn():
    conn = MagicMock(name="conn")
    cur = MagicMock(name="cursor")
    cur.rowcount = 0
    conn.cursor.return_value = cur
    return conn, cur


def _captured_sql(cur) -> str:
    assert cur.execute.call_count >= 1, "cursor.execute was not called"
    return cur.execute.call_args_list[0].args[0]


def test_PE01_pass_expired_updates_status_updated_at():
    """The UPDATE must set status_updated_at = NOW() so we can distinguish
    system auto-pass from manual status changes via dashboard."""
    import database_supabase as db

    conn, cur = _mock_conn()
    with patch.object(db, "get_conn", return_value=conn):
        db.pass_expired_vacancies()
    sql = _captured_sql(cur)
    assert "status_updated_at" in sql.lower(), (
        f"pass_expired_vacancies SQL must update status_updated_at; got:\n{sql}"
    )
    assert "now()" in sql.lower(), (
        f"pass_expired_vacancies SQL must use NOW() for the timestamp; got:\n{sql}"
    )


def test_PE02_pass_expired_filters_by_deadline_and_status():
    """Sanity: keep the original WHERE clause intact — only mark unseen vacancies
    with a past deadline as passed."""
    import database_supabase as db

    conn, cur = _mock_conn()
    with patch.object(db, "get_conn", return_value=conn):
        db.pass_expired_vacancies()
    sql = _captured_sql(cur).lower()
    assert "deadline is not null" in sql
    assert "deadline < current_date" in sql
    assert "status = 'unseen'" in sql
