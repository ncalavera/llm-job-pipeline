"""Application entity — DAL + dashboard plumbing.

Runs against a throwaway SQLite file (SUPABASE_DB_URL cleared for the whole
session by conftest; each test points JOBSEARCH_DB_PATH at its own temp file).
Never touches a real database.

The ``application`` table is migration-only (0010), exactly like scored_by
(0009): a fresh baseline connection does NOT have it, so these tests apply the
migration explicitly before exercising the DAL — the same thing a real
``python3 scripts/migrate.py`` run does. Fully invented companies/roles.
"""

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = str(REPO / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

_CHAIN = ("database_supabase", "config", "company_registry", "db_conn", "db_backend", "applications")


def _fresh_sqlite(monkeypatch, db_file: Path, *, migrate: bool = True):
    """Point the backend chain at a fresh temp SQLite file and reload it.

    ``migrate=True`` also applies migration 0010 (the application table), the way
    a real install's ``migrate.py`` run does. ``migrate=False`` leaves the DB at
    the frozen baseline (no application table) to exercise the table_ready guard.
    """
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    # Drop the backend chain AND every report.* submodule (data_prep binds
    # load_vacancies/config at import; a stale copy would query a prior DB).
    for mod in list(sys.modules):
        if mod in _CHAIN or mod == "scoring_settings" or mod == "report" or mod.startswith(
            "report."
        ):
            sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "applications test must run on the SQLite backend"

    conn = db_backend.get_conn()  # first connection -> frozen baseline
    if migrate:
        sql = (REPO / "sql" / "migrations" / "0010_application.sqlite.sql").read_text(
            encoding="utf-8"
        )
        # Raw executescript (like migrate.py) — the file holds several statements.
        conn._conn.executescript(sql)
        conn.commit()

    import database_supabase

    return database_supabase


def _company(dal, name):
    cid = dal.ensure_company(name, status="active")
    dal.get_conn().commit()
    return cid


def _vacancy(dal, org, title):
    dal.save_vacancies(
        org,
        "B",
        [
            {
                "title": title,
                "snippet": f"{title} -- a genuine open role.",
                "full_description": (f"We are hiring a {title}. " * 10),
                "location": "Berlin, Germany",
                "url": f"https://example.test/jobs/{title.lower().replace(' ', '-')}",
            }
        ],
    )
    dal.get_conn().commit()
    for vid, v in dal.load_vacancies(include_inactive_companies=True).items():
        if v["title"] == title:
            return vid
    raise AssertionError(f"vacancy {title!r} not found")


# ---------------------------------------------------------------------------
# table_ready guard
# ---------------------------------------------------------------------------


def test_table_ready_false_before_migration(tmp_path, monkeypatch):
    _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite", migrate=False)
    import applications

    importlib.reload(applications)
    assert applications.table_ready() is False
    # Reads degrade instead of crashing on a not-yet-migrated DB.
    assert applications.applications_by_vacancy() == {}
    assert applications.applications_by_company() == {}
    assert applications.get_for_vacancy("whatever") is None
    with pytest.raises(RuntimeError):
        applications.record_application("cid", None)


def test_table_ready_true_after_migration(tmp_path, monkeypatch):
    _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite", migrate=True)
    import applications

    importlib.reload(applications)
    assert applications.table_ready() is True


# ---------------------------------------------------------------------------
# create + attach + read
# ---------------------------------------------------------------------------


def test_record_application_creates_row_with_artifacts(tmp_path, monkeypatch):
    dal = _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite")
    import applications

    importlib.reload(applications)

    cid = _company(dal, "Northwind Aid Trust")
    vid = _vacancy(dal, "Northwind Aid Trust", "Programme Officer")

    app_id = applications.record_application(
        cid,
        vid,
        channel="site",
        artifacts={"cv_version": "cv_ops_v3.pdf", "cover_letter_path": "cover_northwind.md"},
        notes="Referred by a past colleague.",
    )
    assert app_id

    app = applications.get_for_vacancy(vid)
    assert app is not None
    assert app["status"] == "applied"
    assert app["channel"] == "site"
    assert app["applied_at"]  # defaulted to today
    assert app["artifacts"]["cv_version"] == "cv_ops_v3.pdf"
    assert app["notes"].startswith("Referred")
    assert app["vacancy_id"] == vid
    assert app["company_id"] == cid


def test_record_application_is_idempotent_per_vacancy(tmp_path, monkeypatch):
    dal = _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite")
    import applications

    importlib.reload(applications)
    cid = _company(dal, "Northwind Aid Trust")
    vid = _vacancy(dal, "Northwind Aid Trust", "Programme Officer")

    first = applications.record_application(cid, vid, artifacts={"cv_version": "v1.pdf"})
    second = applications.record_application(
        cid, vid, status="interview", artifacts={"cover_letter_path": "cl.md"}
    )
    assert first == second  # same row, not a duplicate
    assert len(applications.list_for_company(cid)) == 1

    app = applications.get_for_vacancy(vid)
    assert app["status"] == "interview"
    # Artifacts MERGE, not overwrite.
    assert app["artifacts"]["cv_version"] == "v1.pdf"
    assert app["artifacts"]["cover_letter_path"] == "cl.md"


def test_attach_artifacts_merges(tmp_path, monkeypatch):
    dal = _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite")
    import applications

    importlib.reload(applications)
    cid = _company(dal, "Northwind Aid Trust")
    vid = _vacancy(dal, "Northwind Aid Trust", "Programme Officer")
    app_id = applications.record_application(cid, vid, artifacts={"cv_version": "v1.pdf"})

    assert applications.attach_artifacts(app_id, {"research_urls": ["https://example.test/about"]})
    app = applications.get(app_id)
    assert app["artifacts"]["cv_version"] == "v1.pdf"
    assert app["artifacts"]["research_urls"] == ["https://example.test/about"]


# ---------------------------------------------------------------------------
# status move + cross-connection durability (the DAL-commit contract)
# ---------------------------------------------------------------------------


def test_status_move_persists_across_connections(tmp_path, monkeypatch):
    dal = _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite")
    import applications

    importlib.reload(applications)
    cid = _company(dal, "Northwind Aid Trust")
    vid = _vacancy(dal, "Northwind Aid Trust", "Programme Officer")
    app_id = applications.record_application(cid, vid)

    assert applications.set_status(app_id, "offer")

    # Drop the connection and reopen — a write that did not commit would vanish.
    dal.close_conn()
    reopened = dal.get_conn()
    cur = reopened.cursor()
    cur.execute("SELECT status FROM application WHERE id = %s", (app_id,))
    (status,) = cur.fetchone()
    cur.close()
    assert status == "offer"


def test_set_status_rejects_unknown_status(tmp_path, monkeypatch):
    dal = _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite")
    import applications

    importlib.reload(applications)
    cid = _company(dal, "Northwind Aid Trust")
    app_id = applications.record_application(cid, None)
    with pytest.raises(ValueError):
        applications.set_status(app_id, "ghosted")


def test_hand_added_application_without_vacancy(tmp_path, monkeypatch):
    dal = _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite")
    import applications

    importlib.reload(applications)
    cid = _company(dal, "Northwind Aid Trust")
    a1 = applications.record_application(cid, None, status="draft")
    a2 = applications.record_application(cid, None, status="draft")
    # No vacancy => no dedup => two distinct rows.
    assert a1 != a2
    assert len(applications.list_for_company(cid)) == 2
    # draft => no applied_at auto-filled
    assert applications.get(a1)["applied_at"] is None


# ---------------------------------------------------------------------------
# research -> company_evidence (same table that feeds WANT scoring)
# ---------------------------------------------------------------------------


def test_research_saved_into_company_evidence(tmp_path, monkeypatch):
    dal = _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite")
    cid = _company(dal, "Northwind Aid Trust")

    dal.save_company_evidence(
        cid,
        "manual_url",
        url="https://example.test/impact-report",
        content="Northwind ran a 4-country programme reaching 20k people.",
        meta={"note": "pre-application research"},
    )
    # Idempotent by (company_id, source, url).
    dal.save_company_evidence(
        cid,
        "manual_url",
        url="https://example.test/impact-report",
        content="Updated content.",
    )

    summary = dal.load_company_evidence_summary()
    rows = summary.get(str(cid), [])
    assert len(rows) == 1
    assert rows[0]["source"] == "manual_url"
    assert rows[0]["url"] == "https://example.test/impact-report"


# ---------------------------------------------------------------------------
# dashboard plumbing — the vacancy card + company profile carry it
# ---------------------------------------------------------------------------


def test_dashboard_payload_carries_application_and_research(tmp_path, monkeypatch):
    dal = _fresh_sqlite(monkeypatch, tmp_path / "db.sqlite")
    import applications

    importlib.reload(applications)

    cid = _company(dal, "Northwind Aid Trust")
    vid = _vacancy(dal, "Northwind Aid Trust", "Programme Officer")
    # Score the vacancy so it appears on the dashboard (unscored rows are hidden).
    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE vacancy SET llm_score = 72 WHERE id = %s", (vid,))
    cur.close()
    conn.commit()

    applications.record_application(cid, vid, channel="email", artifacts={"cv_version": "v9.pdf"})
    dal.save_company_evidence(cid, "manual_url", url="https://example.test/mission")

    from report.data_prep import prepare_report_data, prepare_company_data

    report = prepare_report_data()
    grp = next(g for g in report["groups"] if g["id"] == vid)
    assert grp["application"] is not None
    assert grp["application"]["status"] == "applied"
    assert grp["application"]["channel"] == "email"

    companies = prepare_company_data()
    northwind = next(c for c in companies if c["name"] == "Northwind Aid Trust")
    assert northwind["application_count"] == 1
    assert northwind["applications"][0]["artifacts"]["cv_version"] == "v9.pdf"
    assert northwind["research_count"] == 1
