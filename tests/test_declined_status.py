"""Tests for the 'interview' and 'declined' statuses.

The failure these close: a role the user actually wanted (FundraiseUp Senior
Product Manager) sat untriaged for two months, he applied through another
channel, was declined, and the pipeline never knew any of it. The board ended at
'applied', so an application had nowhere to finish, and the employer's own
answer — the strongest calibration signal available — was recorded nowhere.
"""

import learning
from database_supabase import _DECIDED_STATUSES, VALID_STATUSES


# ---------------------------------------------------------------------------
# The statuses exist and are accepted everywhere a status is validated
# ---------------------------------------------------------------------------


def test_DS01_new_statuses_are_valid():
    assert "interview" in VALID_STATUSES
    assert "declined" in VALID_STATUSES


def test_DS02_new_statuses_are_decided():
    """Decided = a re-listing by the employer never overwrites it. This is the
    exact protection the FundraiseUp role lacked: 'expiring' is undecided, so
    when the job was re-listed it was reset to 'unseen' and fell back into the
    untriaged catalogue."""
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
