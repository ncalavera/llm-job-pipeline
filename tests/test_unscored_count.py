"""The dashboard's 'N fetched, none scored yet' hint must count only vacancies
genuinely awaiting scoring — not already-triaged rows that happen to lack a
score. Regression for the inflated-count bug."""

import sys
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from report.data_prep import _count_unscored  # noqa: E402


def _vac(status="unseen", score=None):
    return {"status": status, "llm_score": score}


def test_counts_unseen_unscored():
    vacs = {"a": _vac("unseen", None), "b": _vac("unseen", None)}
    assert _count_unscored(vacs) == 2


def test_excludes_scored():
    vacs = {"a": _vac("unseen", 80), "b": _vac("unseen", 0)}
    assert _count_unscored(vacs) == 0


def test_excludes_triaged_without_score():
    """A passed/skipped/liked vacancy with no score is NOT awaiting scoring."""
    vacs = {
        "passed": _vac("passed", None),
        "skipped": _vac("skipped", None),
        "liked": _vac("liked", None),
        "fresh": _vac("unseen", None),
    }
    assert _count_unscored(vacs) == 1  # only the fresh unseen one


def test_missing_status_treated_as_unseen():
    vacs = {"a": {"llm_score": None}}  # no status key
    assert _count_unscored(vacs) == 1


def test_empty():
    assert _count_unscored({}) == 0
