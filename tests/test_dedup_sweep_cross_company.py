"""Cross-company clustering in dedup_sweep.py (report-only, never auto-merged).

``_cluster`` only groups rows within one company_id (the SASH / SASH Foundation
split never surfaces there). ``_cluster_cross_company`` adds a second pass over
EVERY row, grouping on a shared normalized apply URL + a matching title across
DIFFERENT companies. These clusters are printed every run — dry-run or
--apply — and never enter ``merges``: a split company row is a company-registry
decision, not something this sweep resolves on its own.

Same SQLite harness as tests/test_dedup_sweep_applications.py.
"""

import importlib
import sys

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "dedup_sweep",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"
    import database_supabase as db

    yield db
    db.close_conn()


def _commit(db):
    db.get_conn().commit()


def _job(title, *, url):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role, long enough to clear the content gate.",
        "full_description": f"We are hiring a {title}. " * 12 + "Own the work end to end.",
        "location": "Remote",
        "url": url,
    }


_URL = "https://sash.org/careers/program-officer"
# Two spellings that do NOT fuzzy-fold in ensure_company/_find_mergeable_company
# (mirrors the real "Model Evaluation and Threat Research" / "METR" case in
# test_save_board_vacancies_characterization.py) — a genuine split company pair.
_ORG_A = "Model Evaluation and Threat Research"
_ORG_B = "METR"


def test_same_url_two_companies_flagged_cross_company(dal):
    """Two company rows sharing a posting URL are flagged, not silently
    ignored by the within-company-only clusterer.
    """
    import dedup_sweep

    dal.ensure_company(_ORG_A, status="active")
    _commit(dal)
    dal.save_vacancies(_ORG_A, "B", [_job("Program Officer", url=_URL)])
    _commit(dal)

    dal.ensure_company(_ORG_B, status="active")
    _commit(dal)
    # Bypass save_vacancies' own cross-company fold (the (b) fix) to reproduce
    # a company row already split BEFORE that fix existed.
    dal.save_vacancies(_ORG_B, "B", [_job("Program Officer", url=_URL)], None, {})
    _commit(dal)

    rows = dedup_sweep._load_rows()
    same_company_clusters = dedup_sweep._cluster(rows)
    assert same_company_clusters == []  # different company_id, invisible to _cluster

    cross = dedup_sweep._cluster_cross_company(rows)
    assert len(cross) == 1
    assert {r["org"] for r in cross[0]} == {_ORG_A, _ORG_B}


def test_different_title_same_company_url_not_flagged(dal):
    """A shared generic careers URL across two companies with UNRELATED titles
    must not be flagged — same title guard as the within-company merge."""
    import dedup_sweep

    dal.ensure_company("Company A", status="active")
    _commit(dal)
    dal.save_vacancies("Company A", "B", [_job("Finance Manager", url=_URL)])
    _commit(dal)

    dal.ensure_company("Company B", status="active")
    _commit(dal)
    dal.save_vacancies("Company B", "B", [_job("Software Engineer", url=_URL)], None, {})
    _commit(dal)

    rows = dedup_sweep._load_rows()
    cross = dedup_sweep._cluster_cross_company(rows)
    assert cross == []


def test_main_merges_cross_company_cluster_when_names_are_one_org(monkeypatch, dal, capsys):
    """The 2026-09-21 bug: he applied under "SASH (Seabridge AI)", the board
    listed the same posting under "SASH", and the second row came back as
    unseen. The names read as one org, so --apply folds the company rows and
    the postings then collapse onto the applied row.
    """
    import dedup_sweep

    dal.ensure_company("SASH", status="active")
    _commit(dal)
    # Two postings, so "SASH" is the company row the other folds into.
    dal.save_vacancies(
        "SASH",
        "B",
        [
            _job("Head of Operations", url=_URL),
            _job("Verification Lead", url="https://sash.org/careers/verification-lead"),
        ],
    )
    _commit(dal)
    # Inserted directly: ensure_company now folds this spelling onto "SASH"
    # (the source fix), so the split can only be staged behind its back.
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO company (id, canonical_name, status) VALUES (%s, %s, 'active')",
        ("11111111-1111-1111-1111-111111111111", "SASH (Seabridge AI)"),
    )
    _commit(dal)
    dal.save_vacancies("SASH (Seabridge AI)", "B", [_job("Head of Operations", url=_URL)], None, {})
    _commit(dal)
    cur.execute("SELECT COUNT(*) FROM company WHERE canonical_name LIKE 'SASH%'")
    assert cur.fetchone()[0] == 2, "the split this test repairs must exist first"
    # The hand-added row is the one he applied through.
    cur.execute(
        "UPDATE vacancy SET status = 'applied' WHERE company_id = "
        "(SELECT id FROM company WHERE canonical_name = 'SASH (Seabridge AI)')"
    )
    _commit(dal)

    monkeypatch.setattr(sys, "argv", ["dedup_sweep.py", "--apply"])
    dedup_sweep.main()

    cur.execute("SELECT canonical_name FROM company WHERE canonical_name LIKE 'SASH%'")
    assert cur.fetchall() == [("SASH",)]
    cur.execute("SELECT status FROM vacancy WHERE title = 'Head of Operations'")
    assert cur.fetchall() == [("applied",)], "one row left, and it is the applied one"
    cur.close()


def test_main_never_merges_cross_company_cluster(monkeypatch, dal, capsys):
    """main() --apply must still leave a cross-company duplicate as two rows —
    only same-company merges are ever written."""
    import dedup_sweep

    dal.ensure_company(_ORG_A, status="active")
    _commit(dal)
    dal.save_vacancies(_ORG_A, "B", [_job("Program Officer", url=_URL)])
    _commit(dal)
    dal.ensure_company(_ORG_B, status="active")
    _commit(dal)
    dal.save_vacancies(_ORG_B, "B", [_job("Program Officer", url=_URL)], None, {})
    _commit(dal)

    monkeypatch.setattr(sys, "argv", ["dedup_sweep.py", "--apply"])
    dedup_sweep.main()

    out = capsys.readouterr().out
    assert "CROSS-COMPANY" in out

    cur = dal.get_conn().cursor()
    cur.execute("SELECT COUNT(*) FROM vacancy")
    assert cur.fetchone()[0] == 2
    cur.close()
