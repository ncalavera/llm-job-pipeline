"""Unit tests for database_supabase.load_vacancies() column narrowing contract.

These tests mock the psycopg2 connection and capture the SQL string passed to
cursor.execute(). They do not touch a live database.

Locks the invariant: load_vacancies(light=True) MUST NOT select full_description.
See docs/plans/2026-04-22-001-refactor-dal-egress-narrowing-plan.md for rationale.
"""

import inspect
from unittest.mock import MagicMock


def _mock_conn():
    """Build a MagicMock that looks like a psycopg2 connection.

    cursor.execute captures args; cursor.fetchall returns empty.
    """
    conn = MagicMock(name="conn")
    cur = MagicMock(name="cursor")
    cur.fetchall.return_value = []
    conn.cursor.return_value = cur
    return conn, cur


def _captured_sql(cur) -> str:
    """Extract the SQL string from the first cur.execute() call."""
    assert cur.execute.call_count >= 1, "cursor.execute was not called"
    args = cur.execute.call_args_list[0].args
    return args[0]


def test_load_vacancies_full_mode_includes_vacancy_columns(monkeypatch):
    """Default (light=False) should SELECT all vacancy columns — either v.* or an
    explicit list that INCLUDES full_description."""
    import database_supabase as db

    conn, cur = _mock_conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)

    db.load_vacancies()

    sql = _captured_sql(cur)
    # Either the classic v.* or an explicit list that still has full_description.
    assert "v.*" in sql or "full_description" in sql, (
        "Full-mode SQL must include full_description (via v.* or explicit column)."
        f"\nSQL was:\n{sql}"
    )


def test_load_vacancies_light_excludes_full_description(monkeypatch):
    """Invariant: light=True SQL must not reference full_description anywhere.

    This is the whole point of the kwarg — drop the 8 KB column from the wire.
    """
    import database_supabase as db

    conn, cur = _mock_conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)

    db.load_vacancies(light=True)

    sql = _captured_sql(cur)
    assert "full_description" not in sql, (
        f"light=True SQL must NOT select full_description. \nSQL was:\n{sql}"
    )
    # Must be an explicit list — bare v.* would pull everything.
    assert "v.*" not in sql, (
        f"light=True must use an explicit column list, not v.*.\nSQL was:\n{sql}"
    )


def test_load_vacancies_light_still_includes_locations_and_status(monkeypatch):
    """Light mode keeps locations (small JSONB) and status — over-narrowing would
    break dashboard stats and triage-adjacent callers."""
    import database_supabase as db

    conn, cur = _mock_conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)

    db.load_vacancies(light=True)

    sql = _captured_sql(cur)
    assert "v.locations" in sql, f"light=True must still SELECT v.locations.\nSQL was:\n{sql}"
    assert "v.status" in sql, f"light=True must still SELECT v.status.\nSQL was:\n{sql}"


def test_load_vacancies_status_exclude_filters_in_sql(monkeypatch):
    """status_exclude=['passed','skipped'] must produce a SQL clause that
    excludes those statuses — /score must not process auto-passed expired
    vacancies."""
    import database_supabase as db

    conn, cur = _mock_conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    # Prime the migration-0020 column cache so the presence probe (a PRAGMA /
    # information_schema query) doesn't land in the captured execute() calls.
    monkeypatch.setattr(db, "_scoring_excluded_supported_cache", True)

    db.load_vacancies(unscored_only=True, status_exclude=["passed", "skipped"])

    sql = _captured_sql(cur).lower()
    assert "v.status not in" in sql or "v.status != " in sql, (
        f"status_exclude must produce a NOT IN/!= clause; got:\n{sql}"
    )
    # Both excluded statuses must appear in the parameterised SQL.
    args = cur.execute.call_args_list[0].args
    params = args[1] if len(args) > 1 else []
    assert "passed" in params, f"'passed' missing from SQL params: {params}"
    assert "skipped" in params, f"'skipped' missing from SQL params: {params}"


def test_load_vacancies_status_exclude_omitted_no_clause(monkeypatch):
    """When status_exclude is not passed, no NOT IN clause leaks into SQL.

    unscored_only excludes archived rows — that single != clause is expected;
    any caller-supplied status_exclude (NOT IN) must not leak.
    """
    import database_supabase as db

    conn, cur = _mock_conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    # Prime the migration-0020 column cache (see the status_exclude test above).
    monkeypatch.setattr(db, "_scoring_excluded_supported_cache", True)

    db.load_vacancies(unscored_only=True)

    sql = _captured_sql(cur).lower()
    assert "v.status not in" not in sql
    assert "v.status != 'archived'" in sql


def test_load_vacancies_score_floor_ors_into_company_gate(monkeypatch):
    """score_floor_any_company=N must OR a `v.llm_score > N` clause onto the
    company-status gate, so a strong role is shown whatever its company status."""
    import database_supabase as db

    conn, cur = _mock_conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)

    db.load_vacancies(include_candidate_companies=True, score_floor_any_company=40)

    sql = _captured_sql(cur).lower()
    assert "or v.llm_score >" in sql, (
        f"score floor must OR a score clause onto the company gate; got:\n{sql}"
    )
    args = cur.execute.call_args_list[0].args
    params = args[1] if len(args) > 1 else []
    assert 40 in params, f"floor value 40 missing from SQL params: {params}"


def test_load_vacancies_score_floor_omitted_no_clause(monkeypatch):
    """Without the floor, the company gate stays a plain status check — no score
    OR leaks in (the default keeps today's behaviour)."""
    import database_supabase as db

    conn, cur = _mock_conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)

    db.load_vacancies(include_candidate_companies=True)

    sql = _captured_sql(cur).lower()
    assert "or v.llm_score >" not in sql


def test_load_vacancies_light_default_is_false():
    """Backward-compat guardrail: default value of light must be False.

    A future refactor that flips the default silently would break callers
    (score_vacancies.py, filter_vacancies.py) that rely on full_description.
    """
    import database_supabase as db

    sig = inspect.signature(db.load_vacancies)
    assert "light" in sig.parameters, "load_vacancies must accept a `light` kwarg"
    default = sig.parameters["light"].default
    assert default is False, f"load_vacancies(light=...) default must be False; got {default!r}"
