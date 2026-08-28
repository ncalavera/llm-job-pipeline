"""Migration-chain parity: a fresh install on either backend must converge on
the same shape for the tables the DAL actually uses, and the REAL
``scripts/migrate.py`` chain (``sql/migrations/*``, not a synthetic temp set)
must be well-behaved -- idempotent, zero-pending after a full run -- on both.
"""

import re

import pytest

from _bootstrap import (
    PARITY_PG_URL,
    REPO_ROOT,
    bootstrap_postgres,
    bootstrap_sqlite,
    raw_migrate_fresh_sqlite,
    table_columns,
)

pytestmark = pytest.mark.parity

# Tables both dialects ship. ``dashboard_snapshot`` is Postgres-only by design
# (full mode's live-dashboard read path, never touched by SQLite/simple mode;
# not compared here). ``company_evidence`` (Postgres migration 0006 / SQLite
# baseline + migration 0007) and ``application`` (migration 0010) each ship as a
# dialect pair that SQLite/simple mode reads and writes, so both ARE compared.
SHARED_TABLES = (
    "company",
    "vacancy",
    "archived_hash",
    "board",
    "company_evidence",
    "application",
)


# ---------------------------------------------------------------------------
# migrate.py bootstrapping a TRULY fresh, never-migrated database
# ---------------------------------------------------------------------------


def test_migrate_py_converges_a_never_migrated_postgres_db(monkeypatch):
    """Postgres's migration files guard every ADD COLUMN with
    ``IF NOT EXISTS``, so replaying the full chain against a database whose
    baseline already carries some of those columns is a clean no-op. This is
    the Postgres side of the exact scenario exercised on SQLite below --
    see test_migrate_py_converges_a_never_migrated_sqlite_db."""
    dal = bootstrap_postgres(monkeypatch)  # skips if PARITY_PG_URL is unset
    import migrate

    rc = migrate.cmd_migrate(allow_destructive=False, do_backup=False)
    assert rc == 0
    dal.close_conn()


def test_migrate_py_converges_a_never_migrated_sqlite_db(tmp_path, monkeypatch):
    """`scripts/migrate.py` must bring a brand-new, never-migrated database up
    to the current schema in one clean run -- this is literally the step a
    fresh SQLite install's onboarding runs (/jobs-new step 2a, INSTALL-EASY.md
    step 2b).

    sql/schema.sqlite.sql (the frozen baseline) already declares
    us_eligibility and expiring_alerted_at -- columns that migrations 0003
    and 0005 also try to ADD. Postgres's migration files guard with
    'ADD COLUMN IF NOT EXISTS' so replaying them against an already-current
    baseline is a no-op; SQLite has no such guard for ADD COLUMN, so
    _Sqlite.run() in scripts/migrate.py catches the resulting "duplicate
    column name" error for those specific migrations and treats them as
    applied instead of aborting the run."""
    rc = raw_migrate_fresh_sqlite(monkeypatch, tmp_path)
    assert rc == 0


# ---------------------------------------------------------------------------
# Per-backend: the dialect-paired migrations produce a usable schema
# ---------------------------------------------------------------------------


def test_migrated_schema_accepts_expiring_status(backend):
    """The 'expiring' vacancy status -- added via a CHECK-constraint-widening
    migration on Postgres (0004, sqlite's frozen baseline already included it)
    -- must be insertable on both backends after the chain runs."""
    dal = backend
    company_id = dal.ensure_company("Fictive Robotics Guild", status="active")
    dal.get_conn().commit()

    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO vacancy (dedup_hash, company_id, title, first_seen, last_seen, status)
           VALUES (%s, %s, %s, %s, %s, 'expiring')""",
        ("parity-expiring-hash", company_id, "Programme Lead", "2026-01-01", "2026-01-01"),
    )
    cur.close()
    conn.commit()  # must not raise the status CHECK constraint on either backend

    assert "expiring" in dal.get_vacancy_statuses().values()


def test_migrated_schema_has_us_eligibility_column(backend):
    """vacancy.us_eligibility -- migration 0003, shipped as a dialect pair --
    must exist and round-trip on both backends after the chain runs."""
    dal = backend
    company_id = dal.ensure_company("Fictive Robotics Guild", status="active")
    dal.get_conn().commit()

    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO vacancy
               (dedup_hash, company_id, title, first_seen, last_seen, us_eligibility)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            "parity-uselig-hash",
            company_id,
            "Remote Analyst",
            "2026-01-01",
            "2026-01-01",
            "outside_us_ok",
        ),
    )
    cur.close()
    conn.commit()

    cur = conn.cursor()
    cur.execute("SELECT us_eligibility FROM vacancy WHERE dedup_hash = %s", ("parity-uselig-hash",))
    (value,) = cur.fetchone()
    cur.close()
    assert value == "outside_us_ok"


def test_migrated_schema_has_status_reason_column(backend):
    """vacancy.status_reason -- migration 0014, shipped as a dialect pair --
    must exist and round-trip on both backends after the chain runs. Written
    by archive_board_vacancies (scripts/sources.py disable-board) to record
    why a board-disable archived a still-unseen row."""
    dal = backend
    company_id = dal.ensure_company("Fictive Robotics Guild", status="active")
    dal.get_conn().commit()

    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO vacancy
               (dedup_hash, company_id, title, first_seen, last_seen, status, status_reason)
           VALUES (%s, %s, %s, %s, %s, 'archived', %s)""",
        (
            "parity-status-reason-hash",
            company_id,
            "Programme Lead",
            "2026-01-01",
            "2026-01-01",
            "board_disabled",
        ),
    )
    cur.close()
    conn.commit()

    cur = conn.cursor()
    cur.execute(
        "SELECT status_reason FROM vacancy WHERE dedup_hash = %s",
        ("parity-status-reason-hash",),
    )
    (value,) = cur.fetchone()
    cur.close()
    assert value == "board_disabled"


# ---------------------------------------------------------------------------
# Direct side-by-side: both backends bootstrapped in ONE test, columns diffed
# ---------------------------------------------------------------------------


def test_shared_table_columns_match_between_backends(tmp_path, monkeypatch):
    """Bootstraps a fresh SQLite DB (frozen baseline + full migration chain)
    and, when PARITY_PG_URL is set, a fresh Postgres DB (schema.sql + full
    migration chain) and diffs the resulting column-name sets for the tables
    both dialects ship. This is the one test in the suite that holds both
    backends side by side in the same test body instead of relying on two
    parametrized instances asserting the same literal expectation."""
    sqlite_dal = bootstrap_sqlite(monkeypatch, tmp_path)
    sqlite_cols = {t: table_columns(sqlite_dal, t) for t in SHARED_TABLES}
    sqlite_dal.close_conn()

    if not PARITY_PG_URL:
        pytest.skip(
            "PARITY_PG_URL not set -- see tests/parity/README.md for a one-shot "
            "local Postgres; the SQLite side above still ran."
        )

    pg_dal = bootstrap_postgres(monkeypatch)
    pg_cols = {t: table_columns(pg_dal, t) for t in SHARED_TABLES}
    pg_dal.close_conn()

    for table in SHARED_TABLES:
        assert sqlite_cols[table] == pg_cols[table], (
            f"{table}: columns differ after the full migration chain -- "
            f"sqlite-only={sqlite_cols[table] - pg_cols[table]} "
            f"postgres-only={pg_cols[table] - sqlite_cols[table]}"
        )


def test_parity_bootstrap_replays_every_sqlite_migration(tmp_path, monkeypatch):
    """The SQLite side of the parity comparison must replay EVERY migration the
    backend has, not a list someone remembered to update.

    This exists because it was a literal tuple ending at 0018. Migrations 0025
    (`vacancy.scoring_excluded_reason`) and 0026 (`vacancy.digest_dropped_at`)
    were added afterwards and never reached this bootstrap, so the fresh SQLite
    DB it built was missing two columns Postgres had — and
    test_shared_table_columns_match_between_backends failed on main for every
    commit in between. The list is derived now; this asserts it stays complete.
    """
    from _bootstrap import _sqlite_migration_files

    on_disk = set()
    for path in (REPO_ROOT / "sql" / "migrations").glob("*.sql"):
        m = re.match(r"^(\d+)_(.+?)(?:\.(postgres|sqlite))?\.sql$", path.name)
        if m and m.group(3) != "postgres":
            on_disk.add(m.group(1))
    replayed = set()
    for path in _sqlite_migration_files():
        replayed.add(re.match(r"^(\d+)_", path.name).group(1))

    assert replayed == on_disk, (
        "the parity bootstrap does not replay every SQLite migration -- "
        f"missing={sorted(on_disk - replayed)} unexpected={sorted(replayed - on_disk)}. "
        "A migration that is not replayed makes the SQLite side of the column "
        "diff wrong, which is how 0025 and 0026 went unnoticed."
    )


def test_bootstrapped_sqlite_carries_the_late_migration_columns(tmp_path, monkeypatch):
    """The two columns whose absence broke the cross-backend diff. Named
    explicitly so the regression has a test that says what it was about."""
    dal = bootstrap_sqlite(monkeypatch, tmp_path)
    try:
        cols = table_columns(dal, "vacancy")
    finally:
        dal.close_conn()
    for column in ("scoring_excluded_reason", "digest_dropped_at"):
        assert column in cols, f"vacancy.{column} missing from the bootstrapped SQLite DB"
