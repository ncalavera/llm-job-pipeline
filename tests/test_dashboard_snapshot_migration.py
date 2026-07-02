"""U1 — the dashboard_snapshot table: migration + baseline parity.

The full-mode live dashboard reads its payload from a single Supabase row. This
locks down that the migration creating that row's table is present, well-formed,
non-destructive, and mirrored in the frozen baseline schema so a fresh install
and a migrated install converge.

The migration is Postgres-only (JSONB / TIMESTAMPTZ / NOW()), so we validate its
SQL by content + the migrate runner's own destructive scanner rather than running
it on the SQLite test backend.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "sql" / "migrations" / "0001_dashboard_snapshot.postgres.sql"
_SCHEMA = _ROOT / "sql" / "schema.sql"


def _migrate_module():
    sys.path.insert(0, str(_ROOT / "scripts"))
    import migrate  # noqa: E402

    return migrate


def test_migration_file_exists_and_is_postgres_only():
    assert _MIGRATION.exists(), "0001_dashboard_snapshot.postgres.sql must exist"
    # No SQLite variant — simple mode never reads this table.
    assert not (_MIGRATION.parent / "0001_dashboard_snapshot.sqlite.sql").exists()
    assert not (_MIGRATION.parent / "0001_dashboard_snapshot.sql").exists()


def test_migration_creates_snapshot_table_shape():
    sql = _MIGRATION.read_text(encoding="utf-8")
    lowered = sql.lower()
    assert "create table if not exists dashboard_snapshot" in lowered
    assert "payload" in lowered and "jsonb" in lowered
    assert "updated_at" in lowered and "timestamptz" in lowered
    assert "primary key" in lowered
    # JSONB payload must be NOT NULL so a row always carries data.
    assert "payload" in lowered and "not null" in lowered


def test_migration_is_not_destructive():
    """The migrate runner's own gate must consider this migration safe — a
    snapshot table create must never trip the DROP/DELETE/TRUNCATE guard."""
    migrate = _migrate_module()
    sql = _MIGRATION.read_text(encoding="utf-8")
    stripped = migrate._strip_sql_noise(sql)
    assert migrate._DESTRUCTIVE_RE.search(stripped) is None


def test_schema_baseline_mirrors_snapshot_table():
    """Fresh installs build from schema.sql; it must define the same table so a
    fresh DB and a migrated DB converge."""
    schema = _SCHEMA.read_text(encoding="utf-8").lower()
    assert "create table if not exists dashboard_snapshot" in schema
    assert "payload" in schema and "jsonb" in schema
