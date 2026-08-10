"""An application, once made, never disappears from the board.

Applied / interview / declined are the record of what the user actually tried:
how many applications went out, how far each got, and what came back. That is
the only honest statistic he has about his own search, and it is unrecoverable
if a sweeper quietly archives it.

Three layers have to hold:
  1. no AUTOMATIC archive path may select a role in an application status;
  2. the single status-write choke point must refuse to archive one;
  3. the dashboard must ship it whatever it scored.
"""

import re
from pathlib import Path

import pytest

from database_supabase import APPLICATION_STATUSES
from report.data_prep import _ACTIVE_STATUSES

_DAL = Path(__file__).resolve().parents[1] / "scripts" / "database_supabase.py"


# ---------------------------------------------------------------------------
# Layer 1 — every automatic archival is scoped to untouched rows
# ---------------------------------------------------------------------------


def test_AP01_every_archive_statement_is_scoped_to_unseen():
    """Each `SET status = 'archived'` must be reachable only for 'unseen' rows —
    either by a WHERE clause on this statement or by the SELECT that built its
    id list. A new archival path that forgets this fails here."""
    source = _DAL.read_text()

    # Each archiving UPDATE, with the ~25 lines of context that scope it.
    for match in re.finditer(r"UPDATE vacancy SET status = 'archived'", source):
        start = source.rfind("\ndef ", 0, match.start())
        window = source[start : match.end() + 700]
        assert "'unseen'" in window, (
            "An archiving UPDATE is not scoped to unseen rows:\n"
            + source[match.start() - 200 : match.end() + 300]
        )


# ---------------------------------------------------------------------------
# Layer 2 — the choke point refuses
# ---------------------------------------------------------------------------


def test_AP02_application_statuses_are_the_expected_set():
    assert APPLICATION_STATUSES == {"applied", "interview", "declined"}


@pytest.mark.parametrize("current", sorted(APPLICATION_STATUSES))
def test_AP03_archiving_an_application_is_blocked(monkeypatch, current):
    import database_supabase as dal

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchone(self):
            return (current,)

        def close(self):
            pass

    class _Conn:
        def cursor(self, *a, **kw):
            return _Cur()

    monkeypatch.setattr(dal, "get_conn", lambda: _Conn())

    with pytest.raises(dal.ApplicationArchiveBlocked):
        dal.update_vacancy_status("some-uuid", "archived")

    assert not any("SET status" in s for s in executed), "the UPDATE must not run"


@pytest.mark.parametrize("current", ["unseen", "passed", "skipped", "liked", "expiring"])
def test_AP04_archiving_anything_else_still_works(monkeypatch, current):
    """The guard must not turn into a blanket ban on archiving."""
    import database_supabase as dal

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchone(self):
            return (current,)

        def close(self):
            pass

    class _Conn:
        def cursor(self, *a, **kw):
            return _Cur()

    monkeypatch.setattr(dal, "get_conn", lambda: _Conn())
    dal.update_vacancy_status("some-uuid", "archived")

    assert any("SET status" in s for s in executed)


def test_AP05_force_allows_a_deliberate_correction(monkeypatch):
    """An application logged against the wrong role must still be fixable."""
    import database_supabase as dal

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchone(self):
            return ("applied",)

        def close(self):
            pass

    class _Conn:
        def cursor(self, *a, **kw):
            return _Cur()

    monkeypatch.setattr(dal, "get_conn", lambda: _Conn())
    dal.update_vacancy_status("some-uuid", "archived", force=True)

    assert any("SET status" in s for s in executed)


# ---------------------------------------------------------------------------
# Layer 3 — the dashboard ships them whatever they scored
# ---------------------------------------------------------------------------


def test_AP06_applications_survive_the_dashboard_score_floor():
    for status in APPLICATION_STATUSES:
        assert status in _ACTIVE_STATUSES, (
            f"'{status}' records an application but does not survive the score "
            "floor — a low-scored application would vanish from the board"
        )
