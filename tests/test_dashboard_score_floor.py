"""The dashboard score floor is applied once, in Python, before data ships.

Before this, the 40 floor lived only in the Catalog tab's client-side filter and
in `score_floor_any_company`, which gates roles from UNAPPROVED companies. A weak
role at an APPROVED company therefore shipped in the snapshot and showed up on
every other surface — which is how "EA Funds — Director" (32, a scraped
fundraiser page) and "Elevate Philanthropy — Historical Projects" (28, no
description) reached the owner.
"""

from config import CATALOG_MIN_SCORE


def _keep(vacancy):
    """The rule under test, mirroring scripts/report/data_prep.py."""
    score = vacancy.get("llm_score")
    if score is None or score < 0:
        return False
    return vacancy.get("status") != "unseen" or score >= CATALOG_MIN_SCORE


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


def test_SF03_a_role_he_acted_on_survives_any_score():
    """The weakest role in his liked basket scores 15. Hiding it because a model
    disagreed with him would be the pipeline overruling its owner."""
    for status in ("liked", "to_apply", "applied", "interview", "declined", "passed"):
        assert _keep({"llm_score": 15, "status": status}) is True


# ---------------------------------------------------------------------------
# Unscored rows stay out (they surface after scoring, as before)
# ---------------------------------------------------------------------------


def test_SF04_unscored_is_dropped_whatever_the_status():
    assert _keep({"llm_score": None, "status": "unseen"}) is False
    assert _keep({"llm_score": None, "status": "liked"}) is False
