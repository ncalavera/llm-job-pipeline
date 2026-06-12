"""Tests for triage helpers — deadline filtering logic."""
from datetime import date, timedelta

import pytest


# ---------------------------------------------------------------------------
# is_deadline_passed (pure function, no DB)
# ---------------------------------------------------------------------------

def test_TG01_no_deadline_not_passed():
    from triage import is_deadline_passed
    assert is_deadline_passed({}) is False
    assert is_deadline_passed({"deadline": ""}) is False
    assert is_deadline_passed({"deadline": None}) is False


def test_TG02_future_deadline_not_passed():
    from triage import is_deadline_passed
    future = (date.today() + timedelta(days=30)).isoformat()
    assert is_deadline_passed({"deadline": future}) is False


def test_TG03_today_deadline_not_passed():
    from triage import is_deadline_passed
    today = date.today().isoformat()
    assert is_deadline_passed({"deadline": today}) is False


def test_TG04_past_deadline_is_passed():
    from triage import is_deadline_passed
    past = (date.today() - timedelta(days=1)).isoformat()
    assert is_deadline_passed({"deadline": past}) is True


def test_TG05_old_deadline_is_passed():
    from triage import is_deadline_passed
    old = "2026-01-29"  # The example from issue #199
    # This should be passed since we're well past Jan 2026
    if date.today() > date(2026, 1, 29):
        assert is_deadline_passed({"deadline": old}) is True
    else:
        pytest.skip("Test only valid after 2026-01-29")


def test_TG06_invalid_deadline_not_passed():
    from triage import is_deadline_passed
    assert is_deadline_passed({"deadline": "not-a-date"}) is False
    assert is_deadline_passed({"deadline": "2026-13-01"}) is False
