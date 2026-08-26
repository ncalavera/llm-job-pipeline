"""One status value must be understood identically everywhere: the Python DAL,
learning.py's baskets, the JS server, both SQL schemas, the migration, and the
CSS tokens.

Absorbs:
  * test_declined_status.py — the 'interview' and 'declined' statuses. The
    failure this closes: a role the user actually wanted sat untriaged for two
    months, he applied through another channel, was declined, and the
    pipeline never knew any of it. The board ended at 'applied', so an
    application had nowhere to finish, and the employer's own answer — the
    strongest calibration signal available — was recorded nowhere.
  * test_test_task_status.py — the 'test_task' vacancy status. The gap this
    closes: the board went 'applied' -> 'interview' with nothing in between,
    so a take-home assignment had no column. Those roles sat in Applied
    looking like "waiting for a reply" while work was actually owed, and the
    number of applications that reached a screening exercise was recorded
    nowhere. 'test_task' sits between 'applied' and 'interview' and behaves
    like the stages around it: a decided status (a re-listing never resets
    it), an application status (no sweeper may archive it), and active work
    in the liked basket.
"""

from pathlib import Path

import learning
from database_supabase import (
    APPLICATION_STATUSES,
    VALID_STATUSES,
    _DECIDED_STATUSES,
)
from report.data_prep import _ACTIVE_STATUSES

_ROOT = Path(__file__).resolve().parents[1]


# ===========================================================================
# --- from test_declined_status.py ---
# ===========================================================================

# ---------------------------------------------------------------------------
# The statuses exist and are accepted everywhere a status is validated
# ---------------------------------------------------------------------------


def test_DS01_new_statuses_are_valid():
    assert "interview" in VALID_STATUSES
    assert "declined" in VALID_STATUSES


def test_DS02_new_statuses_are_decided():
    """Decided = a re-listing by the employer never overwrites it. This is the
    protection the lost role lacked: 'expiring' is undecided, so when the job was
    re-listed it was reset to 'unseen' and fell back into the untriaged
    catalogue."""
    assert "interview" in _DECIDED_STATUSES
    assert "declined" in _DECIDED_STATUSES


# ---------------------------------------------------------------------------
# Which basket each one belongs to
# ---------------------------------------------------------------------------


def test_DS03_interview_is_active_interest():
    """An application in flight is active work, not a closed outcome."""
    assert "interview" in learning.LIKED_BASKET
    assert "interview" in learning.DECISION_STATUSES


def test_DS04_declined_is_not_a_user_verdict():
    """He wanted the role; someone else closed it. Counting 'declined' as a user
    decision would teach scoring that he dislikes roles he actively pursued."""
    assert "declined" not in learning.LIKED_BASKET
    assert "declined" not in learning.DECISION_STATUSES
    assert "declined" in learning.REJECTED_STATUSES


def test_DS05_declined_still_proves_he_wanted_it():
    """The backtest reference set is wider than the verdict set on purpose."""
    assert "declined" in learning.WANTED_STATUSES
    for status in learning.LIKED_BASKET:
        assert status in learning.WANTED_STATUSES


# ---------------------------------------------------------------------------
# A filter must never kill a role he applied to and was declined for
# ---------------------------------------------------------------------------


def test_DS06_filter_word_hitting_a_declined_role_is_dirty():
    """A word that would have killed a role he pursued is not a clean filter,
    whatever the employer decided afterwards."""
    declined_title = "Senior Product Manager, New Products"

    dirty = learning.backtest_filter_word("product", [declined_title], [])
    assert dirty["clean"] is False

    clean = learning.backtest_filter_word("logistics", [declined_title], [])
    assert clean["clean"] is True


# ---------------------------------------------------------------------------
# A board that surfaced a role he applied to has earned its keep
# ---------------------------------------------------------------------------


def test_DS07_board_with_a_declined_role_is_not_proposed_for_disabling():
    """Otherwise the board that found him a real, wanted role gets turned off
    because the employer said no."""
    known = {"greenhouse"}
    vacancies = [
        {"source": "greenhouse", "status": "declined", "locations": []},
    ] + [{"source": "greenhouse", "status": "passed", "locations": []} for _ in range(20)]

    proposals = learning.propose_board_disables(vacancies, known, min_seen=5)
    assert proposals == []


def test_DS08_board_with_no_wanted_role_is_still_proposed():
    """The guard above must not disarm the check entirely."""
    known = {"greenhouse"}
    vacancies = [{"source": "greenhouse", "status": "passed", "locations": []} for _ in range(20)]

    proposals = learning.propose_board_disables(vacancies, known, min_seen=5)
    assert [p["board"] for p in proposals] == ["greenhouse"]


# ===========================================================================
# --- from test_test_task_status.py ---
# ===========================================================================

# ---------------------------------------------------------------------------
# The status exists everywhere a status is validated
# ---------------------------------------------------------------------------


def test_TT01_test_task_is_a_valid_status():
    assert "test_task" in VALID_STATUSES


def test_TT02_every_writer_accepts_it():
    """Three copies of the status set gate a write: the DAL, the simple-mode
    dashboard and the self-hosted server. A status missing from any one of them
    is rejected at that door only — the board offers a column the save then
    refuses.

    The Python copies now read one vocabulary (scripts/statuses.py), so only the
    server's JavaScript list can drift. Its own suite asserts membership against
    the parsed array; this is the cheap cross-language check from here."""
    from dashboard_local import VALID_STATUSES as LOCAL_STATUSES

    assert "test_task" in LOCAL_STATUSES

    source = (_ROOT / "server.js").read_text()
    assert '"test_task"' in source, "server.js does not accept 'test_task'"


def test_TT03_test_task_is_decided():
    """Decided = a re-listing by the employer never overwrites it. A role with a
    take-home in progress must never fall back into the untriaged catalogue."""
    assert "test_task" in _DECIDED_STATUSES


# ---------------------------------------------------------------------------
# It is an application: permanent, and it survives the score floor
# ---------------------------------------------------------------------------


def test_TT04_test_task_records_an_application():
    """Work has been submitted to an employer. Archiving that silently would
    erase part of the only honest record of the search."""
    assert "test_task" in APPLICATION_STATUSES


def test_TT05_test_task_survives_the_dashboard_score_floor():
    assert "test_task" in _ACTIVE_STATUSES


# ---------------------------------------------------------------------------
# Which basket it belongs to
# ---------------------------------------------------------------------------


def test_TT06_test_task_is_active_interest():
    """A take-home assignment is the most active an application ever gets, so it
    reads as a liked-basket verdict, exactly like 'interview'."""
    assert "test_task" in learning.LIKED_BASKET
    assert "test_task" in learning.DECISION_STATUSES
    assert "test_task" in learning.WANTED_STATUSES


def test_TT07_test_task_is_not_a_rejection():
    """Unlike 'declined', nobody has closed anything yet."""
    assert "test_task" not in learning.REJECTED_STATUSES


# ---------------------------------------------------------------------------
# The database will accept it
# ---------------------------------------------------------------------------


def test_TT08_both_baseline_schemas_allow_it():
    """A fresh install builds from the baseline schema, so the CHECK constraint
    there must already list the status; an existing database gets it from
    migration 0020."""
    for rel in ("sql/schema.sql", "sql/schema.sqlite.sql"):
        source = (_ROOT / rel).read_text()
        assert "'test_task'" in source, f"{rel} rejects 'test_task'"


def test_TT09_a_migration_widens_the_live_constraint():
    """The baseline is frozen for new installs only — an existing Postgres has
    the old CHECK until a migration replaces it."""
    migration = _ROOT / "sql" / "migrations" / "0020_test_task_status.postgres.sql"
    source = migration.read_text()
    assert "vacancy_status_check" in source
    assert "''test_task''" in source
    # Re-runnable: guarded so a database that already allows it is a no-op.
    assert "IF NOT EXISTS" in source


def test_TT10_the_column_accent_resolves_to_a_defined_token():
    """TRIAGE_COLUMNS colours are CSS custom properties interpolated into inline
    styles by pipeline.js. A token that style.css never defines renders as no
    colour at all — the column header dot and its move buttons go blank."""
    import re

    state_js = (_ROOT / "public" / "modules" / "state.js").read_text()
    css = (_ROOT / "public" / "style.css").read_text()

    columns = state_js[state_js.index("export const TRIAGE_COLUMNS") :]
    columns = columns[: columns.index("\n];")]
    tokens = set(re.findall(r"var\((--[a-z0-9-]+)\)", columns))

    assert "--raspberry" in tokens, "the Test task column lost its accent"
    for token in sorted(tokens):
        assert re.search(rf"^\s*{re.escape(token)}:\s*#", css, re.M), (
            f"{token} is used by a triage column but never defined in style.css"
        )
