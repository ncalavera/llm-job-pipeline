"""The filter stage must never delete or tombstone an application in flight.

`_PROTECTED_STATUSES` was written out by hand in two modules and stopped at
'applied'. The funnel stages added later — 'test_task', 'interview',
'declined' — were in neither copy, so a role with a take-home assignment on the
user's desk was an ordinary unprotected row: the dedup loser guard let it be
deleted, and the classify loop happily proposed it for deletion.

Both copies now read `statuses.PROTECTED_STATUSES`, so a new funnel stage is
protected everywhere the moment it is added to the one vocabulary.

Contract under test:

  * both `_PROTECTED_STATUSES` copies ARE the shared set;
  * `_pick_winner` never makes a protected row the loser;
  * the exact-hash dedup skips a protected loser instead of deleting it;
  * `classify_vacancies` proposes no protected row for deletion.

Orgs and roles invented.
"""

import importlib
import sys

import pytest

from statuses import APPLICATION_STATUSES, PROTECTED_STATUSES


# ---------------------------------------------------------------------------
# One vocabulary, two call sites
# ---------------------------------------------------------------------------


def test_both_modules_use_the_shared_protected_set():
    import filters
    import filter_vacancies

    assert filters._PROTECTED_STATUSES is PROTECTED_STATUSES
    assert filter_vacancies._PROTECTED_STATUSES is PROTECTED_STATUSES
    assert APPLICATION_STATUSES <= PROTECTED_STATUSES


@pytest.mark.parametrize("status", sorted(APPLICATION_STATUSES))
def test_an_application_row_never_loses_a_duplicate_pair(status):
    """Even against a better-scored, longer, older rival."""
    import filters
    import filter_vacancies

    protected = {"id": "1", "status": status, "llm_score": 10, "first_seen": "2026-08-01"}
    rival = {
        "id": "2",
        "status": "unseen",
        "llm_score": 99,
        "full_description": "x" * 5000,
        "first_seen": "2026-01-01",
    }
    for module in (filters, filter_vacancies):
        winner, loser = module._pick_winner(protected, rival)
        assert winner["id"] == "1", module.__name__
        winner, loser = module._pick_winner(rival, protected)
        assert winner["id"] == "1", module.__name__


# ---------------------------------------------------------------------------
# The exact-hash dedup loser guard
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Answers the two SELECTs `_clean_exact_dupes` issues, in order."""

    def __init__(self, responses):
        self._responses = responses
        self._rows = []

    def execute(self, sql, params=None):
        for needle, rows in self._responses:
            if needle in sql:
                self._rows = rows
                return
        self._rows = []

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, responses):
        self._responses = responses
        self.committed = False

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._responses)

    def commit(self):
        self.committed = True


@pytest.mark.parametrize("status", sorted(APPLICATION_STATUSES))
def test_exact_hash_dedup_skips_an_application_loser(monkeypatch, status):
    """Two rows share a dedup_hash and BOTH carry a decision — a liked row and
    an application. One of the two has to be the loser; whichever it is, a
    protected loser is counted and left alone, never handed to the delete list.
    Before the fix the application row was unprotected, so it was deleted."""
    import filter_vacancies

    rows = [
        {
            "id": "aaaa",
            "status": "liked",
            "llm_score": 90,
            "title": "Programme Manager",
            "first_seen": "2026-01-01",
            "last_seen": "2026-08-01",
        },
        {
            "id": "bbbb",
            "status": status,
            "llm_score": 10,
            "title": "Programme Manager",
            "first_seen": "2026-01-01",
            "last_seen": "2026-08-01",
        },
    ]
    conn = _FakeConn(
        [
            ("GROUP BY dedup_hash", [{"dedup_hash": "h1", "ids": ["aaaa", "bbbb"]}]),
            ("SELECT * FROM vacancy", rows),
        ]
    )
    monkeypatch.setattr(filter_vacancies, "get_conn", lambda: conn)
    deleted = []
    monkeypatch.setattr(filter_vacancies, "db_delete_vacancies", lambda ids: deleted.extend(ids))
    monkeypatch.setattr(filter_vacancies, "update_vacancy_fields", lambda *a, **k: None)

    result = filter_vacancies._clean_exact_dupes()

    assert deleted == []
    assert result["merged"] == 0
    assert result["protected"] == 1


# ---------------------------------------------------------------------------
# The classify loop
# ---------------------------------------------------------------------------


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "filter_vacancies",
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


def _categorised(vid, categories):
    return [name for name, items in categories.items() if any(i[0] == vid for i in items)]


@pytest.mark.parametrize("status", ["unseen", *sorted(APPLICATION_STATUSES)])
def test_classify_never_proposes_a_protected_row_for_deletion(dal, monkeypatch, status):
    """The same row, blacklisted by title: proposed for deletion while it is
    undecided, invisible to the filter once it records an application."""
    dal.ensure_company("Northwind Aid Trust", status="active")
    dal.save_vacancies(
        "Northwind Aid Trust",
        "A",
        [
            {
                "title": "Programme Manager",
                "snippet": "A genuine open role.",
                "full_description": "We are hiring a Programme Manager. " * 20,
                "location": "Remote",
                "url": "https://northwind.test/jobs/pm",
            }
        ],
    )
    dal.get_conn().commit()
    cur = dal.get_conn().cursor()
    cur.execute("UPDATE vacancy SET status = %s", (status,))
    cur.close()
    dal.get_conn().commit()

    import filters
    import filter_vacancies

    importlib.reload(filter_vacancies)
    monkeypatch.setattr(filters, "title_words_blacklisted", lambda title: True)

    categories = filter_vacancies.classify_vacancies()
    vid = next(iter(dal.load_vacancies(status=status)))

    if status == "unseen":
        assert _categorised(vid, categories) == ["delete_blacklist"]
    else:
        assert _categorised(vid, categories) == []
