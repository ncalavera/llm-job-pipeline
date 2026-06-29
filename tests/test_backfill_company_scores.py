"""U13 — one-off company-score backfill selection.

The backfill targets every ACTIVE company with a website (regardless of an
existing score, so it re-scores the old-rubric ones) and reports the before/
after alignment_score table. Offline on the local SQLite backend.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry",
                "db_conn", "db_backend", "backfill_company_scores"):
        sys.modules.pop(mod, None)
    import db_backend
    importlib.reload(db_backend)
    import database_supabase as db
    yield db
    db.close_conn()


def _company(db, name, status, website, score=None):
    db.ensure_company(name, status=status)
    cur = db.get_conn().cursor()
    cur.execute(
        "UPDATE company SET website = %s, alignment_score = %s WHERE canonical_name = %s",
        (website, score, name),
    )
    cur.close()
    db.get_conn().commit()


def test_selects_active_with_website_only(dal):
    import backfill_company_scores as bf
    _company(dal, "GiveWell", "active", "https://givewell.org", 38)
    _company(dal, "Fresh Active", "active", "https://fresh.org", None)
    _company(dal, "No Site", "active", "", 70)          # excluded: no website
    _company(dal, "Candidate Co", "candidate", "https://cand.org", 50)  # excluded
    _company(dal, "Inactive Co", "inactive", "https://x.org", 90)        # excluded

    names = bf._active_company_names()
    assert names == ["Fresh Active", "GiveWell"]  # active + website, sorted


def test_scores_table_lists_all_active(dal):
    import backfill_company_scores as bf
    _company(dal, "GiveWell", "active", "https://givewell.org", 38)
    _company(dal, "No Site", "active", "", 70)

    rows = dict(bf._active_scores())
    # Includes active companies even without a website (the score table is for
    # the before/after spot-check, not the re-score target list).
    assert rows["GiveWell"] == 38
    assert "No Site" in rows
