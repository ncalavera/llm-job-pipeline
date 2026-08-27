"""Schema migration runner — applies pending SQL deltas, never loses data.

Contract (read this before adding a migration):

* ``sql/schema.sql`` / ``sql/schema.sqlite.sql`` are the **frozen baseline** —
  the schema as it stood when the migration system was introduced. Do NOT edit
  them to describe new changes. They exist only to build a brand-new database.
* Every schema change after the baseline is a new **numbered migration** under
  ``sql/migrations/``. Both a fresh install (baseline schema -> all migrations)
  and an existing install (only the new migrations) converge on the same shape,
  so there is no double-apply and no ambiguity.

Migration files::

    sql/migrations/0001_add_foo_column.sql            # portable SQL, both backends
    sql/migrations/0002_add_index.postgres.sql        # Postgres-only variant
    sql/migrations/0002_add_index.sqlite.sql          # SQLite-only variant

For one version number a dialect-specific file (``.postgres.sql`` /
``.sqlite.sql``) wins over the generic ``.sql`` for the active backend. If only
the *other* dialect exists for a version, that version is recorded as applied
without running anything (it does not apply to this backend).

Data safety — the whole point of this runner:

* **Automatic backup before every run** that has pending work. SQLite is copied
  with the online-backup API (WAL-safe, consistent) into ``data/backups/``;
  Postgres is dumped with ``pg_dump`` when that binary is available.
* **SQLite auto-restore on failure.** SQLite DDL is not transactional, so a
  migration that fails halfway would leave a partial schema. If anything throws,
  the live database is restored byte-for-byte from the pre-run backup, then an
  integrity check confirms it. Net effect: a failed run is a no-op.
* **Postgres is transactional per migration** — each migration and its ledger
  row commit together, so a failure rolls that migration back cleanly; earlier
  migrations stay applied. The pg_dump is an extra belt-and-braces snapshot.
* **Destructive statements are blocked by default.** A migration containing
  DROP (TABLE/COLUMN/VIEW/INDEX/SCHEMA/DATABASE/…) / TRUNCATE / DELETE FROM /
  ALTER ... DROP aborts the run unless you pass ``--allow-destructive``, so an
  accidental data-dropping migration can never run silently. A column backfill
  (``UPDATE ... SET``) is allowed — it is the normal additive pattern. The scan
  ignores keywords inside comments/strings and matches only at statement starts.

Applied versions are tracked in a ``schema_migrations`` ledger so each runs once.
``--baseline`` adopts an already-current database by recording every pending
migration as applied without running it (e.g. a DB that predates this runner).

Usage::

    python3 scripts/migrate.py                  # backup, then apply pending
    python3 scripts/migrate.py --status         # list state, run nothing
    python3 scripts/migrate.py --baseline       # mark pending as applied, run none
    python3 scripts/migrate.py --allow-destructive   # permit DROP/DELETE/etc.
    python3 scripts/migrate.py --no-backup      # skip the safety backup (CI only)

Backend is chosen like the rest of the pipeline: ``SUPABASE_DB_URL`` set ->
Postgres, otherwise local SQLite.
"""

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# scripts/ is on sys.path[0] when run as ``python3 scripts/migrate.py``.
from db_backend import IS_SQLITE, sqlite_db_path  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "sql" / "migrations"
BACKUPS_DIR = PROJECT_ROOT / "data" / "backups"  # under gitignored data/
KEEP_BACKUPS = 10

_FILENAME_RE = re.compile(r"^(\d+)_(.+?)(?:\.(postgres|sqlite))?\.sql$")

# Statements that destroy existing data or schema objects. Adding a column or
# index is safe; dropping, truncating or deleting is not. A column backfill
# (``UPDATE ... SET`` on a freshly added column) is a normal, expected part of a
# migration, so UPDATE is deliberately NOT treated as destructive. Blocked
# unless --allow-destructive.
#
# The keyword must sit at a statement boundary (file start or just after a
# semicolon) so a keyword appearing mid-statement (e.g. a column named
# ``dropped_at``) does not trip the gate. Comments and string literals are
# stripped before matching (see ``_strip_sql_noise``) so a DROP/DELETE inside a
# ``-- comment`` or a ``'string'`` is ignored.
_DESTRUCTIVE_RE = re.compile(
    r"(?:^|;)\s*"
    r"(?:"
    r"DROP\s+(?:TABLE|COLUMN|VIEW|INDEX|SCHEMA|DATABASE|TRIGGER|SEQUENCE|TYPE|CONSTRAINT)\b"
    r"|TRUNCATE\b"
    r"|DELETE\s+FROM\b"
    r"|ALTER\s+TABLE\s+\S+\s+DROP\b"
    r")",
    re.IGNORECASE,
)

# Length-preserving-ish removal of SQL comments and string/identifier literals,
# so keyword scans never match text inside them.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_SQUOTE_RE = re.compile(r"'(?:''|[^'])*'")
_DQUOTE_RE = re.compile(r'"(?:""|[^"])*"')


def _strip_sql_noise(sql: str) -> str:
    """Blank out comments and quoted literals before scanning for keywords."""
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub(" ", sql)
    sql = _SQUOTE_RE.sub(" ", sql)
    sql = _DQUOTE_RE.sub(" ", sql)
    return sql


def _backend_name() -> str:
    return "sqlite" if IS_SQLITE else "postgres"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def _discover():
    """Return ordered ``[(version, label, path_or_None)]`` for this backend.

    ``path_or_None`` is the file to run; ``None`` means the version exists only
    for the other dialect and should be recorded as a no-op here.
    """
    if not MIGRATIONS_DIR.exists():
        return []

    backend = _backend_name()
    versions: dict[str, dict] = {}
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _FILENAME_RE.match(f.name)
        if not m:
            print(f"  ! ignoring unrecognised migration file: {f.name}", file=sys.stderr)
            continue
        version, label, dialect = m.group(1), m.group(2), m.group(3)
        slot = versions.setdefault(
            version, {"label": label, "generic": None, "sqlite": None, "postgres": None}
        )
        slot[dialect or "generic"] = f

    out = []
    for version in sorted(versions):
        slot = versions[version]
        path = slot[backend] or slot["generic"]  # dialect-specific wins
        out.append((version, slot["label"], path))
    return out


def _scan_destructive(loaded) -> list[str]:
    """Return labels of pending migrations containing destructive statements.

    ``loaded`` is ``[(version, label, path, sql_text_or_None)]`` — the SQL was
    already read once by the caller, so this does no extra I/O.
    """
    hits = []
    for version, label, path, sql in loaded:
        if sql is None:
            continue
        if _DESTRUCTIVE_RE.search(_strip_sql_noise(sql)):
            hits.append(f"{version} {label} ({path.name})")
    return hits


def _rotate_backups(prefix: str):
    if not BACKUPS_DIR.exists():
        return
    kept = sorted(BACKUPS_DIR.glob(f"{prefix}-*"))
    for old in kept[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Backend adapters — migrations run in their NATIVE dialect, so they bypass the
# psycopg2->sqlite translation layer used elsewhere.
# --------------------------------------------------------------------------


class _Sqlite:
    def __init__(self):
        self.path = sqlite_db_path()
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA foreign_keys = ON")
        db_existed = self._has_table("company")
        self._ensure_baseline(db_existed)
        ledger_existed = self._has_table("schema_migrations")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        self.conn.commit()
        # An existing database (had the baseline schema) that the migration
        # ledger has never seen before — i.e. it predates the migration system.
        self.adopted_existing = db_existed and not ledger_existed

    def _has_table(self, name: str) -> bool:
        return (
            self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def _ensure_baseline(self, db_existed: bool):
        if not db_existed:
            schema = (PROJECT_ROOT / "sql" / "schema.sqlite.sql").read_text(encoding="utf-8")
            self.conn.executescript(schema)
            self.conn.commit()
            print("  baseline schema applied (fresh SQLite database)")

    def applied(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}

    def backup(self):
        """Consistent, WAL-safe copy via the SQLite online-backup API."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        dest = BACKUPS_DIR / f"{self.path.stem}-{_stamp()}.db"
        with sqlite3.connect(str(dest)) as d:
            self.conn.commit()
            self.conn.backup(d)
        _rotate_backups(self.path.stem)
        return dest

    def restore(self, backup_path: Path):
        """Revert the live database to a backup, byte-for-byte."""
        self.conn.close()
        # Drop any stale WAL/SHM sidecars so the restored file is authoritative.
        for sidecar in (f"{self.path}-wal", f"{self.path}-shm"):
            p = Path(sidecar)
            if p.exists():
                p.unlink()
        shutil.copy2(backup_path, self.path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA foreign_keys = ON")

    def integrity_ok(self) -> bool:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"

    def run(self, version: str, sql: str | None):
        if sql is not None:
            try:
                self.conn.executescript(sql)  # not transactional — covered by restore()
            except sqlite3.OperationalError as e:
                # SQLite has no ADD COLUMN IF NOT EXISTS (unlike the guarded
                # Postgres siblings), so a single-statement ADD COLUMN whose
                # column already exists raises "duplicate column name". Two
                # sources of that overlap, both a no-op re-add rather than a
                # real inconsistency:
                #   * the frozen baseline already declares a column a later
                #     migration also adds (us_eligibility in 0003,
                #     expiring_alerted_at in 0005); and
                #   * a HALF-MIGRATED database — the column landed but its
                #     ledger row was lost (an adopted DB whose schema advanced
                #     past the runner, then replayed). For 0009 (scored_by),
                #     0011 (board.enabled), 0013 (vacancy.source_board) and
                #     0014 (vacancy.status_reason) that otherwise aborts +
                #     auto-restores on EVERY run, leaving the DB permanently
                #     stuck one migration short with no way forward.
                # In all these cases the column is already there, so treat the
                # migration as applied instead of aborting the whole run.
                #
                # Scoped to these single-statement ADD COLUMN versions on
                # purpose. A future MULTI-statement migration could hit
                # "duplicate column name" on its *second* statement after a
                # *first* statement (e.g. CREATE TABLE) already ran — swallowing
                # that would mark the migration applied while only half of it
                # took effect. New migrations must be idempotent on their own
                # terms (e.g. ``ADD COLUMN IF NOT EXISTS`` as in 0006) rather
                # than relying on this catch.
                known = ("0003", "0005", "0009", "0011", "0013", "0014", "0020", "0021")
                if version not in known or "duplicate column name" not in str(e):
                    raise
                print(f"    (column already present — treating {version} as applied: {e})")
        self.conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        self.conn.commit()

    def close(self):
        self.conn.close()


class _Postgres:
    def __init__(self):
        # Reuse the pipeline's own connector so migrations get the same
        # keepalives, statement/idle timeouts and reconnect retry as every other
        # query — instead of a bare psycopg2.connect that hangs on a flaky pooler.
        from db_backend import _connect_supabase

        self.url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_DIRECT_URL")
        self.conn = _connect_supabase()
        # _connect_supabase leaves a transaction open from its identity SELECT;
        # psycopg2 refuses to flip autocommit mid-transaction, so close it first.
        self.conn.rollback()
        self.conn.autocommit = False
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.schema_migrations')")
            ledger_existed = cur.fetchone()[0] is not None
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, "
                "applied_at TIMESTAMPTZ DEFAULT now())"
            )
        self.conn.commit()
        # Postgres baseline is a manual install step, so a DB whose ledger we've
        # never seen is treated as a pre-existing adoption.
        self.adopted_existing = not ledger_existed

    def applied(self) -> set[str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations")
            return {r[0] for r in cur.fetchall()}

    def backup(self):
        """Best-effort logical dump via pg_dump, if the binary is available."""
        if shutil.which("pg_dump") is None:
            print(
                "  ! pg_dump not found — skipping snapshot. Supabase keeps its own\n"
                "    automated backups, and each migration is transactional, so a\n"
                "    failure rolls back cleanly. Install postgresql-client for a\n"
                "    local snapshot.",
                file=sys.stderr,
            )
            return None
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        dest = BACKUPS_DIR / f"supabase-{_stamp()}.sql"
        try:
            with open(dest, "w", encoding="utf-8") as fh:
                subprocess.run(
                    ["pg_dump", "--no-owner", "--no-privileges", self.url],
                    stdout=fh,
                    check=True,
                    timeout=300,
                )
            _rotate_backups("supabase")
            return dest
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(
                f"  ! pg_dump failed ({e}); continuing — per-migration "
                f"transactions still protect you.",
                file=sys.stderr,
            )
            dest.unlink(missing_ok=True)
            return None

    def restore(self, backup_path: Path):
        # Restoring Postgres from a dump is destructive and environment-specific;
        # we never automate it. Per-migration transactions already prevent
        # partial state, so the dump is only for manual disaster recovery.
        raise NotImplementedError("Postgres restore is manual — see the dump in data/backups/.")

    def integrity_ok(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() == (1,)

    def run(self, version: str, sql: str | None):
        # Per-migration transaction: migration + ledger row commit atomically.
        try:
            with self.conn.cursor() as cur:
                if sql is not None:
                    cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self):
        self.conn.close()


def _open():
    return _Sqlite() if IS_SQLITE else _Postgres()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_status() -> int:
    db = _open()
    try:
        applied = db.applied()
        rows = _discover()
    finally:
        db.close()

    print(f"Backend: {_backend_name()}")
    if not rows:
        print("No migrations defined yet.")
        return 0

    pending = 0
    for version, label, path in rows:
        if version in applied:
            mark = "applied"
        elif path is None:
            mark = "n/a (other dialect)"
        else:
            mark = "PENDING"
            pending += 1
        print(f"  {version}  {label:<40} {mark}")
    print(f"\n{pending} pending, {len(applied)} applied.")
    return 0


def cmd_baseline() -> int:
    """Record every not-yet-applied migration as applied WITHOUT running it.

    For adopting a database that is already at or beyond the current schema
    (e.g. one created before the migration system existed). After this, only
    genuinely new migrations will run.
    """
    db = _open()
    try:
        applied = db.applied()
        to_mark = [(v, lbl) for (v, lbl, _p) in _discover() if v not in applied]
        if not to_mark:
            print("Already baselined — every migration is recorded.")
            return 0
        for version, label in to_mark:
            db.run(version, None)  # record only, run no SQL
            print(f"  = {version} {label}: marked applied (baseline, not run)")
        print(f"\nBaselined — {len(to_mark)} migration(s) recorded without running.")
        return 0
    finally:
        db.close()


def cmd_migrate(allow_destructive: bool, do_backup: bool) -> int:
    db = _open()
    backup_path = None
    try:
        applied = db.applied()
        rows = _discover()
        pending = [(v, lbl, p) for (v, lbl, p) in rows if v not in applied]
        if not pending:
            print(f"Up to date — nothing to apply ({_backend_name()}).")
            return 0

        # An existing DB that predates the ledger: every migration looks pending.
        # Applying them is correct when the DB is at the frozen baseline, but if
        # the DB is already current the user must baseline instead of replaying.
        if db.adopted_existing:
            print(
                "  note: this database predates the migration ledger, so every "
                "migration shows as pending.\n"
                "  If this database is ALREADY up to date, stop now and run "
                "`python3 scripts/migrate.py --baseline`\n"
                "  to record them as applied WITHOUT running them.",
                file=sys.stderr,
            )

        # Read each pending migration once; reuse for the destructive scan AND
        # the apply loop (no double I/O).
        loaded = [
            (v, lbl, p, (p.read_text(encoding="utf-8") if p else None)) for (v, lbl, p) in pending
        ]

        # Safety gate: refuse destructive migrations unless explicitly allowed.
        destructive = _scan_destructive(loaded)
        if destructive and not allow_destructive:
            print(
                "ABORTED — these pending migrations contain destructive statements:",
                file=sys.stderr,
            )
            for d in destructive:
                print(f"    {d}", file=sys.stderr)
            print(
                "\nThey can drop or overwrite existing data. If that is intended, "
                "re-run with --allow-destructive.",
                file=sys.stderr,
            )
            return 2

        # Safety net: snapshot before touching anything.
        if do_backup:
            backup_path = db.backup()
            if backup_path:
                print(f"  backup: {backup_path.relative_to(PROJECT_ROOT)}")

        for version, label, path, sql in loaded:
            if path is None:
                db.run(version, None)
                print(f"  - {version} {label}: recorded (not applicable to {_backend_name()})")
                continue
            print(f"  + {version} {label}: applying {path.name} ...", flush=True)
            db.run(version, sql)

        print(f"\nDone — {len(pending)} migration(s) applied.")
        return 0

    except Exception as e:
        # SQLite DDL is not transactional: roll the whole run back from backup so
        # a failed migration is a clean no-op instead of a half-applied schema.
        print(f"\nMIGRATION FAILED: {e}", file=sys.stderr)
        if IS_SQLITE and backup_path is not None:
            print(f"  restoring database from {backup_path.name} ...", file=sys.stderr)
            db.restore(backup_path)
            if db.integrity_ok():
                print(
                    "  restore OK — database is back to its pre-migration state.", file=sys.stderr
                )
            else:
                print(
                    f"  ! integrity check failed after restore. Your backup is "
                    f"safe at {backup_path}",
                    file=sys.stderr,
                )
        elif not IS_SQLITE:
            print(
                "  the failed migration was rolled back (transactional). Earlier "
                "migrations, if any, stayed applied.",
                file=sys.stderr,
            )
            if backup_path:
                print(f"  full snapshot for disaster recovery: {backup_path}", file=sys.stderr)
        return 1
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply pending schema migrations safely.")
    ap.add_argument("--status", action="store_true", help="show state, run nothing")
    ap.add_argument(
        "--baseline",
        action="store_true",
        help="mark all pending migrations as applied WITHOUT running "
        "them (adopt an already-current database)",
    )
    ap.add_argument(
        "--allow-destructive",
        action="store_true",
        help="permit migrations containing DROP/DELETE/TRUNCATE/etc.",
    )
    ap.add_argument(
        "--no-backup", action="store_true", help="skip the pre-migration safety backup (CI only)"
    )
    args = ap.parse_args()
    if args.status:
        return cmd_status()
    if args.baseline:
        return cmd_baseline()
    return cmd_migrate(allow_destructive=args.allow_destructive, do_backup=not args.no_backup)


if __name__ == "__main__":
    sys.exit(main())
