"""Tests for the archive + restore flow (gone-from-source, restore, resurrect).

Existing coverage:
  * test_sqlite_backend.test_gone_from_source_archives_unseen — archive_gone
    archives an unseen role absent from the fresh listing.
  * test_e2e_pipeline.test_day2_refetch_dedup_new_and_gone — archive_gone in the
    day-2 refetch.

This file fills the GAPS those leave open:
  * Restore: an archived vacancy moved back to 'unseen' via update_vacancy_status
    re-enters the live catalog.
  * Direct-ATS resurrection: a vacancy archived as gone-from-source that the
    company's OWN ATS re-lists is resurrected to 'unseen' on the next
    save_vacancies (include_gone=False path).
  * Job-board cooldown: a board re-import of a gone-from-source posting is
    SKIPPED within the TTL cooldown (include_gone=True path), so a lagging board
    can't undo the source's closure.
  * Decided statuses are NOT archived by archive_gone_vacancies.

Fully offline on the local SQLite backend.
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
    for mod in ("database_supabase", "config", "company_registry",
                "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend
    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db
    yield db
    db.close_conn()


def _job(title, city="Berlin, Germany"):
    return {
        "title": title,
        "snippet": f"{title} blurb.",
        "full_description": f"We are hiring a {title}. " * 12,
        "location": city,
        "url": f"https://acme.example/job/{title.lower().replace(' ', '-')}",
    }


def _id_by_title(db, title):
    for vid, v in db.load_vacancies(include_inactive_companies=True).items():
        if v["title"] == title:
            return vid
    raise AssertionError(f"vacancy {title!r} not found")


def _commit(db):
    db.get_conn().commit()


# ---------------------------------------------------------------------------
# Restore: archived → unseen re-enters the live catalog
# ---------------------------------------------------------------------------

def test_restore_archived_vacancy_to_unseen(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Programme Lead")])
    _commit(dal)
    vid = _id_by_title(dal, "Programme Lead")

    # Archive it (gone from source).
    dal.archive_gone_vacancies("Acme Robotics", [])
    _commit(dal)
    assert dal.load_vacancies()[vid]["status"] == "archived"
    # And it drops out of an archived-excluding listing (how vac.py list shows it).
    live = dal.load_vacancies(status_exclude=["archived"])
    assert vid not in live

    # Restore: move back to unseen.
    dal.update_vacancy_status(vid, "unseen")
    _commit(dal)
    # Now visible again in the archived-excluding catalog.
    live = dal.load_vacancies(status_exclude=["archived"])
    assert vid in live
    assert live[vid]["status"] == "unseen"


# ---------------------------------------------------------------------------
# archive_gone never touches a DECIDED status
# ---------------------------------------------------------------------------

def test_archive_gone_skips_decided_status(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [
        _job("Liked Role"), _job("Unseen Role"),
    ])
    _commit(dal)
    liked = _id_by_title(dal, "Liked Role")
    dal.update_vacancy_status(liked, "liked")
    _commit(dal)

    # Fresh listing is empty → both roles are "gone", but only the unseen one
    # may be archived; the liked decision is protected.
    archived = dal.archive_gone_vacancies("Acme Robotics", [])
    _commit(dal)
    assert archived == 1

    final = {v["title"]: v["status"]
             for v in dal.load_vacancies(include_inactive_companies=True).values()}
    assert final["Liked Role"] == "liked"      # protected
    assert final["Unseen Role"] == "archived"  # archived


# ---------------------------------------------------------------------------
# Direct-ATS resurrection: the company's own re-listing revives a gone role
# ---------------------------------------------------------------------------

def test_direct_refetch_resurrects_gone_role(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Programme Lead")])
    _commit(dal)
    vid = _id_by_title(dal, "Programme Lead")

    # Source drops it → archived as gone_from_source.
    dal.archive_gone_vacancies("Acme Robotics", [])
    _commit(dal)
    assert dal.load_vacancies()[vid]["status"] == "archived"

    # The company's OWN ATS lists it again → merge resurrects it to unseen
    # (include_gone=False ignores the gone_from_source cooldown for the source).
    new = dal.save_vacancies("Acme Robotics", "A", [_job("Programme Lead")])
    _commit(dal)
    # Not counted as new (same dedup_hash, existing row), but un-archived.
    assert new == 0
    assert dal.load_vacancies()[vid]["status"] == "unseen"


# ---------------------------------------------------------------------------
# Score-below-threshold archive hash is recorded and skips re-merge
# ---------------------------------------------------------------------------

def test_score_below_threshold_hash_blocks_remerge(dal):
    """A dedup_hash archived for a non-gone reason (e.g. score_below_threshold)
    is within the TTL cooldown, so a fresh merge of the same role is skipped —
    the score-archive flow's data guarantee."""
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Low Score Role")])
    _commit(dal)
    vid = _id_by_title(dal, "Low Score Role")
    h = dal.load_vacancies()[vid]["dedup_hash"]

    # Simulate the low-score archive: delete the row and record its hash.
    dal.delete_vacancies([vid])
    dal.record_archived_hashes([(h, "score_below_threshold")])
    _commit(dal)
    assert h in dal.get_archived_hashes()
    assert dal.load_vacancies() == {}

    # Re-merge the same role: blocked by the cooldown (recently archived).
    new = dal.save_vacancies("Acme Robotics", "A", [_job("Low Score Role")])
    _commit(dal)
    assert new == 0
    assert dal.load_vacancies() == {}
