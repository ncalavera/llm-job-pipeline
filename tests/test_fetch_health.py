"""Unit tests for the fetch-health classifier (scripts/fetch_health.py).

Pure-function coverage of the bucket decision logic — the part where a
misclassification would hide a broken company or cry wolf on a healthy one.
Fully offline, no DB.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_health as fh


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


# --- companies --------------------------------------------------------------


def test_no_strategy_is_never():
    assert fh.classify_company({"fetch_strategy": "", "fetch_status": "ok"}, 10) == "NEVER"


def test_error_status_is_broken():
    row = {"fetch_strategy": "greenhouse", "fetch_status": "js_required", "vacancy_count": 0}
    assert fh.classify_company(row, 10) == "BROKEN"


def test_failure_streak_is_broken_even_if_status_missing():
    row = {"fetch_strategy": "lever", "fetch_status": None, "consecutive_failures": 3}
    assert fh.classify_company(row, 10) == "BROKEN"


def test_has_vacancies_is_ok():
    row = {"fetch_strategy": "greenhouse", "fetch_status": "ok", "vacancy_count": 5}
    assert fh.classify_company(row, 10) == "OK"


def test_recent_empty_fetch_is_empty_not_broken():
    # render_ok_zero / no_data are healthy — a real, successful, empty listing.
    row = {
        "fetch_strategy": "workable",
        "fetch_status": "render_ok_zero",
        "vacancy_count": 0,
        "last_success": _days_ago(1),
        "last_fetched": _days_ago(1),
    }
    assert fh.classify_company(row, 10) == "EMPTY"


def test_long_unsuccessful_is_stale():
    row = {
        "fetch_strategy": "workable",
        "fetch_status": "no_data",
        "vacancy_count": 0,
        "last_success": _days_ago(30),
        "last_fetched": _days_ago(1),
    }
    assert fh.classify_company(row, 10) == "STALE"


def test_attempted_never_succeeded_is_stale():
    row = {
        "fetch_strategy": "firecrawl_scrape",
        "fetch_status": "no_data",
        "vacancy_count": 0,
        "last_success": None,
        "last_fetched": _days_ago(30),
    }
    assert fh.classify_company(row, 10) == "STALE"


# --- boards ------------------------------------------------------------------


def test_board_error_is_broken():
    assert fh.classify_board({"fetch_status": "error: timeout", "vacancy_count": 0}, 10) == "BROKEN"


def test_board_no_telemetry_is_unknown():
    assert fh.classify_board({"fetch_status": None, "vacancy_count": None}, 10) == "UNKNOWN"


def test_board_zero_rows_is_empty():
    row = {"fetch_status": "ok", "vacancy_count": 0, "last_fetched": _days_ago(1)}
    assert fh.classify_board(row, 10) == "EMPTY"


def test_board_with_rows_is_ok():
    row = {"fetch_status": "ok", "vacancy_count": 42, "last_fetched": _days_ago(1)}
    assert fh.classify_board(row, 10) == "OK"


def test_board_old_fetch_is_stale():
    row = {"fetch_status": "ok", "vacancy_count": 42, "last_fetched": _days_ago(30)}
    assert fh.classify_board(row, 10) == "STALE"
