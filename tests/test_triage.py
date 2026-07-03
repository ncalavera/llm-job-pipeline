"""Tests for triage helpers — deadline filtering logic."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from config import DASHBOARD_TZ


def _today():
    """The same clock is_deadline_passed uses — anchor the relative-date tests to
    it, not to naive local, so they never disagree with the function under test
    across the local-vs-DASHBOARD_TZ midnight window."""
    return datetime.now(DASHBOARD_TZ).date()


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

    future = (_today() + timedelta(days=30)).isoformat()
    assert is_deadline_passed({"deadline": future}) is False


def test_TG03_today_deadline_not_passed():
    from triage import is_deadline_passed

    today = _today().isoformat()
    assert is_deadline_passed({"deadline": today}) is False


def test_TG04_past_deadline_is_passed():
    from triage import is_deadline_passed

    past = (_today() - timedelta(days=1)).isoformat()
    assert is_deadline_passed({"deadline": past}) is True


def test_TG05_old_deadline_is_passed():
    from triage import is_deadline_passed

    old = (_today() - timedelta(days=180)).isoformat()
    assert is_deadline_passed({"deadline": old}) is True


def test_TG06_invalid_deadline_not_passed():
    from triage import is_deadline_passed

    assert is_deadline_passed({"deadline": "not-a-date"}) is False
    assert is_deadline_passed({"deadline": "2026-13-01"}) is False


# ---------------------------------------------------------------------------
# Expiry clock must match every OTHER surface (report/data_prep, filter,
# database_supabase all use datetime.now(DASHBOARD_TZ).date()). Naive
# date.today() made triage disagree with the report during the local-midnight-
# to-UTC window: a role read "expired" here while still "live" in the report.
# ---------------------------------------------------------------------------


class _FixedClock:
    """Stand-in for ``datetime`` whose ``now()`` returns a fixed instant,
    converting to the requested tz exactly as the real ``datetime.now(tz)``
    would — so the SAME instant yields different local dates per timezone."""

    def __init__(self, instant):
        self._instant = instant

    def now(self, tz=None):
        if tz is None:
            return self._instant.astimezone().replace(tzinfo=None)
        return self._instant.astimezone(tz)


# 02:00 UTC on the 4th — still the 3rd in any zone west of UTC.
_WINDOW_INSTANT = datetime(2026, 7, 4, 2, 0, tzinfo=timezone.utc)


def test_TG07_expiry_uses_dashboard_tz_not_naive_local(monkeypatch):
    """The reproduced bug: at 02:00 UTC, a 2026-07-03 deadline is expired under
    the UTC dashboard clock but NOT under a west-of-UTC local clock. Triage must
    follow DASHBOARD_TZ, whatever the fetch machine's local tz is."""
    import triage

    monkeypatch.setattr(triage, "datetime", _FixedClock(_WINDOW_INSTANT))

    monkeypatch.setattr(triage, "DASHBOARD_TZ", ZoneInfo("UTC"))
    assert triage.is_deadline_passed({"deadline": "2026-07-03"}) is True

    monkeypatch.setattr(triage, "DASHBOARD_TZ", ZoneInfo("America/New_York"))
    assert triage.is_deadline_passed({"deadline": "2026-07-03"}) is False


@pytest.mark.parametrize(
    "tzname",
    ["UTC", "America/New_York", "Asia/Kolkata", "Pacific/Kiritimati"],
)
def test_TG08_expiry_matches_the_shared_dashboard_clock(monkeypatch, tzname):
    """For any DASHBOARD_TZ, ``is_deadline_passed`` agrees with
    ``datetime.now(DASHBOARD_TZ).date()`` — yesterday is expired, today is not."""
    import triage

    tz = ZoneInfo(tzname)
    monkeypatch.setattr(triage, "datetime", _FixedClock(_WINDOW_INSTANT))
    monkeypatch.setattr(triage, "DASHBOARD_TZ", tz)

    today = _WINDOW_INSTANT.astimezone(tz).date()
    yesterday = (today - timedelta(days=1)).isoformat()

    assert triage.is_deadline_passed({"deadline": yesterday}) is True
    assert triage.is_deadline_passed({"deadline": today.isoformat()}) is False
