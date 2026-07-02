"""End-to-end tests for the sources CLI (scripts/sources.py).

Drives the real command through a throwaway SQLite DB brought up with the board
table (0002) and the enabled flag (0011) on top of the frozen baseline -- the
same shape a migrated install has. Proves the visibility screen and that an
enabled board is committed (survives the process), plus the unknown-id guard.

Fully offline, invented orgs/boards, isolated temp DB.
"""

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "sql" / "migrations"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh SQLite DB + a freshly imported sources module bound to it."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.delenv("JOB_BOARDS", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    # Reload the whole chain BEFORE importing sources so its module-level DAL
    # bindings land on this backend.
    for mod in (
        "sources",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    conn = db_backend.get_conn()  # first connection auto-applies the frozen baseline
    cur = conn.cursor()
    for m in ("0002_board_table", "0011_board_enabled"):  # 0011 ALTERs what 0002 creates
        cur.execute((MIGRATIONS / f"{m}.sqlite.sql").read_text(encoding="utf-8"))
    cur.close()
    conn.commit()

    import database_supabase
    import sources

    yield sources, database_supabase
    database_supabase.close_conn()


def _a_known_board(sources):
    """First real board id from config -- keeps the test place/taste-agnostic."""
    return sorted(sources._ALL_JOB_BOARDS)[0]


def test_enable_board_persists_and_shows_in_list(env, capsys):
    sources, dal = env
    board = _a_known_board(sources)

    assert sources.main(["enable-board", board]) == 0
    assert dal.get_enabled_boards() == [board]

    capsys.readouterr()  # drop the enable message
    assert sources.main([]) == 0  # default subcommand == list
    out = capsys.readouterr().out
    assert board in out
    assert sources._board_name(board) in out


def test_enabled_board_survives_a_reconnect(env):
    """The persistence guarantee: the flag is committed, not just held in-session."""
    sources, dal = env
    board = _a_known_board(sources)
    assert sources.main(["enable-board", board]) == 0
    dal.close_conn()  # drop the connection the enable wrote through
    assert board in dal.get_enabled_boards()  # a fresh connection still sees it


def test_unknown_board_is_rejected_and_persists_nothing(env, capsys):
    sources, dal = env
    assert sources.main(["enable-board", "definitely_not_a_board"]) == 2
    out = capsys.readouterr().out
    assert "Unknown board" in out
    assert dal.get_enabled_boards() == []


def test_disable_board_clears_the_flag(env):
    sources, dal = env
    board = _a_known_board(sources)
    sources.main(["enable-board", board])
    assert dal.get_enabled_boards() == [board]

    assert sources.main(["disable-board", board]) == 0
    assert dal.get_enabled_boards() == []


@pytest.fixture()
def env_unmigrated(tmp_path, monkeypatch):
    """Same chain as `env`, but with a board table that predates 0011 — the
    shape a user has after `git pull` but before `python3 scripts/migrate.py`."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.delenv("JOB_BOARDS", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "sources",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    conn = db_backend.get_conn()
    cur = conn.cursor()
    cur.execute((MIGRATIONS / "0002_board_table.sqlite.sql").read_text(encoding="utf-8"))
    cur.close()
    conn.commit()

    import database_supabase
    import sources

    yield sources, database_supabase
    database_supabase.close_conn()


def test_unmigrated_schema_gets_migrate_hint_not_traceback(env_unmigrated, capsys):
    """Review finding on #39: pre-0011 schema must answer with the migrate
    command, not a raw OperationalError."""
    sources, dal = env_unmigrated
    board = _a_known_board(sources)

    assert sources.main(["enable-board", board]) == 1
    out = capsys.readouterr().out
    assert "scripts/migrate.py" in out

    with pytest.raises(dal.BoardPersistenceUnavailable, match="migrate.py"):
        dal.get_enabled_boards()


def test_list_reports_active_companies(env, capsys):
    sources, dal = env
    dal.ensure_company("Fictive Aid Trust", status="active")
    dal.ensure_company("Dormant Labs", status="inactive")
    dal.get_conn().commit()

    assert sources.main([]) == 0
    out = capsys.readouterr().out
    assert "Fictive Aid Trust" in out  # active -> listed
    assert "Dormant Labs" not in out  # inactive -> not a live source
