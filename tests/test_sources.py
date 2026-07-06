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
    for m in (
        "0002_board_table",  # 0011 ALTERs what 0002 creates
        "0011_board_enabled",
        "0013_add_source_board",  # needed to resolve board -> its vacancies
        "0014_add_vacancy_status_reason",  # needed to record why they were archived
    ):
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


def _seed_vacancy(dal, *, company_id, title, status, source_board, llm_score=None):
    """Insert a bare vacancy row directly -- bypasses save_board_vacancies so
    each test controls status/source_board/llm_score precisely, independent of
    the dedup/company-resolution machinery that isn't under test here."""
    cur = dal.get_conn().cursor()
    cur.execute(
        """INSERT INTO vacancy
               (dedup_hash, company_id, title, first_seen, last_seen, status,
                source_board, llm_score)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            dal.make_vacancy_id(source_board, title),
            company_id,
            title,
            "2026-01-01",
            "2026-01-01",
            status,
            source_board,
            llm_score,
        ),
    )
    cur.close()


def _status_and_reason(dal, title):
    cur = dal.get_conn().cursor()
    cur.execute("SELECT status, status_reason FROM vacancy WHERE title = %s", (title,))
    row = cur.fetchone()
    cur.close()
    return tuple(row) if row else None


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


def test_disable_board_archives_its_unseen_vacancies_and_reports_it(env, capsys):
    """The go-forward rule: disabling a board must not leave its untriaged
    rows stuck forever. Scored/decided rows of the SAME board are untouched."""
    sources, dal = env
    board = _a_known_board(sources)
    board_name = sources._ALL_JOB_BOARDS[board]["name"]  # display name != board id
    dal.sync_boards({board: sources._ALL_JOB_BOARDS[board]})
    dal.get_conn().commit()
    sources.main(["enable-board", board])

    company_id = dal.ensure_company("Fictive Robotics Guild", status="candidate")
    dal.get_conn().commit()
    _seed_vacancy(
        dal, company_id=company_id, title="Unseen Analyst", status="unseen", source_board=board_name
    )
    _seed_vacancy(
        dal,
        company_id=company_id,
        title="Liked Role",
        status="liked",
        source_board=board_name,
        llm_score=80,
    )
    dal.get_conn().commit()

    capsys.readouterr()  # drop the enable message
    rc = sources.main(["disable-board", board])
    out = capsys.readouterr().out

    assert rc == 0
    assert "1 unseen vacancy archived (board_disabled)" in out
    assert dal.get_enabled_boards() == []
    assert _status_and_reason(dal, "Unseen Analyst") == ("archived", "board_disabled")
    assert _status_and_reason(dal, "Liked Role") == ("liked", None)  # decided row untouched


def test_disable_board_does_not_touch_other_boards_unseen_vacancies(env):
    sources, dal = env
    board_a, board_b = sorted(sources._ALL_JOB_BOARDS)[:2]
    name_a = sources._ALL_JOB_BOARDS[board_a]["name"]
    name_b = sources._ALL_JOB_BOARDS[board_b]["name"]
    dal.sync_boards(
        {board_a: sources._ALL_JOB_BOARDS[board_a], board_b: sources._ALL_JOB_BOARDS[board_b]}
    )
    dal.get_conn().commit()

    company_id = dal.ensure_company("Fictive Robotics Guild", status="candidate")
    dal.get_conn().commit()
    _seed_vacancy(
        dal, company_id=company_id, title="Board A Role", status="unseen", source_board=name_a
    )
    _seed_vacancy(
        dal, company_id=company_id, title="Board B Role", status="unseen", source_board=name_b
    )
    dal.get_conn().commit()

    assert sources.main(["disable-board", board_a]) == 0

    assert _status_and_reason(dal, "Board A Role") == ("archived", "board_disabled")
    assert _status_and_reason(dal, "Board B Role")[0] == "unseen"  # untouched


def test_disable_board_archives_scored_but_unseen_row_too(env):
    """'Unseen' is decided by status alone: a row that was LLM-scored but never
    triaged (status still 'unseen') archives exactly like a never-scored one --
    scoring is not a decision, so it earns no protection here."""
    sources, dal = env
    board = _a_known_board(sources)
    board_name = sources._ALL_JOB_BOARDS[board]["name"]
    dal.sync_boards({board: sources._ALL_JOB_BOARDS[board]})
    dal.get_conn().commit()

    company_id = dal.ensure_company("Fictive Robotics Guild", status="candidate")
    dal.get_conn().commit()
    _seed_vacancy(
        dal,
        company_id=company_id,
        title="Scored Yet Unseen",
        status="unseen",
        source_board=board_name,
        llm_score=64,
    )
    dal.get_conn().commit()

    assert sources.main(["disable-board", board]) == 0
    assert _status_and_reason(dal, "Scored Yet Unseen") == ("archived", "board_disabled")


def test_disable_board_aborts_on_shared_display_name(env, capsys):
    """board.name carries no UNIQUE constraint: when two catalog rows share one
    display name, a name-keyed archive would sweep the OTHER board's rows too.
    The guard aborts the archive (board still gets disabled) with exit 1."""
    sources, dal = env
    board = _a_known_board(sources)
    board_name = sources._ALL_JOB_BOARDS[board]["name"]
    dal.sync_boards({board: sources._ALL_JOB_BOARDS[board]})
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO board (id, name) VALUES (%s, %s)",
        ("impostor_board", board_name),  # same display name, different id
    )
    cur.close()
    dal.get_conn().commit()

    company_id = dal.ensure_company("Fictive Robotics Guild", status="candidate")
    dal.get_conn().commit()
    _seed_vacancy(
        dal, company_id=company_id, title="Ambiguous Role", status="unseen", source_board=board_name
    )
    dal.get_conn().commit()

    rc = sources.main(["disable-board", board])
    out = capsys.readouterr().out

    assert rc == 1
    assert "archiving its unseen vacancies failed" in out
    assert "is shared by boards" in out
    assert dal.get_enabled_boards() == []  # the disable itself still landed
    assert _status_and_reason(dal, "Ambiguous Role")[0] == "unseen"  # nothing swept


def test_reenabling_a_board_does_not_resurrect_archived_rows(env):
    sources, dal = env
    board = _a_known_board(sources)
    board_name = sources._ALL_JOB_BOARDS[board]["name"]
    dal.sync_boards({board: sources._ALL_JOB_BOARDS[board]})
    dal.get_conn().commit()

    company_id = dal.ensure_company("Fictive Robotics Guild", status="candidate")
    dal.get_conn().commit()
    _seed_vacancy(
        dal, company_id=company_id, title="Once Unseen", status="unseen", source_board=board_name
    )
    dal.get_conn().commit()

    sources.main(["disable-board", board])
    sources.main(["enable-board", board])  # re-enable

    assert _status_and_reason(dal, "Once Unseen") == ("archived", "board_disabled")


def test_disable_unknown_board_id_archives_nothing_and_does_not_crash(env, capsys):
    """cmd_disable is deliberately tolerant of an id config no longer ships
    (the clean-up path). Archiving must degrade to zero, not raise."""
    sources, dal = env
    rc = sources.main(["disable-board", "definitely_not_a_board"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 unseen vacancies archived (board_disabled)" in out


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


@pytest.fixture()
def env_pre_source_board(tmp_path, monkeypatch):
    """Same chain as `env`, but only 0002 + 0011 applied -- the shape between
    migrations 0011 and 0013: board.enabled exists, vacancy.source_board
    doesn't yet."""
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
    for m in ("0002_board_table", "0011_board_enabled"):
        cur.execute((MIGRATIONS / f"{m}.sqlite.sql").read_text(encoding="utf-8"))
    cur.close()
    conn.commit()

    import database_supabase
    import sources

    yield sources, database_supabase
    database_supabase.close_conn()


def test_disable_board_without_source_board_column_does_not_crash(env_pre_source_board, capsys):
    """A schema between 0011 and 0013 (board.enabled exists, vacancy.source_board
    doesn't yet) must still disable cleanly -- archiving degrades to zero
    instead of raising a raw "no such column" error."""
    sources, dal = env_pre_source_board
    board = _a_known_board(sources)
    sources.main(["enable-board", board])
    capsys.readouterr()

    rc = sources.main(["disable-board", board])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 unseen vacancies archived (board_disabled)" in out


@pytest.fixture()
def env_pre_status_reason(tmp_path, monkeypatch):
    """Same chain as `env`, but WITHOUT the status_reason migration -- exactly
    the production shape at the moment this feature ships: vacancy.source_board
    exists (0013 already applied), vacancy.status_reason does not (0014 not yet
    run)."""
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
    for m in ("0002_board_table", "0011_board_enabled", "0013_add_source_board"):
        cur.execute((MIGRATIONS / f"{m}.sqlite.sql").read_text(encoding="utf-8"))
    cur.close()
    conn.commit()

    import database_supabase
    import sources

    yield sources, database_supabase
    database_supabase.close_conn()


def test_disable_board_without_status_reason_column_still_archives(env_pre_status_reason, capsys):
    """On a schema with source_board but not yet status_reason (0013 applied,
    0014 pending -- the exact prod shape this ships into), the archive still
    happens; only the reason recording is skipped (the guarded fallback
    branch)."""
    sources, dal = env_pre_status_reason
    board = _a_known_board(sources)
    board_name = sources._ALL_JOB_BOARDS[board]["name"]
    dal.sync_boards({board: sources._ALL_JOB_BOARDS[board]})
    dal.get_conn().commit()
    sources.main(["enable-board", board])

    company_id = dal.ensure_company("Fictive Robotics Guild", status="candidate")
    dal.get_conn().commit()
    _seed_vacancy(
        dal,
        company_id=company_id,
        title="Pre-Reason Role",
        status="unseen",
        source_board=board_name,
    )
    dal.get_conn().commit()

    capsys.readouterr()  # drop the enable message
    rc = sources.main(["disable-board", board])
    out = capsys.readouterr().out

    assert rc == 0
    assert "1 unseen vacancy archived (board_disabled)" in out
    cur = dal.get_conn().cursor()
    cur.execute("SELECT status FROM vacancy WHERE title = %s", ("Pre-Reason Role",))
    assert cur.fetchone()[0] == "archived"
    cur.close()


def test_list_reports_active_companies(env, capsys):
    sources, dal = env
    dal.ensure_company("Fictive Aid Trust", status="active")
    dal.ensure_company("Dormant Labs", status="inactive")
    dal.get_conn().commit()

    assert sources.main([]) == 0
    out = capsys.readouterr().out
    assert "Fictive Aid Trust" in out  # active -> listed
    assert "Dormant Labs" not in out  # inactive -> not a live source
