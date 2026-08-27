"""A pre-existing simple-mode SQLite database must reach the application funnel.

Migrations 0019 ('interview', 'declined') and 0020 ('test_task') shipped as
Postgres-only files. SQLite recorded both as "n/a (other dialect)" and applied
nothing, so every SQLite database created before the frozen baseline was edited
still carried the old ten-value CHECK: moving a role to 'test_task' failed with
"CHECK constraint failed" and the whole funnel was unreachable in simple mode.

0021_status_check_rebuild.sqlite.sql closes that gap the only way SQLite allows
— rebuilding the table — and declares the destructive waiver on its first line
so the rebuild's DROP does not abort an unattended `python3 scripts/migrate.py`.

Contract under test:

  * a database built with the pre-0019 CHECK rejects 'test_task' before the
    migrations and accepts it after;
  * the rebuild carries every row across and leaves application->vacancy links
    intact (the `application` FK is ON DELETE SET NULL, so a rebuild with
    foreign keys enforced would silently blank them);
  * the destructive gate lets the declared migration through, and still blocks
    the same SQL without the declaration.
"""

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = REPO_ROOT / "sql" / "migrations" / "0021_status_check_rebuild.sqlite.sql"

_WIDE_CHECK = """'test_task', 'interview',
                                            'declined', 'accepted',
                                            'expiring', 'archived'"""
_NARROW_CHECK = "'expiring', 'archived'"


def _pre_0019_schema() -> str:
    """The frozen baseline as it stood before the funnel statuses existed."""
    sql = (REPO_ROOT / "sql" / "schema.sqlite.sql").read_text(encoding="utf-8")
    assert _WIDE_CHECK in sql, "baseline CHECK moved — update this fixture"
    return sql.replace(_WIDE_CHECK, _NARROW_CHECK, 1)


def _seed(conn):
    conn.execute("INSERT INTO company (id, canonical_name) VALUES ('c1', 'Northwind Aid Trust')")
    conn.execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, first_seen, last_seen, status) "
        "VALUES ('v1', 'h1', 'c1', 'Programme Manager', '2026-08-01', '2026-08-20', 'applied')"
    )
    conn.commit()


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    """A SQLite database that predates the funnel statuses, holding one role
    the user already applied to."""
    db_file = tmp_path / "jobsearch.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(_pre_0019_schema())
    _seed(conn)
    conn.close()

    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "migrate",
    ):
        sys.modules.pop(mod, None)
    return db_file


def _set_status(db_file, status):
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("UPDATE vacancy SET status = ? WHERE id = 'v1'", (status,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The gap, and its close
# ---------------------------------------------------------------------------


def test_legacy_sqlite_db_rejects_test_task_before_migrating(legacy_db):
    with pytest.raises(sqlite3.IntegrityError):
        _set_status(legacy_db, "test_task")


@pytest.mark.parametrize("status", ["test_task", "interview", "declined"])
def test_legacy_sqlite_db_accepts_the_funnel_statuses_after_migrating(
    legacy_db, monkeypatch, status
):
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import migrate

    importlib.reload(migrate)

    rc = migrate.cmd_migrate(allow_destructive=False, do_backup=False)
    assert rc == 0, "the rebuild must apply unattended, without --allow-destructive"

    _set_status(legacy_db, status)
    conn = sqlite3.connect(str(legacy_db))
    try:
        row = conn.execute("SELECT title, status FROM vacancy WHERE id = 'v1'").fetchone()
    finally:
        conn.close()
    assert row == ("Programme Manager", status), "the rebuild must carry every row across"


def test_rebuild_keeps_application_links(legacy_db, monkeypatch):
    """The `application` FK is ON DELETE SET NULL: rebuilding with foreign keys
    enforced would blank vacancy_id for every application on the board."""
    import db_backend

    importlib.reload(db_backend)
    import migrate

    importlib.reload(migrate)
    assert migrate.cmd_migrate(allow_destructive=False, do_backup=False) == 0

    conn = sqlite3.connect(str(legacy_db))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO application (id, vacancy_id, company_id, status) "
            "VALUES ('a1', 'v1', 'c1', 'applied')"
        )
        conn.commit()
        # Re-run the rebuild with that row in place — the same statements the
        # runner executes, against a database that now holds an application.
        conn.executescript(MIGRATION.read_text(encoding="utf-8"))
        conn.commit()
        link = conn.execute("SELECT vacancy_id FROM application WHERE id = 'a1'").fetchone()
        vacancy = conn.execute("SELECT id FROM vacancy WHERE id = 'v1'").fetchone()
    finally:
        conn.close()

    assert vacancy == ("v1",)
    assert link == ("v1",), "the rebuild blanked the application's vacancy link"


# ---------------------------------------------------------------------------
# The destructive gate
# ---------------------------------------------------------------------------


def test_gate_waives_the_declared_migration_and_still_blocks_it_undeclared(tmp_path):
    import migrate

    importlib.reload(migrate)
    sql = MIGRATION.read_text(encoding="utf-8")

    loaded = [("0021", "status_check_rebuild", MIGRATION, sql)]
    blocked, waived = migrate._scan_destructive(loaded)
    assert blocked == []
    assert len(waived) == 1
    assert "rebuilds vacancy" in waived[0]

    undeclared = tmp_path / "0021_undeclared.sqlite.sql"
    undeclared.write_text(sql.split("\n", 1)[1], encoding="utf-8")
    blocked, waived = migrate._scan_destructive(
        [("0021", "undeclared", undeclared, undeclared.read_text(encoding="utf-8"))]
    )
    assert waived == []
    assert len(blocked) == 1, "a rebuild that declares nothing must still abort the run"
