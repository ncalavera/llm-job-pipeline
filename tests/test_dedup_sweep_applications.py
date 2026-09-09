"""The dedup sweep must never delete a row that records an application.

`_STATUS_RANK` was a hand-written list that stopped at 'applied'. The three
funnel stages added later — 'test_task', 'interview', 'declined' — were missing
from it, so such a row ranked 0: below 'unseen', below even 'archived'. In a
cluster it was therefore picked as the LOSER and hard-DELETEd, and the only
record that the user ever applied went with it.

Contract under test:

  * every APPLICATION status outranks every other status, so the application row
    is always the survivor and the cluster inherits its decision;
  * a cluster holding TWO application rows is never collapsed — collapsing it
    would delete one of them — it goes to manual review untouched;
  * `_apply_merge` refuses outright if a loser records an application, so the
    DELETE cannot happen even if a future caller skips the checks above.

Same SQLite harness as tests/test_dedup_board_prefix_retitle.py. Orgs invented.
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


_ORG = "Northwind Aid Trust"
_REQ_URL = "https://northwind.test/jobs/programme-manager"
_DESC = "We are hiring a Programme Manager. " * 20


def _commit(db):
    db.get_conn().commit()


def _job(title, *, url=_REQ_URL):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": _DESC,
        "location": "Remote",
        "url": url,
    }


def _rows(db):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT id, title, status FROM vacancy")
    rows = cur.fetchall()
    cur.close()
    return rows


def _two_variants(db, first_status, second_status):
    """Two rows of ONE role — a seniority rename — sharing an apply URL, so the
    sweep clusters them. Inserted behind the save path (which would fold the
    rename itself) by storing an unrelated title and repointing it after."""
    db.ensure_company(_ORG, status="active")
    db.save_vacancies(_ORG, "A", [_job("Programme Manager")])
    _commit(db)
    db.save_vacancies(_ORG, "A", [_job("Placeholder Independent Role", url=_REQ_URL + "-tmp")])
    _commit(db)

    plain_hash = db.make_vacancy_id(_ORG, "Programme Manager")
    placeholder_hash = db.make_vacancy_id(_ORG, "Placeholder Independent Role")
    renamed_hash = db.make_vacancy_id(_ORG, "Senior Programme Manager")
    cur = db.get_conn().cursor()
    cur.execute(
        "UPDATE vacancy SET title = %s, dedup_hash = %s, status = %s, locations = %s "
        "WHERE dedup_hash = %s",
        (
            "Senior Programme Manager",
            renamed_hash,
            second_status,
            db.Json([{"url": _REQ_URL}]),
            placeholder_hash,
        ),
    )
    cur.execute("UPDATE vacancy SET status = %s WHERE dedup_hash = %s", (first_status, plain_hash))
    cur.close()
    _commit(db)
    assert len(_rows(db)) == 2


def _sweep(monkeypatch, apply=False):
    import dedup_sweep

    importlib.reload(dedup_sweep)
    monkeypatch.setattr(sys, "argv", ["dedup_sweep.py"] + (["--apply"] if apply else []))
    dedup_sweep.main()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_every_application_status_outranks_every_other_status(dal):
    import dedup_sweep

    importlib.reload(dedup_sweep)
    from statuses import ALL_STATUSES, APPLICATION_STATUSES

    ranks = dedup_sweep._STATUS_RANK
    assert set(ranks) == set(ALL_STATUSES), "every status needs a rank — a missing one is 0"
    floor = min(ranks[s] for s in APPLICATION_STATUSES)
    others = [ranks[s] for s in ALL_STATUSES if s not in APPLICATION_STATUSES]
    assert floor > max(others)


# ---------------------------------------------------------------------------
# The sweep itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["test_task", "interview", "declined", "applied"])
def test_application_row_survives_and_the_undecided_copy_is_folded(dal, monkeypatch, status):
    """The row with work in flight must be the survivor, not the loser."""
    _two_variants(dal, status, "unseen")

    _sweep(monkeypatch, apply=True)

    rows = _rows(dal)
    assert len(rows) == 1, [(r["title"], r["status"]) for r in rows]
    assert rows[0]["status"] == status


def test_two_application_rows_are_left_for_a_human(dal, monkeypatch):
    """Collapsing would delete one application, so the cluster is untouched."""
    _two_variants(dal, "applied", "interview")

    _sweep(monkeypatch, apply=True)

    rows = _rows(dal)
    assert sorted(r["status"] for r in rows) == ["applied", "interview"]


def test_apply_merge_refuses_an_application_loser(dal):
    """The last check before the DELETE, independent of survivor selection."""
    import dedup_sweep

    importlib.reload(dedup_sweep)
    survivor = {"id": "1", "status": "unseen", "llm_score": None, "locations": []}
    loser = {"id": "2", "status": "test_task", "llm_score": None, "locations": []}

    with pytest.raises(dal.ApplicationArchiveBlocked):
        dedup_sweep._apply_merge(survivor, [loser])
