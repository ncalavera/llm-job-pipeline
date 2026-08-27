"""Tests for scripts/migrate.py — the data-safety guarantee of the pipeline.

The migration runner is the one piece of the system that is allowed to touch
the live schema, so its safety contract (backup, destructive guard, SQLite
auto-restore on failure, idempotency) is what these tests lock down.

Everything runs OFFLINE on the local SQLite backend. We never touch the repo's
real ``sql/migrations/`` directory: ``MIGRATIONS_DIR`` and ``BACKUPS_DIR`` are
monkeypatched to temp dirs, and ``cmd_migrate`` / ``cmd_status`` are driven
in-process for fast, precise assertions.
"""

import importlib
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

# Real repo root — used only to copy the frozen baseline schema into the temp
# project layout so the runner can bootstrap a fresh DB hermetically.
_REAL_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixture — a fresh migrate module bound to an isolated temp SQLite DB, with
# the migrations and backups directories redirected to temp paths.
# ---------------------------------------------------------------------------


@pytest.fixture()
def mig(tmp_path, monkeypatch):
    """Reload migrate.py against a temp SQLite DB + temp migrations/backups dirs.

    Yields a small namespace with the module, the migrations dir, the backups
    dir and the live DB path so each test can populate migrations and assert.
    """
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    # Rebuild the backend chain so IS_SQLITE / sqlite_db_path pick up the env.
    for mod in (
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "migrate",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "migrate tests must run on SQLite"

    import migrate

    importlib.reload(migrate)

    # Build a hermetic temp project layout so MIGRATIONS_DIR, BACKUPS_DIR and the
    # baseline-schema lookup all live under tmp_path. PROJECT_ROOT is repointed
    # at tmp_path so cmd_migrate's ``backup.relative_to(PROJECT_ROOT)`` print
    # works (the backups dir is now genuinely under the project root). The frozen
    # baseline schema is copied in so _ensure_baseline can bootstrap a fresh DB.
    proj = tmp_path
    (proj / "sql").mkdir()
    shutil.copy2(_REAL_ROOT / "sql" / "schema.sqlite.sql", proj / "sql" / "schema.sqlite.sql")
    migrations_dir = proj / "sql" / "migrations"
    migrations_dir.mkdir()
    backups_dir = proj / "data" / "backups"
    monkeypatch.setattr(migrate, "PROJECT_ROOT", proj)
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", migrations_dir)
    monkeypatch.setattr(migrate, "BACKUPS_DIR", backups_dir)

    ns = type("MigEnv", (), {})()
    ns.m = migrate
    ns.migrations_dir = migrations_dir
    ns.backups_dir = backups_dir
    ns.db_path = db_file
    return ns


def _columns(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _applied_versions(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _table_exists(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def _logical_snapshot(db_path):
    """Full schema + data dump — a content-level fingerprint of the DB.

    Used instead of raw bytes for the restore test: the SQLite online-backup
    API writes a fresh file whose header change-counter differs from the
    original, so the *bytes* of the live file legitimately differ after a
    restore even though the schema and every row are identical. The safety
    guarantee the runner makes is logical, not byte-for-byte, equality of
    content — which an ``iterdump()`` captures exactly.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Baseline schema bootstrap on a fresh DB
# ---------------------------------------------------------------------------


def test_fresh_db_gets_baseline_schema(mig):
    """A brand-new empty SQLite DB → opening the runner applies the baseline,
    so the ``company`` table exists afterward (zero pending migrations)."""
    assert not mig.db_path.exists()
    rc = mig.m.cmd_migrate(allow_destructive=False, do_backup=True)
    assert rc == 0
    assert _table_exists(mig.db_path, "company")
    assert _table_exists(mig.db_path, "vacancy")
    # No migration files → ledger has no version rows but the table exists.
    assert _applied_versions(mig.db_path) == set()


# ---------------------------------------------------------------------------
# Pending migration applied + recorded; second run is idempotent
# ---------------------------------------------------------------------------


def test_pending_migration_applied_and_recorded(mig):
    (mig.migrations_dir / "0001_add_foo.sql").write_text(
        "ALTER TABLE company ADD COLUMN foo_flag INTEGER;\n", encoding="utf-8"
    )
    rc = mig.m.cmd_migrate(allow_destructive=False, do_backup=True)
    assert rc == 0
    assert "foo_flag" in _columns(mig.db_path, "company")
    assert "0001" in _applied_versions(mig.db_path)


def test_second_run_is_idempotent(mig):
    (mig.migrations_dir / "0001_add_foo.sql").write_text(
        "ALTER TABLE company ADD COLUMN foo_flag INTEGER;\n", encoding="utf-8"
    )
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=True) == 0
    backups_after_first = sorted(mig.backups_dir.glob("*")) if mig.backups_dir.exists() else []

    # Second run: nothing pending → no-op, no new backup, ledger unchanged.
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=True) == 0
    assert _applied_versions(mig.db_path) == {"0001"}
    backups_after_second = sorted(mig.backups_dir.glob("*")) if mig.backups_dir.exists() else []
    assert backups_after_second == backups_after_first, (
        "an up-to-date run must not create another backup"
    )


# ---------------------------------------------------------------------------
# --status lists pending vs applied without changing the DB
# ---------------------------------------------------------------------------


def test_status_lists_without_mutating(mig, capsys):
    # Bootstrap the baseline first so the DB file exists and is stable.
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    (mig.migrations_dir / "0001_add_foo.sql").write_text(
        "ALTER TABLE company ADD COLUMN foo_flag INTEGER;\n", encoding="utf-8"
    )

    before = mig.db_path.read_bytes()
    rc = mig.m.cmd_status()
    after = mig.db_path.read_bytes()

    assert rc == 0
    out = capsys.readouterr().out
    assert "PENDING" in out
    assert "0001" in out
    # --status must not apply anything.
    assert "foo_flag" not in _columns(mig.db_path, "company")
    assert _applied_versions(mig.db_path) == set()
    assert before == after, "--status must not modify the database file"


def test_status_marks_applied(mig, capsys):
    (mig.migrations_dir / "0001_add_foo.sql").write_text(
        "ALTER TABLE company ADD COLUMN foo_flag INTEGER;\n", encoding="utf-8"
    )
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    assert mig.m.cmd_status() == 0
    out = capsys.readouterr().out
    assert "applied" in out
    assert "0 pending" in out


# ---------------------------------------------------------------------------
# Destructive guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "destructive_sql",
    [
        "DROP TABLE company;",
        "DELETE FROM company;",
        "TRUNCATE company;",
    ],
)
def test_destructive_migration_aborts_and_no_op(mig, destructive_sql):
    """A migration with a destructive statement aborts with a nonzero exit and
    does NOT modify the DB (no ledger row, schema untouched)."""
    # Establish a stable baseline DB first.
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    before_bytes = mig.db_path.read_bytes()

    (mig.migrations_dir / "0001_danger.sql").write_text(destructive_sql + "\n", encoding="utf-8")

    rc = mig.m.cmd_migrate(allow_destructive=False, do_backup=True)
    assert rc == 2, "destructive run must exit nonzero (2)"
    # DB unchanged byte-for-byte; ledger has no 0001 row.
    assert mig.db_path.read_bytes() == before_bytes
    assert "0001" not in _applied_versions(mig.db_path)
    # And the company table still exists / has data integrity.
    assert _table_exists(mig.db_path, "company")


def test_destructive_migration_runs_with_allow_flag(mig):
    """With --allow-destructive the same migration is permitted and recorded."""
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    # A safe-ish destructive statement (DELETE on an empty table) so we can
    # assert it actually ran and was recorded.
    (mig.migrations_dir / "0001_cleanup.sql").write_text(
        "DELETE FROM company WHERE 1=0;\n", encoding="utf-8"
    )
    rc = mig.m.cmd_migrate(allow_destructive=True, do_backup=True)
    assert rc == 0
    assert "0001" in _applied_versions(mig.db_path)


# ---------------------------------------------------------------------------
# Automatic backup
# ---------------------------------------------------------------------------


def test_backup_created_for_pending_run(mig):
    """A run with pending work and an existing DB creates a backup file under
    the backups dir."""
    # Bootstrap baseline (DB now exists and is non-empty).
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    assert mig.db_path.exists() and mig.db_path.stat().st_size > 0

    (mig.migrations_dir / "0001_add_foo.sql").write_text(
        "ALTER TABLE company ADD COLUMN foo_flag INTEGER;\n", encoding="utf-8"
    )
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=True) == 0

    backups = list(mig.backups_dir.glob("*.db"))
    assert backups, "a pending run must drop a backup under data/backups/"
    # The backup is a usable SQLite file with the PRE-migration schema
    # (no foo_flag column yet) — proving it was taken before applying.
    bconn = sqlite3.connect(str(backups[0]))
    try:
        cols = {r[1] for r in bconn.execute("PRAGMA table_info(company)")}
    finally:
        bconn.close()
    assert "foo_flag" not in cols


# ---------------------------------------------------------------------------
# SQLite auto-restore on failure — the failed run must be a clean no-op
# ---------------------------------------------------------------------------


def test_failed_migration_restores_db_clean_no_op(mig):
    """A migration that adds a column and THEN runs invalid SQL must leave the
    DB exactly as before the run: the added column is ABSENT, the ledger is
    unchanged, and the full schema+data content is identical. SQLite DDL is not
    transactional, so this proves the backup-restore safety net turns a failed
    run into a clean no-op."""
    # Stable baseline first.
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    before_snapshot = _logical_snapshot(mig.db_path)
    before_versions = _applied_versions(mig.db_path)

    # First statement is valid (adds a column), second is invalid SQL → throws
    # mid-script, after the DDL already half-applied.
    (mig.migrations_dir / "0001_broken.sql").write_text(
        "ALTER TABLE company ADD COLUMN halfway INTEGER;\nTHIS IS NOT VALID SQL;\n",
        encoding="utf-8",
    )

    rc = mig.m.cmd_migrate(allow_destructive=False, do_backup=True)
    assert rc == 1, "a failed migration must return exit code 1"

    # The added column must be ABSENT — restore reverted the half-applied DDL.
    assert "halfway" not in _columns(mig.db_path, "company")
    # Ledger unchanged.
    assert _applied_versions(mig.db_path) == before_versions
    assert "0001" not in _applied_versions(mig.db_path)
    # And the full schema+data content is identical to the pre-run snapshot.
    assert _logical_snapshot(mig.db_path) == before_snapshot


def test_failed_run_without_backup_still_returns_failure(mig):
    """--no-backup on a failing migration can't auto-restore, but the runner
    must still surface the failure (exit 1) rather than swallow it."""
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    (mig.migrations_dir / "0001_broken.sql").write_text(
        "ALTER TABLE company ADD COLUMN x INTEGER;\nBROKEN SQL HERE;\n",
        encoding="utf-8",
    )
    rc = mig.m.cmd_migrate(allow_destructive=False, do_backup=False)
    assert rc == 1


def test_duplicate_column_guard_is_scoped_to_known_versions(mig):
    """_Sqlite.run()'s "duplicate column name" catch-and-skip exists ONLY for
    migrations 0003 and 0005, whose columns are already declared in the frozen
    baseline schema (see test_migrate_py_converges_a_never_migrated_sqlite_db
    in tests/parity/test_migrations_parity.py for that positive path). Any
    OTHER version that raises the same SQLite error — e.g. a migration that
    re-adds a column it already added in an earlier statement — must NOT be
    swallowed: it has to propagate like any other OperationalError, trip the
    restore-on-failure safety net, and never reach the ledger as applied."""
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    before_snapshot = _logical_snapshot(mig.db_path)
    before_versions = _applied_versions(mig.db_path)

    # Two statements adding the SAME column -> the second raises "duplicate
    # column name" on version 0001, which is outside the (0003, 0005) allowlist.
    (mig.migrations_dir / "0001_dup.sql").write_text(
        "ALTER TABLE company ADD COLUMN dup_flag INTEGER;\n"
        "ALTER TABLE company ADD COLUMN dup_flag INTEGER;\n",
        encoding="utf-8",
    )

    rc = mig.m.cmd_migrate(allow_destructive=False, do_backup=True)
    assert rc == 1, "duplicate column outside the allowlist must fail, not be swallowed"

    # Restore reverted the half-applied DDL (first ALTER did succeed before
    # the second one raised) -- same clean-no-op guarantee as any other failure.
    assert "dup_flag" not in _columns(mig.db_path, "company")
    assert _applied_versions(mig.db_path) == before_versions
    assert "0001" not in _applied_versions(mig.db_path)
    assert _logical_snapshot(mig.db_path) == before_snapshot


@pytest.mark.parametrize("version", ["0009", "0011", "0025", "0026"])
def test_half_migrated_single_add_column_is_idempotent(mig, version, capsys):
    """0009 (scored_by) / 0011 (board.enabled) / 0025 (scoring_excluded_reason)
    / 0026 (digest_dropped_at) are single
    ADD COLUMN migrations with no SQLite ``IF NOT EXISTS``. On a HALF-MIGRATED
    DB — the column exists but its ledger row was lost — re-running used to hit
    'duplicate column name', abort, auto-restore, and stay stuck one migration
    short on EVERY subsequent run. These versions are now in the swallow-on-
    duplicate allowlist, so the re-run records them and moves on."""
    col = f"col_{version}"
    (mig.migrations_dir / f"{version}_add.sqlite.sql").write_text(
        f"ALTER TABLE vacancy ADD COLUMN {col} TEXT;\n", encoding="utf-8"
    )
    # First apply: column added, version recorded.
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    assert col in _columns(mig.db_path, "vacancy")
    assert version in _applied_versions(mig.db_path)

    # Simulate the half-migrated state: column present, ledger row gone.
    conn = sqlite3.connect(str(mig.db_path))
    conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
    conn.commit()
    conn.close()
    assert version not in _applied_versions(mig.db_path)

    # Re-run: the duplicate ADD COLUMN is treated as already-applied, not a stuck
    # abort. Twice, to prove it converges rather than looping on failure.
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    assert version in _applied_versions(mig.db_path)
    assert "column already present" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# A version number already recorded as "n/a (other dialect)" must never be
# reused for a later dialect-specific migration -- it would silently never run
# (the exact bug fixed by renumbering 0006_company_evidence.sqlite.sql to 0007).
# ---------------------------------------------------------------------------


def test_reusing_a_number_recorded_as_other_dialect_never_applies(mig):
    """Simulates the real-world timeline: a migration ships Postgres-only
    first, so every SQLite DB that runs migrate.py in that window records it
    as "n/a (other dialect)". If a LATER SQLite file reuses that SAME number,
    the version is already in the ledger -- cmd_migrate sees nothing pending
    and the SQLite migration never runs, no matter how many times it is
    invoked afterward."""
    # (a) old code: a Postgres-only migration. Recorded as n/a on this SQLite DB.
    (mig.migrations_dir / "0001_evidence.postgres.sql").write_text("SELECT 1;\n", encoding="utf-8")
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    assert "0001" in _applied_versions(mig.db_path)

    # (b) BUGGY upgrade: the SQLite counterpart reuses number 0001. It never
    # runs -- 0001 is already applied, so it is invisible.
    (mig.migrations_dir / "0001_evidence.sqlite.sql").write_text(
        "ALTER TABLE company ADD COLUMN evidence_flag INTEGER;\n", encoding="utf-8"
    )
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    assert "evidence_flag" not in _columns(mig.db_path, "company")

    # (b) FIXED upgrade: ship the SQLite counterpart under the next FREE
    # number instead of reusing 0001.
    (mig.migrations_dir / "0001_evidence.sqlite.sql").unlink()
    (mig.migrations_dir / "0002_evidence.sqlite.sql").write_text(
        "ALTER TABLE company ADD COLUMN evidence_flag INTEGER;\n", encoding="utf-8"
    )
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    assert "evidence_flag" in _columns(mig.db_path, "company")
    assert _applied_versions(mig.db_path) == {"0001", "0002"}

    # (c) idempotent replay.
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    assert "evidence_flag" in _columns(mig.db_path, "company")


# ---------------------------------------------------------------------------
# Multiple pending migrations apply in order
# ---------------------------------------------------------------------------


def test_multiple_migrations_apply_in_order(mig):
    (mig.migrations_dir / "0001_a.sql").write_text(
        "ALTER TABLE company ADD COLUMN col_a INTEGER;\n", encoding="utf-8"
    )
    (mig.migrations_dir / "0002_b.sql").write_text(
        "ALTER TABLE company ADD COLUMN col_b INTEGER;\n", encoding="utf-8"
    )
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=True) == 0
    cols = _columns(mig.db_path, "company")
    assert {"col_a", "col_b"} <= cols
    assert _applied_versions(mig.db_path) == {"0001", "0002"}


# ---------------------------------------------------------------------------
# Destructive-statement gate: precise over/under-match (code-review fixes)
# ---------------------------------------------------------------------------


def test_update_set_backfill_is_not_destructive(mig):
    """The canonical additive pattern — ADD COLUMN then backfill with UPDATE
    SET — must run WITHOUT --allow-destructive (UPDATE is not data loss)."""
    (mig.migrations_dir / "0001_backfill.sql").write_text(
        "ALTER TABLE company ADD COLUMN region TEXT;\nUPDATE company SET region = 'unknown';\n",
        encoding="utf-8",
    )
    rc = mig.m.cmd_migrate(allow_destructive=False, do_backup=True)
    assert rc == 0
    assert "region" in _columns(mig.db_path, "company")
    assert "0001" in _applied_versions(mig.db_path)


def test_destructive_keyword_in_comment_is_ignored(mig):
    """A DROP/DELETE mentioned inside a comment or string literal must not trip
    the gate — only real statements count."""
    (mig.migrations_dir / "0001_safe.sql").write_text(
        "-- this replaces the old DROP TABLE company logic\n"
        "ALTER TABLE company ADD COLUMN note TEXT;\n",
        encoding="utf-8",
    )
    rc = mig.m.cmd_migrate(allow_destructive=False, do_backup=True)
    assert rc == 0
    assert "note" in _columns(mig.db_path, "company")


def test_drop_schema_is_blocked(mig):
    """DROP SCHEMA / DROP DATABASE etc. (not just DROP TABLE/COLUMN) must be
    caught by the destructive gate."""
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    (mig.migrations_dir / "0001_drop.sql").write_text(
        "DROP SCHEMA public CASCADE;\n", encoding="utf-8"
    )
    rc = mig.m.cmd_migrate(allow_destructive=False, do_backup=True)
    assert rc == 2  # aborted by the gate
    assert _applied_versions(mig.db_path) == set()


# ---------------------------------------------------------------------------
# --baseline: adopt an already-current DB without running migrations
# ---------------------------------------------------------------------------


def test_baseline_records_without_running(mig):
    """--baseline marks pending migrations applied WITHOUT executing their SQL,
    so a column a migration *would* add is absent but the version is recorded."""
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0
    (mig.migrations_dir / "0001_add_thing.sql").write_text(
        "ALTER TABLE company ADD COLUMN thing INTEGER;\n", encoding="utf-8"
    )
    rc = mig.m.cmd_baseline()
    assert rc == 0
    assert "0001" in _applied_versions(mig.db_path)
    assert "thing" not in _columns(mig.db_path, "company")  # recorded, not run
    # A subsequent normal run sees nothing pending.
    assert mig.m.cmd_migrate(allow_destructive=False, do_backup=False) == 0


# ---------------------------------------------------------------------------
# Postgres init ordering (fake connection — no live Supabase).
#
# _connect_supabase leaves a transaction open from its identity SELECT.
# psycopg2 forbids assigning `autocommit` while a transaction is in progress
# ("set_session cannot be used inside a transaction"), so _Postgres.__init__
# must rollback() BEFORE it flips autocommit. These fakes model that one
# invariant so the ordering is locked without touching a real database.
# ---------------------------------------------------------------------------


class _FakePgCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.calls.append(("execute", sql.strip().split()[0].upper()))
        self._conn._open_txn = True  # any statement opens a transaction
        self._conn._last_sql = sql

    def fetchone(self):
        # to_regclass(...) → ledger absent (None); any other SELECT → benign row.
        if "to_regclass" in (self._conn._last_sql or ""):
            return (None,)
        return (1,)

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakePgConn:
    """psycopg2-connection stand-in enforcing the mid-transaction autocommit
    guard. Starts life mid-transaction, mirroring _connect_supabase's
    uncommitted identity SELECT."""

    def __init__(self):
        self.calls = []
        self._open_txn = True  # identity SELECT left a txn open
        self._autocommit = False
        self._last_sql = None

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        if self._open_txn:
            raise RuntimeError("set_session cannot be used inside a transaction")
        self.calls.append(("autocommit", value))
        self._autocommit = value

    def cursor(self, cursor_factory=None):
        return _FakePgCursor(self)

    def rollback(self):
        self.calls.append(("rollback", None))
        self._open_txn = False

    def commit(self):
        self.calls.append(("commit", None))
        self._open_txn = False

    def close(self):
        self.calls.append(("close", None))


def test_postgres_init_rolls_back_before_flipping_autocommit(mig, monkeypatch):
    """_Postgres.__init__ must clear the open identity-SELECT transaction with
    rollback() before assigning autocommit, or psycopg2 raises "set_session
    cannot be used inside a transaction". The fake connection reproduces that
    guard, so constructing _Postgres without the rollback would raise here."""
    import db_backend

    fake = _FakePgConn()
    monkeypatch.setattr(db_backend, "_connect_supabase", lambda: fake)

    pg = mig.m._Postgres()  # must not raise

    kinds = [c[0] for c in fake.calls]
    assert "rollback" in kinds, fake.calls
    assert "autocommit" in kinds, fake.calls
    # The regression lock: rollback strictly precedes the autocommit flip.
    assert kinds.index("rollback") < kinds.index("autocommit")
    # Ledger bootstrap ran and was committed inside the fresh transaction.
    assert ("commit", None) in fake.calls
    # Ledger absent (to_regclass → None) → treated as an existing-DB adoption.
    assert pg.adopted_existing is True
