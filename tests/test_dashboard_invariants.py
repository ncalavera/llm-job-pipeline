"""Dashboard invariants that must hold regardless of how the payload was built:
the score floor applied once in Python, the English-only Cyrillic ban on the
public shell, and the ``dashboard_snapshot`` migration/baseline parity.

Absorbed: test_dashboard_score_floor.py, test_dashboard_no_cyrillic.py,
test_dashboard_snapshot_migration.py.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = str(REPO / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


# ---------------------------------------------------------------------------
# --- from test_dashboard_score_floor.py ---
#
# The dashboard score floor is applied once, in Python, before data ships.
#
# Before this, the 40 floor lived only in the Catalog tab's client-side filter
# and in `score_floor_any_company`, which gates roles from UNAPPROVED
# companies. A weak role at an APPROVED company therefore shipped in the
# snapshot and showed up on every other surface — which is how a scraped
# fundraiser page (scored 32) and a case-study heading with no description
# (scored 28) ended up on the dashboard.
# ---------------------------------------------------------------------------

from config import CATALOG_MIN_SCORE  # noqa: E402
from report.data_prep import keep_on_dashboard as _keep  # noqa: E402

# `_keep` IS the production rule (report.data_prep.keep_on_dashboard), not a
# copy of it: this file used to re-implement the filter, so the two could drift
# and the test would still pass while the dashboard shipped the weak tail.


# ---------------------------------------------------------------------------
# The weak tail never reaches the dashboard
# ---------------------------------------------------------------------------


def test_SF01_undecided_below_floor_is_dropped():
    assert _keep({"llm_score": 32, "status": "unseen"}) is False
    assert _keep({"llm_score": 28, "status": "unseen"}) is False
    assert _keep({"llm_score": CATALOG_MIN_SCORE - 1, "status": "unseen"}) is False


def test_SF02_undecided_at_or_above_floor_is_kept():
    assert _keep({"llm_score": CATALOG_MIN_SCORE, "status": "unseen"}) is True
    assert _keep({"llm_score": 78, "status": "unseen"}) is True


# ---------------------------------------------------------------------------
# A decision outranks the number
# ---------------------------------------------------------------------------


def test_SF03_a_role_being_worked_survives_any_score():
    """The weakest role in the liked basket scores 15. Hiding it because a model
    disagreed would be the pipeline overruling its user."""
    for status in (
        "liked",
        "to_apply",
        "to_research",
        "to_network",
        "applied",
        "test_task",
        "interview",
    ):
        assert _keep({"llm_score": 15, "status": status}) is True


def test_SF04_a_declined_role_survives_any_score():
    """A closed application is history the user asked to keep — it is the record
    of what he tried, and it feeds scoring calibration."""
    assert _keep({"llm_score": 15, "status": "declined"}) is True


def test_SF05_rejected_roles_below_the_floor_are_dropped():
    """'passed' and 'skipped' are dead ends, not decisions to keep in view.
    Treating them as active shipped a bulk pass of 190 roles straight back onto
    the board. Above the floor they stay, so a strong role he rejected is still
    visible."""
    for status in ("passed", "skipped"):
        assert _keep({"llm_score": 15, "status": status}) is False
        assert _keep({"llm_score": CATALOG_MIN_SCORE, "status": status}) is True


# ---------------------------------------------------------------------------
# Unscored rows stay out (they surface after scoring, as before)
# ---------------------------------------------------------------------------


def test_SF06_unscored_is_dropped_whatever_the_status():
    assert _keep({"llm_score": None, "status": "unseen"}) is False
    assert _keep({"llm_score": None, "status": "liked"}) is False


# ---------------------------------------------------------------------------
# --- from test_dashboard_no_cyrillic.py ---
#
# Regression: the English public dashboard ships zero Cyrillic.
#
# The dashboard shell + logic (``public/index.html``, ``public/app.js``,
# ``public/style.css``, ``public/modules/*.js``) is the public,
# English-language UI. Raw Cyrillic (``[Ѐ-ӿ]``, U+0400–U+04FF) or its escaped
# form (``\\u04XX``) in any of these tracked files means owner-language copy
# leaked into the public shell.
#
# ``public/data.js`` is excluded: it is gitignored, generated, and legitimately
# carries vacancy text in any language.
# ---------------------------------------------------------------------------

PUBLIC = REPO / "public"

RAW_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
ESCAPED_CYRILLIC = re.compile(r"\\u04[0-9a-fA-F]{2}")


def _dashboard_files() -> list[Path]:
    files: list[Path] = []
    for rel in ("index.html", "app.js", "style.css"):
        p = PUBLIC / rel
        if p.exists():
            files.append(p)
    modules = PUBLIC / "modules"
    if modules.exists():
        files.extend(sorted(modules.glob("*.js")))
    return files


def test_dashboard_files_present():
    """The scan must actually have files to scan (catches a moved layout)."""
    files = _dashboard_files()
    assert files, "no public dashboard files found — scan would be vacuous"
    # data.js must never be in scope.
    assert all(p.name != "data.js" for p in files)


def test_no_raw_cyrillic_in_dashboard():
    hits: list[str] = []
    for p in _dashboard_files():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if RAW_CYRILLIC.search(line):
                hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")
    assert not hits, "Raw Cyrillic leaked into the English dashboard:\n" + "\n".join(hits)


def test_no_escaped_cyrillic_in_dashboard():
    hits: list[str] = []
    for p in _dashboard_files():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if ESCAPED_CYRILLIC.search(line):
                hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")
    assert not hits, "Escaped Cyrillic (\\u04XX) leaked into the English dashboard:\n" + "\n".join(
        hits
    )


# ---------------------------------------------------------------------------
# --- from test_dashboard_snapshot_migration.py ---
#
# U1 — the dashboard_snapshot table: migration + baseline parity.
#
# The full-mode live dashboard reads its payload from a single Supabase row.
# This locks down that the migration creating that row's table is present,
# well-formed, non-destructive, and mirrored in the frozen baseline schema so
# a fresh install and a migrated install converge.
#
# The migration is Postgres-only (JSONB / TIMESTAMPTZ / NOW()), so we validate
# its SQL by content + the migrate runner's own destructive scanner rather
# than running it on the SQLite test backend.
# ---------------------------------------------------------------------------

_MIGRATION = REPO / "sql" / "migrations" / "0001_dashboard_snapshot.postgres.sql"
_SCHEMA = REPO / "sql" / "schema.sql"


def _migrate_module():
    sys.path.insert(0, str(REPO / "scripts"))
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
