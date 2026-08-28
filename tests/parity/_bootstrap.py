"""Shared bootstrap helpers for the SQLite<->Postgres backend-parity suite.

Not a test module itself (no ``test_`` prefix — pytest never collects it).
Imported by ``conftest.py`` (the parametrized ``backend`` fixture) and by any
test that needs a direct side-by-side comparison instead.

Both backends are brought up the way a real install actually gets built: the
frozen baseline schema, then the REAL ``scripts/migrate.py`` chain on top —
never a synthetic temp migrations directory (that is what
``tests/test_migrate.py`` exercises, to lock down the runner's mechanics).
This suite instead proves the actual ``sql/migrations/*`` files converge to
the same shape on both backends.
"""

import importlib
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_SQL = REPO_ROOT / "sql" / "schema.sql"

#: One-shot local Postgres for the parity suite (see tests/parity/README.md).
#: Never a hosted project — the guard below refuses anything that looks like
#: one, and the default (empty) skips the Postgres half entirely.
PARITY_PG_URL = os.environ.get("PARITY_PG_URL", "").strip()

if PARITY_PG_URL and "supabase" in PARITY_PG_URL.lower():
    raise RuntimeError(
        "PARITY_PG_URL looks like a hosted Supabase URL. The backend-parity "
        "suite must only run against a throwaway local Postgres (docker run "
        "postgres:16, or a CI service container) -- refusing to start. See "
        "tests/parity/README.md."
    )

# Modules whose state depends on the active backend; reloaded fresh on every
# switch. Mirrors the pattern already used by tests/test_e2e_pipeline.py,
# tests/test_archive_restore.py, tests/test_migrate.py, etc.
_CHAIN_MODULES = (
    "database_supabase",
    "config",
    "company_registry",
    "db_conn",
    "db_backend",
    "migrate",
)


def _reload_chain():
    for mod in _CHAIN_MODULES:
        sys.modules.pop(mod, None)


def _sqlite_migration_files():
    """Every migration file the SQLite backend should replay on top of the
    frozen baseline, in version order. A dialect-specific ``.sqlite.sql`` wins
    over the generic ``.sql`` for a version; a Postgres-only version is simply
    absent here, which is correct — it has nothing to apply."""
    by_version: dict[str, Path] = {}
    for path in sorted((REPO_ROOT / "sql" / "migrations").glob("*.sql")):
        m = re.match(r"^(\d+)_(.+?)(?:\.(postgres|sqlite))?\.sql$", path.name)
        if not m:
            continue
        version, dialect = m.group(1), m.group(3)
        if dialect == "postgres":
            continue
        # A .sqlite.sql beats the generic .sql for the same version.
        if dialect == "sqlite" or version not in by_version:
            by_version[version] = path
    return [by_version[v] for v in sorted(by_version)]


def bootstrap_sqlite(monkeypatch, tmp_path):
    """Fresh temp-file SQLite DB, brought up the way the rest of this repo's
    test suite already does it: a bare first connection (auto-applies the
    frozen baseline), then the migrations SQLite genuinely still needs on
    top of that baseline -- the board table (0002), the scored_by provenance
    column (0009), the application table (0010), the board.enabled
    persistence flag (0011), the vacancy.source_board provenance column
    (0013), and the vacancy.status_reason column (0014), none of which the
    frozen baseline declares.

    Deliberately NOT routed through migrate.py's ledger-driven replay: that
    path now works (migrate.py's SQLite runner tolerates the duplicate-column
    overlap between sql/schema.sqlite.sql and migrations 0003/0005), but it
    also runs a pre-migration backup via the SQLite online-backup API on
    every call. This helper is the cheap path used by every OTHER parity
    test: a bare connection (auto-applies the frozen baseline) plus the
    post-baseline migrations applied directly -- no backup, no full ledger
    replay. See raw_migrate_fresh_sqlite() below and
    test_migrations_parity.py::test_migrate_py_converges_a_never_migrated_sqlite_db
    for the real, unguarded migrate.py path.
    """
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    _reload_chain()

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "bootstrap_sqlite must land on the SQLite backend"

    conn = db_backend.get_conn()  # first connection -> auto-applies schema.sqlite.sql
    # Apply via the RAW sqlite3 connection's executescript, the way migrate.py's
    # _Sqlite.run does: a migration file can hold several statements (0010 =
    # CREATE TABLE + indexes) and the psycopg2-compatible cursor.execute runs
    # only one statement at a time.
    #
    # The list is DERIVED, not hand-written. It used to be a literal tuple
    # ending at 0018, so migrations 0025 and 0026 (added later) never reached
    # the SQLite side and this file silently made the cross-backend column
    # diff fail on main. A list that must be edited by hand every time someone
    # adds a migration will be forgotten again; globbing cannot be.
    #
    # Files are applied in version order. The frozen baseline already declares
    # some of what the early migrations add (0003/0005), so a duplicate column
    # is tolerated here exactly as migrate.py tolerates it — that overlap is
    # the baseline being ahead, not a broken migration.
    raw = conn._conn
    for path in _sqlite_migration_files():
        try:
            raw.executescript(path.read_text(encoding="utf-8"))
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise AssertionError(f"parity bootstrap: {path.name} failed -- {e}") from e
    conn.commit()

    import database_supabase as dal

    return dal


def raw_migrate_fresh_sqlite(monkeypatch, tmp_path):
    """The UNGUARDED bootstrap: a truly fresh temp SQLite file run straight
    through migrate.py's own ledger-driven replay of the real
    sql/migrations/* chain -- exactly what a first-time `python3
    scripts/migrate.py` run (INSTALL-EASY.md 2b, /jobs-new step 2a) does.

    Used only by the dedicated bug-report test; every other test uses
    bootstrap_sqlite()'s workaround above so it isn't collateral damage.
    Returns migrate.cmd_migrate()'s exit code (0 = clean bootstrap).
    """
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    _reload_chain()

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "raw_migrate_fresh_sqlite must land on the SQLite backend"

    import migrate

    importlib.reload(migrate)
    return migrate.cmd_migrate(allow_destructive=False, do_backup=False)


def _apply_postgres_baseline():
    """Run the frozen sql/schema.sql once. Idempotent (CREATE ... IF NOT
    EXISTS throughout), so re-running it against an already-baselined DB in a
    later test is a cheap no-op."""
    import db_backend

    conn = db_backend._connect_supabase()
    try:
        cur = conn.cursor()
        cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        cur.close()
        conn.commit()
    finally:
        conn.close()


def bootstrap_postgres(monkeypatch):
    """One-shot local Postgres: schema.sql baseline + the real migration
    chain on top, matching the documented fresh-install flow (INSTALL.md).
    Skips the calling test with a clear reason when PARITY_PG_URL is unset.
    """
    if not PARITY_PG_URL:
        pytest.skip(
            "PARITY_PG_URL not set -- the Postgres half of the parity suite "
            "needs a one-shot local Postgres (see tests/parity/README.md); "
            "the SQLite half still runs on every plain `pytest`."
        )
    monkeypatch.setenv("SUPABASE_DB_URL", PARITY_PG_URL)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.delenv("JOBSEARCH_DB_PATH", raising=False)
    _reload_chain()

    import db_backend

    importlib.reload(db_backend)
    assert not db_backend.IS_SQLITE, "bootstrap_postgres must land on the Postgres backend"

    _apply_postgres_baseline()

    import migrate

    importlib.reload(migrate)
    # allow_destructive: this is a THROWAWAY one-shot Postgres that was created
    # seconds ago from the frozen baseline — there is no data a destructive
    # migration could destroy. Without it, any migration containing a reviewed
    # DROP (e.g. 0018's dead-column cleanup, a no-op on a fresh baseline) aborts
    # the whole parity suite. Prod keeps the interactive gate: jobs-update runs
    # migrate.py without this flag.
    rc = migrate.cmd_migrate(allow_destructive=True, do_backup=False)
    assert rc == 0, "Postgres migration bootstrap failed"

    import database_supabase as dal

    truncate_postgres(dal)
    return dal


def truncate_postgres(dal):
    """Isolate each test from the last: wipe rows, keep schema + migration
    ledger (so the migration chain is applied once per session, not per
    test)."""
    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE archived_hash, vacancy, company, board CASCADE")
    cur.close()
    conn.commit()


def table_columns(dal, table):
    """Column-name set for `table`, read the native way for the active
    backend (PRAGMA vs information_schema)."""
    import db_backend

    conn = dal.get_conn()
    cur = conn.cursor()
    if db_backend.IS_SQLITE:
        cur.execute(f"PRAGMA table_info({table})")
        names = {row[1] for row in cur.fetchall()}
    else:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        names = {row[0] for row in cur.fetchall()}
    cur.close()
    return names
