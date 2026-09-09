"""Tests for scripts/db_backend.py: .env loading and precedence, the
production-write guard, and the easy-install path that must never import
psycopg2.

Absorbed from tests/test_env_loader.py, tests/test_prod_write_guard.py,
tests/test_simple_mode_no_psycopg2.py. Covers: the stdlib .env parser and
loader (auto-load, shell-var precedence, disable flag, a fresh-interpreter
first-import check), the backend-mismatch banner and host label it prints,
the guard that blocks an unrecognized ad-hoc script from writing to prod
Postgres (pure helpers + the _GuardedCursor/_GuardedConn wrappers), and a
simple-mode smoke test proving every DB-touching entry script runs on SQLite
with psycopg2 rendered un-importable.
"""

import importlib
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import db_backend

SCRIPTS_DIR = Path(db_backend.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _restore_env():
    """load_dotenv mutates os.environ via setdefault (not through monkeypatch),
    so snapshot and restore the whole environment around every test to keep the
    rest of the suite deterministic.
    """
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def _reenable_dotenv(_restore_env):
    """conftest sets LLM_PIPELINE_DISABLE_DOTENV=1 suite-wide (so the import-time
    load can't re-inject the maintainer's real .env). The tests moved from
    tests/test_env_loader.py exercise the loader on purpose — against tmp paths
    only — so re-enable it locally for exactly those tests; ``_restore_env``
    above puts the flag back for the rest of the suite afterwards.
    """
    os.environ.pop("LLM_PIPELINE_DISABLE_DOTENV", None)


# --- from test_env_loader.py ---
#
# Tests for the stdlib .env loader and backend-mismatch banner in db_backend.
#
# These lock the auto-load contract: a filled repo-root ``.env`` is picked up
# automatically (no manual ``export``), an already-exported shell var wins over
# the file, an absent/empty ``.env`` stays on SQLite without error, a file-vs-
# runtime backend mismatch is flagged loudly, and a missing psycopg2 driver in
# full mode gives an actionable message instead of a raw ModuleNotFoundError.

# --- parsing ---------------------------------------------------------------


@pytest.mark.usefixtures("_reenable_dotenv")
def test_parse_dotenv_handles_comments_quotes_export_and_urls(tmp_path):
    (tmp_path / ".env").write_text(
        "\n"
        "# a comment\n"
        "export SUPABASE_DB_URL='postgresql://u:p@h:5432/db?sslmode=require'\n"
        'AUTH_PASS="se=cr#et"\n'
        "  SPACED = value \n"
        "NO_EQUALS_LINE\n"
        "EMPTY=\n"
    )
    values = db_backend._parse_dotenv(tmp_path / ".env")

    # verbatim value after first '=' — colons/@/slashes/query survive
    assert values["SUPABASE_DB_URL"] == "postgresql://u:p@h:5432/db?sslmode=require"
    # surrounding quotes stripped, an inner '=' and '#' are preserved
    assert values["AUTH_PASS"] == "se=cr#et"
    assert values["SPACED"] == "value"
    assert values["EMPTY"] == ""
    assert "NO_EQUALS_LINE" not in values


@pytest.mark.usefixtures("_reenable_dotenv")
def test_parse_dotenv_missing_file_returns_empty(tmp_path):
    assert db_backend._parse_dotenv(tmp_path / "nope.env") == {}


# --- load into os.environ --------------------------------------------------


@pytest.mark.usefixtures("_reenable_dotenv")
def test_load_dotenv_from_root_populates_environ(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SUPABASE_DB_URL=postgresql://real/db\n")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)

    declared = db_backend.load_dotenv(tmp_path)

    assert declared["SUPABASE_DB_URL"] == "postgresql://real/db"
    assert os.environ["SUPABASE_DB_URL"] == "postgresql://real/db"
    # backend selection would now resolve to Postgres
    assert db_backend._supabase_url() == "postgresql://real/db"


@pytest.mark.usefixtures("_reenable_dotenv")
def test_existing_env_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://preset/db")
    (tmp_path / ".env").write_text("SUPABASE_DB_URL=postgresql://fromfile/db\n")

    declared = db_backend.load_dotenv(tmp_path)

    # the file still reports what it declared...
    assert declared["SUPABASE_DB_URL"] == "postgresql://fromfile/db"
    # ...but the already-exported shell var keeps priority
    assert os.environ["SUPABASE_DB_URL"] == "postgresql://preset/db"


@pytest.mark.usefixtures("_reenable_dotenv")
def test_absent_dotenv_is_silent_and_stays_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)

    assert db_backend.load_dotenv(tmp_path) == {}  # empty dir, no .env
    assert db_backend._supabase_url() is None  # -> IS_SQLITE would be True


@pytest.mark.usefixtures("_reenable_dotenv")
def test_empty_dotenv_is_silent(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("\n# only comments here\n\n")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)

    assert db_backend.load_dotenv(tmp_path) == {}


@pytest.mark.usefixtures("_reenable_dotenv")
def test_disable_flag_makes_load_a_noop(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SUPABASE_DB_URL=postgresql://real/db\n")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("LLM_PIPELINE_DISABLE_DOTENV", "1")

    assert db_backend.load_dotenv(tmp_path) == {}
    assert "SUPABASE_DB_URL" not in os.environ


@pytest.mark.usefixtures("_reenable_dotenv")
def test_dotenv_path_override_replaces_repo_root(tmp_path, monkeypatch):
    custom = tmp_path / "custom.env"
    custom.write_text("SUPABASE_DB_URL=postgresql://override/db\n")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("LLM_PIPELINE_DOTENV_PATH", str(custom))

    declared = db_backend.load_dotenv()  # no root arg -> honors the override

    assert declared == {"SUPABASE_DB_URL": "postgresql://override/db"}
    assert os.environ["SUPABASE_DB_URL"] == "postgresql://override/db"


# --- first-import regression (fresh interpreter) ----------------------------
#
# The in-process tests above patch db_backend after it is already imported, so
# they cannot catch a bug in the import-time load itself. These run a FRESH
# python that imports db_backend for the first time — the exact moment
# _DOTENV_VALUES = load_dotenv() executes. The fake .env lives in tmp_path and
# is wired in via LLM_PIPELINE_DOTENV_PATH; the real repo root is never touched.
#
# The probe variable is deliberately NEUTRAL (not SUPABASE_DB_URL): a Supabase
# URL would flip IS_SQLITE and make the import require psycopg2, which the easy
# / SQLite-only contributor setup (INSTALL-EASY.md) intentionally does not
# install — the test must stay green there, without a skip. Injection is proven
# just as strictly by the probe; the backend must remain SQLite throughout.

_PROBE = "LLM_PIPELINE_DOTENV_PROBE"
_PROBE_VALUE = "hello-from-dotenv"

_FIRST_IMPORT_CODE = (
    "import os, db_backend; "
    f"print(os.environ.get('{_PROBE}', '<unset>')); "
    "print(db_backend.IS_SQLITE)"
)


def _first_import(tmp_path, *, disable: bool) -> list[str]:
    dotenv = tmp_path / "fake.env"
    dotenv.write_text(f"{_PROBE}={_PROBE_VALUE}\n")

    env = os.environ.copy()
    for key in ("SUPABASE_DB_URL", "SUPABASE_DIRECT_URL", "LLM_PIPELINE_DISABLE_DOTENV", _PROBE):
        env.pop(key, None)
    env["LLM_PIPELINE_DOTENV_PATH"] = str(dotenv)
    env["PYTHONPATH"] = str(SCRIPTS_DIR)
    if disable:
        env["LLM_PIPELINE_DISABLE_DOTENV"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", _FIRST_IMPORT_CODE],
        env=env,
        cwd=tmp_path,  # proves root resolution comes from __file__/override, not cwd
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


@pytest.mark.usefixtures("_reenable_dotenv")
def test_first_import_loads_dotenv_in_fresh_interpreter(tmp_path):
    probe, is_sqlite = _first_import(tmp_path, disable=False)
    assert probe == _PROBE_VALUE  # import-time load injected the .env value
    assert is_sqlite == "True"  # backend untouched — no psycopg2 needed


@pytest.mark.usefixtures("_reenable_dotenv")
def test_first_import_respects_disable_flag(tmp_path):
    probe, is_sqlite = _first_import(tmp_path, disable=True)
    assert probe == "<unset>"  # nothing re-injected after a scrub
    assert is_sqlite == "True"


# --- backend banner --------------------------------------------------------


def _banner(monkeypatch, *, is_sqlite, dotenv):
    monkeypatch.setattr(db_backend, "IS_SQLITE", is_sqlite)
    monkeypatch.setattr(db_backend, "_DOTENV_VALUES", dotenv)
    buf = io.StringIO()
    db_backend.print_backend_banner(buf)
    return buf.getvalue()


@pytest.mark.usefixtures("_reenable_dotenv")
def test_banner_names_sqlite(monkeypatch):
    out = _banner(monkeypatch, is_sqlite=True, dotenv={})
    assert "Backend: local SQLite" in out
    assert "WARNING" not in out


@pytest.mark.usefixtures("_reenable_dotenv")
def test_banner_names_postgres(monkeypatch):
    # No .env file at all is a hazard, not a clean bill of health: the URL
    # must have come from an ambient shell var, so the mismatch warning below
    # is expected to fire (see test_warns_ambient_supabase_url_with_no_dotenv_
    # file_at_all for the dedicated assertions).
    out = _banner(monkeypatch, is_sqlite=False, dotenv={})
    # The banner names the ACTUAL host (self-hosted Postgres since 2026-08), so
    # it asserts the prefix, not a provider name — see _pg_host_label.
    assert "Backend: Postgres (" in out
    assert "WARNING" in out


# --- mismatch warning ------------------------------------------------------


@pytest.mark.usefixtures("_reenable_dotenv")
def test_warns_when_env_wants_supabase_but_run_is_sqlite(monkeypatch):
    out = _banner(
        monkeypatch,
        is_sqlite=True,
        dotenv={"SUPABASE_DB_URL": "postgresql://real/db"},
    )
    assert "WARNING" in out
    assert "configured for Postgres" in out
    assert "local SQLite" in out
    assert "!" * 10 in out  # loud rule, not a lone stderr line


@pytest.mark.usefixtures("_reenable_dotenv")
def test_warns_when_env_has_no_supabase_but_run_is_postgres(monkeypatch):
    out = _banner(
        monkeypatch,
        is_sqlite=False,
        dotenv={"AUTH_USER": "admin"},  # a .env exists, but no Supabase URL
    )
    assert "WARNING" in out
    assert "inherited" in out
    assert "unset SUPABASE_DB_URL" in out


@pytest.mark.usefixtures("_reenable_dotenv")
def test_no_warning_when_supabase_env_matches_postgres(monkeypatch):
    out = _banner(
        monkeypatch,
        is_sqlite=False,
        dotenv={"SUPABASE_DB_URL": "postgresql://real/db"},
    )
    assert "WARNING" not in out


@pytest.mark.usefixtures("_reenable_dotenv")
def test_no_warning_when_sqlite_and_env_has_no_supabase(monkeypatch):
    out = _banner(monkeypatch, is_sqlite=True, dotenv={"AUTH_USER": "admin"})
    assert "WARNING" not in out


@pytest.mark.usefixtures("_reenable_dotenv")
def test_warns_ambient_supabase_url_with_no_dotenv_file_at_all(monkeypatch):
    """Regression: an ambient SUPABASE_DB_URL with NO .env file used to slip
    past the early-return guard (``if not _DOTENV_VALUES: return``) and drive
    Postgres with zero warning — the exact hazard INSTALL-EASY.md documents
    but the runtime never enforced."""
    out = _banner(monkeypatch, is_sqlite=False, dotenv={})
    assert "WARNING" in out
    assert "inherited" in out
    assert "unset SUPABASE_DB_URL" in out


# --- the host label the banner prints --------------------------------------


def _label(monkeypatch, url):
    monkeypatch.setenv("SUPABASE_DB_URL", url)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    return db_backend._pg_host_label()


@pytest.mark.usefixtures("_reenable_dotenv")
def test_host_label_names_a_remote_host(monkeypatch):
    assert _label(monkeypatch, "postgresql://user:pw@db.example.test:5432/jobsearch") == (
        "db.example.test"
    )


@pytest.mark.usefixtures("_reenable_dotenv")
def test_host_label_reads_a_url_with_no_database_path(monkeypatch):
    """The regex this replaced required a trailing "/dbname" and fell back to
    the useless "postgres" without one — hiding the host the banner exists to
    show."""
    assert _label(monkeypatch, "postgresql://user:pw@db.example.test:5432") == "db.example.test"


@pytest.mark.usefixtures("_reenable_dotenv")
def test_host_label_survives_an_at_sign_in_the_password(monkeypatch):
    assert _label(monkeypatch, "postgresql://user:p@ss@db.example.test/jobsearch") == (
        "db.example.test"
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
@pytest.mark.usefixtures("_reenable_dotenv")
def test_host_label_names_a_tunnel_with_its_port(monkeypatch, host):
    assert _label(monkeypatch, f"postgresql://user:pw@{host}:15432/jobsearch") == (
        f"local tunnel {host}:15432"
    )


@pytest.mark.usefixtures("_reenable_dotenv")
def test_host_label_defaults_the_tunnel_port(monkeypatch):
    assert _label(monkeypatch, "postgresql://user:pw@127.0.0.1/jobsearch") == (
        "local tunnel 127.0.0.1:5432"
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "postgresql://user:pw@db.example.test:not-a-port/jobsearch",
    ],
)
@pytest.mark.usefixtures("_reenable_dotenv")
def test_host_label_falls_back_instead_of_raising(monkeypatch, url):
    """A banner must never be the thing that crashes a run."""
    assert _label(monkeypatch, url) == "postgres"


# --- missing psycopg2 driver ----------------------------------------------


@pytest.mark.usefixtures("_reenable_dotenv")
def test_require_psycopg2_missing_gives_actionable_error(monkeypatch, capsys):
    # simulate "driver not installed" — import psycopg2 raises ImportError
    monkeypatch.setitem(sys.modules, "psycopg2", None)

    with pytest.raises(SystemExit):
        db_backend._require_psycopg2()

    err = capsys.readouterr().err
    assert "psycopg2 is not installed" in err
    assert "pip install -r requirements.txt" in err
    # never leaks the raw ModuleNotFoundError text to the user
    assert "ModuleNotFoundError" not in err


# --- from test_prod_write_guard.py ---
#
# Tests for the prod-write guard in db_backend.
#
# An ad-hoc script run with SUPABASE_DB_URL set in the environment (a stray
# export, a debug one-off with no pytest fixture to clean up after it) used to
# be able to INSERT/UPDATE/DELETE straight into the live Supabase DB. These
# tests lock the guard's contract directly against the pure helper functions
# (``_is_write_statement`` / ``_running_as_pipeline_script`` /
# ``_prod_write_context_ok`` / ``_check_write_allowed``) and the cursor/conn
# wrappers (``_GuardedCursor`` / ``_GuardedConn``) — no live Postgres needed,
# per the ticket's constraint. A fake cursor/connection stands in for psycopg2.
#
# Because pytest itself sets ``PYTEST_CURRENT_TEST`` for the duration of every
# test, the "unrecognized context" scenarios must explicitly delete it to
# simulate what running OUTSIDE pytest looks like. Pytest re-sets that variable
# at every setup/call/teardown phase transition, so the deletion must happen
# INSIDE the test body (the "call" phase) via a plain helper function rather
# than a fixture (fixtures run during "setup", one phase transition too early —
# pytest overwrites the variable again right before "call" begins).
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
        # A leading comment must not smuggle a write past the guard.
        "-- backfill\nUPDATE vacancy SET status = %s WHERE id = %s",
        "/* one-off fix */ INSERT INTO company (canonical_name) VALUES (%s)",
        "-- a\n-- b\n  /* c */\nDELETE FROM vacancy WHERE id = %s",
        "/* multi\n   line */-- and a line comment\nINSERT INTO board (id) VALUES (%s)",
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
        "-- what would an UPDATE hit?\nSELECT * FROM vacancy",  # comment stays a comment
        "/* DELETE nothing */ SELECT 1",
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

    # A leading comment must not slip a write through in a blocked context.
    with pytest.raises(db_backend.ProdWriteBlocked):
        db_backend._check_write_allowed("-- backfill\nUPDATE vacancy SET status = %s WHERE id = %s")


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


@pytest.mark.parametrize("entrypoint", sorted(db_backend._PIPELINE_ENTRYPOINTS))
def test_pipeline_entrypoint_context_allows_write(monkeypatch, entrypoint):
    """Every KNOWN entrypoint (run_daily.py and each stage/CLI it covers) is
    identified by argv[0]: allowlisted basename AND inside repo scripts/."""
    _not_pytest(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.delenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, raising=False)
    scripts_dir = db_backend.PROJECT_ROOT / "scripts"
    monkeypatch.setattr("sys.argv", [str(scripts_dir / entrypoint)])

    assert db_backend._running_as_pipeline_script() is True
    assert db_backend._prod_write_context_ok() is True
    db_backend._check_write_allowed(
        "UPDATE vacancy SET llm_score = NULL WHERE id = ANY(%s::uuid[])"
    )


def test_allowlisted_entrypoints_actually_exist():
    """The allowlist names real files — a renamed/deleted entrypoint would
    otherwise silently lose its write access (or keep trusting a ghost)."""
    scripts_dir = db_backend.PROJECT_ROOT / "scripts"
    for name in db_backend._PIPELINE_ENTRYPOINTS:
        assert (scripts_dir / name).exists(), f"allowlisted entrypoint missing: scripts/{name}"


def test_unknown_script_inside_scripts_dir_is_blocked(monkeypatch):
    """Location is not identity (the incident's exact shape): a NEW scratch
    one-off dumped into scripts/ is NOT trusted until deliberately
    allowlisted."""
    _not_pytest(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.delenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, raising=False)
    scripts_dir = db_backend.PROJECT_ROOT / "scripts"
    monkeypatch.setattr("sys.argv", [str(scripts_dir / "scratch_expire_inc_debug.py")])

    assert db_backend._running_as_pipeline_script() is False
    with pytest.raises(db_backend.ProdWriteBlocked):
        db_backend._check_write_allowed("INSERT INTO company (canonical_name) VALUES (%s)")


def test_allowlisted_basename_outside_scripts_dir_is_blocked(monkeypatch, tmp_path):
    """A same-named file elsewhere (e.g. /tmp/run_daily.py) can't borrow trust:
    the directory check must hold alongside the basename allowlist."""
    _not_pytest(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.delenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, raising=False)
    monkeypatch.setattr("sys.argv", [str(tmp_path / "run_daily.py")])

    assert db_backend._running_as_pipeline_script() is False
    with pytest.raises(db_backend.ProdWriteBlocked):
        db_backend._check_write_allowed("INSERT INTO company (canonical_name) VALUES (%s)")


def test_running_as_pipeline_script_false_for_empty_argv0(monkeypatch):
    monkeypatch.setattr("sys.argv", [""])
    assert db_backend._running_as_pipeline_script() is False


def test_running_as_pipeline_script_false_for_python_dash_c(monkeypatch):
    """``python3 -c "..."`` one-liners have argv[0] == "-c" — never trusted."""
    monkeypatch.setattr("sys.argv", ["-c"])
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
        self.cursor_kwargs = []
        self.closed = 0
        self.committed = False
        self.entered = False
        self.exited = False

    def cursor(self, *args, **kwargs):
        self.cursor_kwargs.append((args, kwargs))
        cur = _FakeCursor()
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = 1

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        return False


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


def test_guarded_conn_cursor_forwards_args_and_kwargs():
    """cursor(cursor_factory=...) and psycopg2 extras like cursor(name=...)
    (server-side cursors) must pass through untouched."""
    fake_conn = _FakeConn()
    guarded = db_backend._GuardedConn(fake_conn)

    guarded.cursor()
    guarded.cursor(cursor_factory="RealDictCursor-marker")
    guarded.cursor(name="server_side")

    assert fake_conn.cursor_kwargs == [
        ((), {}),
        ((), {"cursor_factory": "RealDictCursor-marker"}),
        ((), {"name": "server_side"}),
    ]


def test_guarded_conn_is_a_context_manager():
    """``with conn:`` looks up __enter__/__exit__ on the TYPE, bypassing
    __getattr__ — the wrapper must delegate them explicitly."""
    fake_conn = _FakeConn()
    guarded = db_backend._GuardedConn(fake_conn)

    with guarded as ctx:
        assert ctx is guarded
    assert fake_conn.entered is True
    assert fake_conn.exited is True


# --- from test_simple_mode_no_psycopg2.py ---
#
# Simple-mode smoke test: every DB-touching entry script imports and performs
# a basic operation on a clean SQLite backend with **no psycopg2 available**.
#
# This locks the easy-install contract: the SQLite path must never import the
# compiled Postgres driver. We simulate its absence by pointing
# ``sys.modules['psycopg2']`` (and ``psycopg2.extras``) at ``None`` so any stray
# ``import psycopg2`` raises ``ImportError`` — the package stays physically
# installed, only this view removes it. The scripts must therefore source their
# ``Json`` / ``RealDictCursor`` helpers from ``db_backend`` (whose SQLite branch
# never touches psycopg2), not from ``psycopg2.extras`` directly.
#
# Everything runs offline against a fresh temp SQLite DB (tmp_path).
# Import graph rebuilt per test so IS_SQLITE recomputes for the temp DB and the
# entry scripts re-import their db_backend-sourced helpers under the block.
_RESET = [
    "score_companies",
    "fetch_companies",
    "enrich_blind_vacancies",
    "triage",
    "database_supabase",
    "company_registry",
    "config",
    "db_conn",
    "db_backend",
    "filter_vacancies",
    "filters",
    "fetchers",
    "quality",
    "run_status",
    "prompts",
]


@pytest.fixture()
def simple(tmp_path, monkeypatch):
    """Clean SQLite backend with psycopg2 rendered un-importable."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    # Make psycopg2 un-importable for the duration of the test.
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", None)

    # Remember the real modules so they can be put back verbatim afterwards.
    # Rebuilding the import graph here binds config/filters/fetchers/prompts to
    # this temp DB and to the psycopg2-less view. Those rebuilt modules must not
    # outlive the test: every other test module bound its own references at
    # collection time, and leaving the replacements in sys.modules quietly
    # changes what later tests see (a fetcher parsing 1 job instead of 2 because
    # it re-read an empty registry). Restoring the ORIGINAL objects — not just
    # popping the new ones — is what keeps the rest of the session honest.
    _saved = {mod: sys.modules[mod] for mod in _RESET if mod in sys.modules}

    for mod in _RESET:
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "simple-mode tests must run on SQLite"

    import database_supabase as dal

    ns = type("SimpleEnv", (), {})()
    ns.dal = dal
    yield ns
    dal.close_conn()

    for mod in _RESET:
        sys.modules.pop(mod, None)
    sys.modules.update(_saved)


def test_psycopg2_is_unimportable(simple):
    """The simulation genuinely removes psycopg2 from view."""
    with pytest.raises(ImportError):
        import psycopg2  # noqa: F401


def test_db_backend_reexports_shims_without_psycopg2(simple):
    """db_backend exposes Json / RealDictCursor from its SQLite branch."""
    import db_backend

    assert db_backend.Json({"a": 1}).dumps() == '{"a": 1}'
    assert db_backend.RealDictCursor is not None
    assert db_backend.get_conn() is not None


def test_score_companies_cmd_save_persists(simple):
    """score_companies imports and cmd_save writes enrichment to SQLite."""
    import score_companies

    dal = simple.dal
    cid = dal.ensure_company("Acme Robotics", status="candidate")
    dal.get_conn().commit()

    payload = [
        {
            "payload_kind": "company",
            "id": str(cid),
            "canonical_name": "Acme Robotics",
            "enrichment": {
                "about": {"description": "x", "sector": "Robotics"},
                "mission_fit": {"alignment_score": 70, "alignment_label": "ok"},
                "alignment_score": 70,
            },
        }
    ]
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        score_companies.cmd_save(types.SimpleNamespace(no_auto_review=True))
    finally:
        sys.stdin = old_stdin

    assert dal.load_company_enrichment("Acme Robotics")["alignment_score"] == 70


def test_fetch_companies_social_reaches_json_import(simple, monkeypatch):
    """fetch_companies imports and cmd_social runs the (formerly psycopg2) Json
    import path without psycopg2. _enrich_social_signals is stubbed to yield no
    data, so the loop `continue`s and no row is written — the point is that the
    `from db_backend import Json` line is reached and resolves under the block."""
    import fetch_companies

    dal = simple.dal
    cid = dal.ensure_company("Social Co", status="candidate")
    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE company SET website = %s WHERE id = %s", ("https://social.example", cid))
    cur.close()
    conn.commit()

    monkeypatch.setattr(fetch_companies, "_get_firecrawl_client", lambda: object())
    monkeypatch.setattr(fetch_companies, "_enrich_social_signals", lambda client, name: None)

    # Reaches `from db_backend import Json` (line before the loop) and returns
    # cleanly; no psycopg2 needed.
    fetch_companies.cmd_social(types.SimpleNamespace(company=None, limit=None))


def test_enrich_blind_vacancies_main_on_empty_db(simple, monkeypatch):
    """enrich_blind_vacancies imports and main() runs a DB read on an empty DB,
    returning before any Firecrawl call."""
    import enrich_blind_vacancies

    monkeypatch.setattr(sys, "argv", ["enrich_blind_vacancies.py"])
    enrich_blind_vacancies.main()  # no blind vacancies → clean return


def test_triage_update_vacancies_in_db(simple):
    """triage imports and update_vacancies_in_db writes via the RealDictCursor +
    Json shims on SQLite (its former psycopg2.extras import)."""
    import triage

    dal = simple.dal
    dal.ensure_company("Triage Co", status="active")
    dal.save_vacancies(
        "Triage Co",
        "A",
        [
            {
                "title": "Data Lead",
                "snippet": "Data Lead blurb.",
                "full_description": "We are hiring a Data Lead. " * 12,
                "location": "Berlin, Germany",
                # deliberately no "url" — update_vacancies_in_db fills it
            }
        ],
    )
    dal.get_conn().commit()

    vid = next(
        v_id
        for v_id, v in dal.load_vacancies(include_inactive_companies=True).items()
        if v["title"] == "Data Lead"
    )

    changed = triage.update_vacancies_in_db({vid: {"url": "https://triage.example/job"}})
    assert changed == 1

    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT locations FROM vacancy WHERE id = %s", (vid,))
    locs = cur.fetchone()[0]  # decoded from JSON TEXT by the shim
    cur.close()
    assert locs[0]["url"] == "https://triage.example/job"
