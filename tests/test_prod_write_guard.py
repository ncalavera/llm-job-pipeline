"""Tests for the prod-write guard in db_backend (DHA-421).

An ad-hoc script run with SUPABASE_DB_URL set in the environment (a stray
export, a debug one-off with no pytest fixture to clean up after it) used to
be able to INSERT/UPDATE/DELETE straight into the live Supabase DB. These
tests lock the guard's contract directly against the pure helper functions
(``_is_write_statement`` / ``_running_as_pipeline_script`` /
``_prod_write_context_ok`` / ``_check_write_allowed``) and the cursor/conn
wrappers (``_GuardedCursor`` / ``_GuardedConn``) — no live Postgres needed,
per the ticket's constraint. A fake cursor/connection stands in for psycopg2.

Because pytest itself sets ``PYTEST_CURRENT_TEST`` for the duration of every
test, the "unrecognized context" scenarios must explicitly delete it to
simulate what running OUTSIDE pytest looks like. Pytest re-sets that variable
at every setup/call/teardown phase transition, so the deletion must happen
INSIDE the test body (the "call" phase) via a plain helper function rather
than a fixture (fixtures run during "setup", one phase transition too early —
pytest overwrites the variable again right before "call" begins).
"""

import os

import pytest

import db_backend


@pytest.fixture(autouse=True)
def _restore_env():
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


def _not_pytest(monkeypatch):
    """Simulate "not running under pytest" for the context check: pytest sets
    PYTEST_CURRENT_TEST for the duration of every test, which would otherwise
    make every scenario below look like the trusted pytest context. Must be
    called from inside the test body — see the module docstring."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


def _as_ad_hoc_script(monkeypatch, tmp_path):
    """Simulate an unrecognized ad-hoc script: not pytest, argv[0] outside
    scripts/, Postgres backend (IS_SQLITE False), no override env var. Must be
    called from inside the test body — see the module docstring."""
    _not_pytest(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr("sys.argv", [str(tmp_path / "scratch_debug.py")])
    monkeypatch.delenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, raising=False)


# ---------------------------------------------------------------------------
# _is_write_statement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO company (canonical_name) VALUES (%s)",
        "  \n  UPDATE vacancy SET status = %s WHERE id = %s",
        "DELETE FROM vacancy WHERE id = ANY(%s::uuid[])",
        "insert into company (canonical_name) values (%s)",
    ],
)
def test_is_write_statement_true_for_dml(sql):
    assert db_backend._is_write_statement(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM vacancy WHERE status = %s",
        "",
        "WITH x AS (SELECT 1) SELECT * FROM x",  # not INSERT/UPDATE/DELETE-led
    ],
)
def test_is_write_statement_false_for_reads(sql):
    assert db_backend._is_write_statement(sql) is False


# ---------------------------------------------------------------------------
# _prod_write_context_ok / _check_write_allowed
# ---------------------------------------------------------------------------


def test_sqlite_backend_always_allowed(monkeypatch, tmp_path):
    """The guard is Postgres-only — SQLite (never prod) is always allowed,
    regardless of argv[0] or the override env var."""
    _not_pytest(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", True)
    monkeypatch.setattr("sys.argv", [str(tmp_path / "scratch_debug.py")])
    monkeypatch.delenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, raising=False)

    assert db_backend._prod_write_context_ok() is True
    db_backend._check_write_allowed("INSERT INTO company (canonical_name) VALUES (%s)")


def test_unrecognized_ad_hoc_script_is_blocked(monkeypatch, tmp_path):
    """The core acceptance case: an unrecognized script, Postgres backend, no
    override — INSERT/UPDATE/DELETE must raise before touching a connection."""
    _as_ad_hoc_script(monkeypatch, tmp_path)

    assert db_backend._prod_write_context_ok() is False

    with pytest.raises(db_backend.ProdWriteBlocked, match="JOBSEARCH_ALLOW_PROD_WRITE"):
        db_backend._check_write_allowed("INSERT INTO company (canonical_name) VALUES (%s)")

    with pytest.raises(db_backend.ProdWriteBlocked):
        db_backend._check_write_allowed("UPDATE vacancy SET status = %s WHERE id = %s")

    with pytest.raises(db_backend.ProdWriteBlocked):
        db_backend._check_write_allowed("DELETE FROM vacancy WHERE id = %s")


def test_reads_unaffected_even_when_blocked_context(monkeypatch, tmp_path):
    """SELECT never raises, even in an otherwise-blocked context."""
    _as_ad_hoc_script(monkeypatch, tmp_path)

    db_backend._check_write_allowed("SELECT 1")
    db_backend._check_write_allowed("SELECT * FROM vacancy WHERE status = %s")


def test_explicit_override_env_var_allows_write(monkeypatch, tmp_path):
    _as_ad_hoc_script(monkeypatch, tmp_path)
    monkeypatch.setenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, "1")

    assert db_backend._prod_write_context_ok() is True
    db_backend._check_write_allowed("INSERT INTO company (canonical_name) VALUES (%s)")


def test_override_env_var_wrong_value_still_blocks(monkeypatch, tmp_path):
    _as_ad_hoc_script(monkeypatch, tmp_path)
    monkeypatch.setenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, "true")  # not "1"

    assert db_backend._prod_write_context_ok() is False
    with pytest.raises(db_backend.ProdWriteBlocked):
        db_backend._check_write_allowed("INSERT INTO company (canonical_name) VALUES (%s)")


def test_pytest_context_allows_write(monkeypatch, tmp_path):
    """PYTEST_CURRENT_TEST — set by pytest itself for every running test — is
    trusted, matching tests/parity/'s real-local-Postgres suite."""
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr("sys.argv", [str(tmp_path / "scratch_debug.py")])
    monkeypatch.delenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_prod_write_guard.py::fake (call)")

    assert db_backend._prod_write_context_ok() is True
    db_backend._check_write_allowed("INSERT INTO company (canonical_name) VALUES (%s)")


def test_pipeline_entrypoint_context_allows_write(monkeypatch):
    """run_daily.py (and every scripts/*.py stage it subprocesses) is
    identified by argv[0] resolving inside this repo's scripts/ directory."""
    _not_pytest(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.delenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, raising=False)
    scripts_dir = db_backend.PROJECT_ROOT / "scripts"
    monkeypatch.setattr("sys.argv", [str(scripts_dir / "run_daily.py")])

    assert db_backend._running_as_pipeline_script() is True
    assert db_backend._prod_write_context_ok() is True
    db_backend._check_write_allowed("UPDATE vacancy SET llm_score = NULL WHERE id = ANY(%s::uuid[])")


def test_running_as_pipeline_script_false_for_empty_argv0(monkeypatch):
    monkeypatch.setattr("sys.argv", [""])
    assert db_backend._running_as_pipeline_script() is False


# ---------------------------------------------------------------------------
# _GuardedCursor / _GuardedConn — the wrapping layer used by get_conn()
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Stands in for a psycopg2 cursor: records calls, never touches a real DB."""

    def __init__(self):
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, seq_of_params):
        self.executed.append((sql, list(seq_of_params)))

    def fetchone(self):
        return ("fake-row",)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _FakeConn:
    def __init__(self):
        self.cursors = []
        self.closed = 0
        self.committed = False

    def cursor(self, cursor_factory=None):
        cur = _FakeCursor()
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = 1


def test_guarded_cursor_blocks_write_before_touching_real_cursor(monkeypatch, tmp_path):
    _as_ad_hoc_script(monkeypatch, tmp_path)
    fake = _FakeCursor()
    guarded = db_backend._GuardedCursor(fake)

    with pytest.raises(db_backend.ProdWriteBlocked):
        guarded.execute("INSERT INTO company (canonical_name) VALUES (%s)", ("Acme",))

    # The raise happened BEFORE the real cursor's execute ran — no write reached
    # the (fake, standing in for a real) connection at all.
    assert fake.executed == []


def test_guarded_cursor_passes_reads_straight_through(monkeypatch, tmp_path):
    _as_ad_hoc_script(monkeypatch, tmp_path)
    fake = _FakeCursor()
    guarded = db_backend._GuardedCursor(fake)

    guarded.execute("SELECT 1")
    assert fake.executed == [("SELECT 1", None)]
    assert guarded.fetchone() == ("fake-row",)  # __getattr__ passthrough
    guarded.close()
    assert fake.closed is True


def test_guarded_cursor_allows_write_in_trusted_context(monkeypatch):
    _not_pytest(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    scripts_dir = db_backend.PROJECT_ROOT / "scripts"
    monkeypatch.setattr("sys.argv", [str(scripts_dir / "run_daily.py")])

    fake = _FakeCursor()
    guarded = db_backend._GuardedCursor(fake)
    guarded.execute("INSERT INTO company (canonical_name) VALUES (%s)", ("Acme",))

    assert fake.executed == [("INSERT INTO company (canonical_name) VALUES (%s)", ("Acme",))]


def test_guarded_conn_wraps_cursor_and_passes_through_commit_close():
    fake_conn = _FakeConn()
    guarded = db_backend._GuardedConn(fake_conn)

    cur = guarded.cursor()
    assert isinstance(cur, db_backend._GuardedCursor)

    guarded.commit()
    assert fake_conn.committed is True

    assert guarded.closed == 0  # __getattr__ passthrough
    guarded.close()
    assert fake_conn.closed == 1
