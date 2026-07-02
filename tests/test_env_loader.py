"""Tests for the stdlib .env loader and backend-mismatch banner in db_backend.

These lock the auto-load contract: a filled repo-root ``.env`` is picked up
automatically (no manual ``export``), an already-exported shell var wins over
the file, an absent/empty ``.env`` stays on SQLite without error, a file-vs-
runtime backend mismatch is flagged loudly, and a missing psycopg2 driver in
full mode gives an actionable message instead of a raw ModuleNotFoundError.
"""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

import db_backend

SCRIPTS_DIR = Path(db_backend.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _restore_env():
    """load_dotenv mutates os.environ via setdefault (not through monkeypatch),
    so snapshot and restore the whole environment around every test to keep the
    rest of the suite deterministic.

    conftest sets LLM_PIPELINE_DISABLE_DOTENV=1 suite-wide (so the import-time
    load can't re-inject the maintainer's real .env). These tests exercise the
    loader on purpose — against tmp paths only — so re-enable it locally; the
    teardown restore puts the flag back for the rest of the suite.
    """
    saved = os.environ.copy()
    os.environ.pop("LLM_PIPELINE_DISABLE_DOTENV", None)
    yield
    os.environ.clear()
    os.environ.update(saved)


# --- parsing ---------------------------------------------------------------


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


def test_parse_dotenv_missing_file_returns_empty(tmp_path):
    assert db_backend._parse_dotenv(tmp_path / "nope.env") == {}


# --- load into os.environ --------------------------------------------------


def test_load_dotenv_from_root_populates_environ(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SUPABASE_DB_URL=postgresql://real/db\n")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)

    declared = db_backend.load_dotenv(tmp_path)

    assert declared["SUPABASE_DB_URL"] == "postgresql://real/db"
    assert os.environ["SUPABASE_DB_URL"] == "postgresql://real/db"
    # backend selection would now resolve to Postgres
    assert db_backend._supabase_url() == "postgresql://real/db"


def test_existing_env_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://preset/db")
    (tmp_path / ".env").write_text("SUPABASE_DB_URL=postgresql://fromfile/db\n")

    declared = db_backend.load_dotenv(tmp_path)

    # the file still reports what it declared...
    assert declared["SUPABASE_DB_URL"] == "postgresql://fromfile/db"
    # ...but the already-exported shell var keeps priority
    assert os.environ["SUPABASE_DB_URL"] == "postgresql://preset/db"


def test_absent_dotenv_is_silent_and_stays_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)

    assert db_backend.load_dotenv(tmp_path) == {}  # empty dir, no .env
    assert db_backend._supabase_url() is None  # -> IS_SQLITE would be True


def test_empty_dotenv_is_silent(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("\n# only comments here\n\n")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)

    assert db_backend.load_dotenv(tmp_path) == {}


def test_disable_flag_makes_load_a_noop(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SUPABASE_DB_URL=postgresql://real/db\n")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("LLM_PIPELINE_DISABLE_DOTENV", "1")

    assert db_backend.load_dotenv(tmp_path) == {}
    assert "SUPABASE_DB_URL" not in os.environ


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

_FIRST_IMPORT_CODE = (
    "import os, db_backend; "
    "print(os.environ.get('SUPABASE_DB_URL', '<unset>')); "
    "print(db_backend.IS_SQLITE)"
)


def _first_import(tmp_path, *, disable: bool) -> list[str]:
    dotenv = tmp_path / "fake.env"
    dotenv.write_text("SUPABASE_DB_URL=postgresql://fake-first-import/db\n")

    env = os.environ.copy()
    for key in ("SUPABASE_DB_URL", "SUPABASE_DIRECT_URL", "LLM_PIPELINE_DISABLE_DOTENV"):
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


def test_first_import_loads_dotenv_in_fresh_interpreter(tmp_path):
    supabase_url, is_sqlite = _first_import(tmp_path, disable=False)
    assert supabase_url == "postgresql://fake-first-import/db"
    assert is_sqlite == "False"  # backend selection saw the .env value


def test_first_import_respects_disable_flag(tmp_path):
    supabase_url, is_sqlite = _first_import(tmp_path, disable=True)
    assert supabase_url == "<unset>"  # nothing re-injected after a scrub
    assert is_sqlite == "True"


# --- backend banner --------------------------------------------------------


def _banner(monkeypatch, *, is_sqlite, dotenv):
    monkeypatch.setattr(db_backend, "IS_SQLITE", is_sqlite)
    monkeypatch.setattr(db_backend, "_DOTENV_VALUES", dotenv)
    buf = io.StringIO()
    db_backend.print_backend_banner(buf)
    return buf.getvalue()


def test_banner_names_sqlite(monkeypatch):
    out = _banner(monkeypatch, is_sqlite=True, dotenv={})
    assert "Backend: local SQLite" in out
    assert "WARNING" not in out


def test_banner_names_postgres(monkeypatch):
    out = _banner(monkeypatch, is_sqlite=False, dotenv={})
    assert "Backend: Postgres (Supabase)" in out
    assert "WARNING" not in out  # no .env file -> nothing to contradict


# --- mismatch warning ------------------------------------------------------


def test_warns_when_env_wants_supabase_but_run_is_sqlite(monkeypatch):
    out = _banner(
        monkeypatch,
        is_sqlite=True,
        dotenv={"SUPABASE_DB_URL": "postgresql://real/db"},
    )
    assert "WARNING" in out
    assert "configured for Supabase" in out
    assert "local SQLite" in out
    assert "!" * 10 in out  # loud rule, not a lone stderr line


def test_warns_when_env_has_no_supabase_but_run_is_postgres(monkeypatch):
    out = _banner(
        monkeypatch,
        is_sqlite=False,
        dotenv={"AUTH_USER": "admin"},  # a .env exists, but no Supabase URL
    )
    assert "WARNING" in out
    assert "inherited" in out
    assert "unset SUPABASE_DB_URL" in out


def test_no_warning_when_supabase_env_matches_postgres(monkeypatch):
    out = _banner(
        monkeypatch,
        is_sqlite=False,
        dotenv={"SUPABASE_DB_URL": "postgresql://real/db"},
    )
    assert "WARNING" not in out


def test_no_warning_when_sqlite_and_env_has_no_supabase(monkeypatch):
    out = _banner(monkeypatch, is_sqlite=True, dotenv={"AUTH_USER": "admin"})
    assert "WARNING" not in out


# --- missing psycopg2 driver ----------------------------------------------


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
