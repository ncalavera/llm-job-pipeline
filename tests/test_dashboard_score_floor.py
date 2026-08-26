"""The dashboard score floor is applied once, in Python, before data ships.

Before this, the 40 floor lived only in the Catalog tab's client-side filter and
in `score_floor_any_company`, which gates roles from UNAPPROVED companies. A weak
role at an APPROVED company therefore shipped in the snapshot and showed up on
every other surface — which is how a scraped fundraiser page (scored 32) and a
case-study heading with no description (scored 28) ended up on the dashboard.
"""

from config import CATALOG_MIN_SCORE
from report.data_prep import keep_on_dashboard as _keep

# `_keep` IS the production rule (report.data_prep.keep_on_dashboard), not a
# copy of it: this file used to re-implement the filter, so the two could drift
# and the test would still pass while the dashboard shipped the weak tail.


# ---------------------------------------------------------------------------
# The weak tail never reaches the dashboard
# ---------------------------------------------------------------------------


def test_SF01_undecided_below_floor_is_dropped():
    assert _keep({"llm_score": 32, "status": "unseen"}) is False
    assert _keep({"llm_score": 28, "status": "unseen"}) is False
    assert _keep({"llm_score": CATALOG_MIN_SCORE - 1, "status": "unseen"}) is False


def test_SF02_undecided_at_or_above_floor_is_kept():
    assert _keep({"llm_score": CATALOG_MIN_SCORE, "status": "unseen"}) is True
    assert _keep({"llm_score": 78, "status": "unseen"}) is True


# ---------------------------------------------------------------------------
# A decision outranks the number
# ---------------------------------------------------------------------------


def test_SF03_a_role_being_worked_survives_any_score():
    """The weakest role in the liked basket scores 15. Hiding it because a model
    disagreed would be the pipeline overruling its user."""
    for status in (
        "liked",
        "to_apply",
        "to_research",
        "to_network",
        "applied",
        "test_task",
        "interview",
    ):
        assert _keep({"llm_score": 15, "status": status}) is True


def test_SF04_a_declined_role_survives_any_score():
    """A closed application is history the user asked to keep — it is the record
    of what he tried, and it feeds scoring calibration."""
    assert _keep({"llm_score": 15, "status": "declined"}) is True


def test_SF05_rejected_roles_below_the_floor_are_dropped():
    """'passed' and 'skipped' are dead ends, not decisions to keep in view.
    Treating them as active shipped a bulk pass of 190 roles straight back onto
    the board. Above the floor they stay, so a strong role he rejected is still
    visible."""
    for status in ("passed", "skipped"):
        assert _keep({"llm_score": 15, "status": status}) is False
        assert _keep({"llm_score": CATALOG_MIN_SCORE, "status": status}) is True


# ---------------------------------------------------------------------------
# Unscored rows stay out (they surface after scoring, as before)
# ---------------------------------------------------------------------------


def test_SF06_unscored_is_dropped_whatever_the_status():
    assert _keep({"llm_score": None, "status": "unseen"}) is False
    assert _keep({"llm_score": None, "status": "liked"}) is False
