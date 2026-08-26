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


def test_first_import_loads_dotenv_in_fresh_interpreter(tmp_path):
    probe, is_sqlite = _first_import(tmp_path, disable=False)
    assert probe == _PROBE_VALUE  # import-time load injected the .env value
    assert is_sqlite == "True"  # backend untouched — no psycopg2 needed


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


def test_banner_names_sqlite(monkeypatch):
    out = _banner(monkeypatch, is_sqlite=True, dotenv={})
    assert "Backend: local SQLite" in out
    assert "WARNING" not in out


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


def test_warns_ambient_supabase_url_with_no_dotenv_file_at_all(monkeypatch):
    """Regression: an ambient SUPABASE_DB_URL with NO .env file used to slip
    past the early-return guard (``if not _DOTENV_VALUES: return``) and drive
    Postgres with zero warning — the exact hazard INSTALL-EASY.md documents
    but the runtime never enforced."""
    out = _banner(monkeypatch, is_sqlite=False, dotenv={})
    assert "WARNING" in out
    assert "inherited" in out
    assert "unset SUPABASE_DB_URL" in out


def test_no_warning_when_postgres_url_comes_from_dotenv(monkeypatch):
    out = _banner(
        monkeypatch,
        is_sqlite=False,
        dotenv={"SUPABASE_DB_URL": "postgresql://real/db"},
    )
    assert "WARNING" not in out


# --- the host label the banner prints --------------------------------------


def _label(monkeypatch, url):
    monkeypatch.setenv("SUPABASE_DB_URL", url)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    return db_backend._pg_host_label()


def test_host_label_names_a_remote_host(monkeypatch):
    assert _label(monkeypatch, "postgresql://user:pw@db.example.test:5432/jobsearch") == (
        "db.example.test"
    )


def test_host_label_reads_a_url_with_no_database_path(monkeypatch):
    """The regex this replaced required a trailing "/dbname" and fell back to
    the useless "postgres" without one — hiding the host the banner exists to
    show."""
    assert _label(monkeypatch, "postgresql://user:pw@db.example.test:5432") == "db.example.test"


def test_host_label_survives_an_at_sign_in_the_password(monkeypatch):
    assert _label(monkeypatch, "postgresql://user:p@ss@db.example.test/jobsearch") == (
        "db.example.test"
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_host_label_names_a_tunnel_with_its_port(monkeypatch, host):
    assert _label(monkeypatch, f"postgresql://user:pw@{host}:15432/jobsearch") == (
        f"local tunnel {host}:15432"
    )


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
def test_host_label_falls_back_instead_of_raising(monkeypatch, url):
    """A banner must never be the thing that crashes a run."""
    assert _label(monkeypatch, url) == "postgres"


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
