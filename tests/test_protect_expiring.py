"""U2 — latency protection: high-fit roles are never silently lost.

Unseen roles scoring >= PROTECT_SCORE that go gone-from-source or expire past
their deadline flip to 'expiring' (kept visible, alerted) instead of being
silently archived/passed. Below the threshold, behaviour is unchanged. A
re-listed 'expiring' role resurrects to a normal active state.

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


def _set(db, vid, **cols):
    cur = db.get_conn().cursor()
    sets = ", ".join(f"{k} = %s" for k in cols)
    cur.execute(f"UPDATE vacancy SET {sets} WHERE id = %s",
                list(cols.values()) + [vid])
    cur.close()
    _commit(db)


def _status(db, title):
    return db.load_vacancies(include_inactive_companies=True)[
        _id_by_title(db, title)]["status"]


# ---------------------------------------------------------------------------
# Gone-from-source: protect >= PROTECT_SCORE, archive the rest
# ---------------------------------------------------------------------------

def test_high_fit_gone_becomes_expiring_not_archived(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Senior Advisor")])
    _commit(dal)
    vid = _id_by_title(dal, "Senior Advisor")
    _set(dal, vid, llm_score=89)

    archived = dal.archive_gone_vacancies("Acme Robotics", [])  # empty listing
    _commit(dal)
    assert archived == 0, "a protected role must not count as archived"
    assert dal.load_vacancies()[vid]["status"] == "expiring"
    # NOT tombstoned — a re-listing must be free to resurrect it.
    h = dal.load_vacancies()[vid]["dedup_hash"]
    assert h not in dal.get_archived_hashes()


def test_low_fit_gone_still_archived(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Junior Clerk")])
    _commit(dal)
    vid = _id_by_title(dal, "Junior Clerk")
    _set(dal, vid, llm_score=45)

    archived = dal.archive_gone_vacancies("Acme Robotics", [])
    _commit(dal)
    assert archived == 1
    assert dal.load_vacancies()[vid]["status"] == "archived"


# ---------------------------------------------------------------------------
# Past deadline: protect >= PROTECT_SCORE, pass the rest
# ---------------------------------------------------------------------------

def test_high_fit_expired_becomes_expiring_not_passed(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [
        _job("Head of Programmes"), _job("Filing Assistant"),
    ])
    _commit(dal)
    hi = _id_by_title(dal, "Head of Programmes")
    lo = _id_by_title(dal, "Filing Assistant")
    _set(dal, hi, llm_score=72, deadline="2020-01-01")
    _set(dal, lo, llm_score=30, deadline="2020-01-01")

    dal.pass_expired_vacancies()
    _commit(dal)
    assert _status(dal, "Head of Programmes") == "expiring"
    assert _status(dal, "Filing Assistant") == "passed"


def test_decided_role_untouched_by_auto_pass(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Liked Role")])
    _commit(dal)
    vid = _id_by_title(dal, "Liked Role")
    _set(dal, vid, llm_score=80, deadline="2020-01-01")
    dal.update_vacancy_status(vid, "liked")
    _commit(dal)

    dal.pass_expired_vacancies()
    _commit(dal)
    # Auto-pass only ever touches 'unseen'; the user's decision is preserved.
    assert _status(dal, "Liked Role") == "liked"


# ---------------------------------------------------------------------------
# Resurrection: a re-listed expiring role returns to active
# ---------------------------------------------------------------------------

def test_expiring_role_resurrects_on_relisting(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Senior Advisor")])
    _commit(dal)
    vid = _id_by_title(dal, "Senior Advisor")
    _set(dal, vid, llm_score=89)

    dal.archive_gone_vacancies("Acme Robotics", [])  # → expiring
    _commit(dal)
    assert dal.load_vacancies()[vid]["status"] == "expiring"
    # Pretend the loud alert already fired for it.
    _set(dal, vid, expiring_alerted_at="2026-06-20T00:00:00")

    # The company re-lists it → resurrected to a normal active state, and the
    # alert flag is cleared so a future expiry can re-alert.
    new = dal.save_vacancies("Acme Robotics", "A", [_job("Senior Advisor")])
    _commit(dal)
    assert new == 0
    row = dal.load_vacancies()[vid]
    assert row["status"] == "unseen"
    assert row["expiring_alerted_at"] is None
