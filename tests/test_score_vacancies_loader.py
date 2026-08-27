"""score_vacancies._load_and_dedup honours the filter pass's exclusion record.

The filter stage (migration 0020) writes vacancy.scoring_excluded_reason; the
scorer's loader selects only reasoned-NULL rows, orders oldest-unscored-first
when unattended, and keeps its own blind/boilerplate check for one proven
night before that check is removed.
"""

import importlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

MIGRATION_SQLITE = (
    Path(__file__).resolve().parent.parent
    / "sql"
    / "migrations"
    / "0020_add_vacancy_scoring_excluded_reason.sqlite.sql"
)


def _force_sqlite(monkeypatch, db_file):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db

    return db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = _force_sqlite(monkeypatch, tmp_path / "loader.db")
    cur = db.get_conn().cursor()
    cur.execute(MIGRATION_SQLITE.read_text(encoding="utf-8"))
    db.get_conn().commit()
    cur.close()
    sys.modules.pop("score_vacancies", None)
    import score_vacancies as sv

    importlib.reload(sv)
    yield db, sv
    db.close_conn()


def _seed(
    db,
    org,
    title,
    *,
    desc="A real job description with plenty of content. " * 4,
    created_at=None,
    reason=None,
):
    db.ensure_company(org, status="active")
    canonical = db.resolve_canonical_name(org)
    dedup_hash = db.make_vacancy_id(canonical, title)
    company_id = db.resolve_company_id(org)
    today = date.today().isoformat()
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO vacancy (dedup_hash, company_id, title, full_description, "
        "first_seen, last_seen, locations, status, scoring_excluded_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            dedup_hash,
            company_id,
            title,
            desc,
            today,
            today,
            json.dumps([{"location": "Berlin, Germany", "url": "https://x.test/1"}]),
            "unseen",
            reason,
        ),
    )
    if created_at:
        cur.execute(
            "UPDATE vacancy SET created_at = ? WHERE dedup_hash = ?", (created_at, dedup_hash)
        )
    cur.execute("SELECT id FROM vacancy WHERE dedup_hash = ?", (dedup_hash,))
    vid = str(cur.fetchone()[0])
    conn.commit()
    cur.close()
    return vid


def _role_keys(sv, **kwargs):
    roles, _fitness, _stats = sv._load_and_dedup(include_candidates=False, **kwargs)
    return [key for key, _rep, _members in roles]


def test_reasoned_row_is_not_offered_to_the_scorer(env):
    db, sv = env
    _seed(db, "GiveWell", "Program Manager", reason="US-only location")
    _seed(db, "CleanOrg", "Backend Engineer")

    keys = _role_keys(sv)

    assert ("CleanOrg", "Backend Engineer") in keys
    assert ("GiveWell", "Program Manager") not in keys


def test_blind_row_is_skipped_until_enriched(env):
    """The scorer keeps its own blind check for one proven night: a blind row
    (NULL reason) is not offered while blind, and is offered after enrichment."""
    db, sv = env
    _seed(db, "Eta Labs", "Blind Role", desc="")

    assert ("Eta Labs", "Blind Role") not in _role_keys(sv)

    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE vacancy SET full_description = ? WHERE title = ?",
        ("Now a full, meaningful job description with real duties. " * 4, "Blind Role"),
    )
    conn.commit()
    cur.close()

    assert ("Eta Labs", "Blind Role") in _role_keys(sv)


def test_unattended_orders_oldest_unscored_first(env):
    db, sv = env
    _seed(db, "NewOrg", "New Role", created_at="2026-08-26 10:00:00")
    _seed(db, "OldOrg", "Old Role", created_at="2026-08-01 10:00:00")
    _seed(db, "MidOrg", "Mid Role", created_at="2026-08-15 10:00:00")

    keys = _role_keys(sv, unattended=True)
    assert keys == [
        ("OldOrg", "Old Role"),
        ("MidOrg", "Mid Role"),
        ("NewOrg", "New Role"),
    ]

    # Default (attended) keeps the newest-first load order.
    keys_default = _role_keys(sv)
    assert keys_default[0] == ("NewOrg", "New Role")


def test_unattended_is_a_cli_flag_on_the_scorer(env):
    _db, sv = env
    args = sv.build_parser().parse_args(["--local", "--unattended"])
    assert args.unattended is True
    args = sv.build_parser().parse_args(["--local"])
    assert args.unattended is False
