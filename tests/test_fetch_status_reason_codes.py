"""fetch_status reason codes: distinguish empty from broken (U9 / WS6).

Covers the three outcomes that used to collapse into the ambiguous ``no_data``:
  * ``render_ok_zero``   — a real successful fetch that is genuinely empty
  * ``credit_exhausted`` — a firecrawl_scrape aborted because credits hit 0
  * ``js_required``      — a JS-rendered shell (UNCHANGED regression guard)
"""

import fetchers
from fetch_vacancies import _resolve_fetch_status
from database_supabase import is_fetch_error


# ---------------------------------------------------------------------------
# _resolve_fetch_status — render_ok_zero + js_required-unchanged
# ---------------------------------------------------------------------------


class TestResolveFetchStatus:
    def test_successful_empty_becomes_render_ok_zero(self):
        assert _resolve_fetch_status("ok", False, None) == "render_ok_zero"

    def test_js_required_shell_unchanged(self):
        # Regression guard: the pre-U9 behaviour was ok + no jobs + js_required.
        assert _resolve_fetch_status("ok", False, "js_required") == "js_required"

    def test_credit_exhausted_recorded(self):
        assert _resolve_fetch_status("ok", False, "credit_exhausted") == "credit_exhausted"

    def test_jobs_present_stays_ok(self):
        # Non-empty fetch is never downgraded, whatever the scrape override says.
        assert _resolve_fetch_status("ok", True, "js_required") == "ok"
        assert _resolve_fetch_status("ok", True, None) == "ok"

    def test_error_status_passes_through(self):
        assert _resolve_fetch_status("error: boom", False, None) == "error: boom"
        assert _resolve_fetch_status("error: boom", False, "credit_exhausted") == "error: boom"


# ---------------------------------------------------------------------------
# is_fetch_error — render_ok_zero is healthy, the rest are broken
# ---------------------------------------------------------------------------


class TestIsFetchErrorClassification:
    def test_render_ok_zero_is_not_an_error(self):
        assert is_fetch_error("render_ok_zero") is False

    def test_ok_and_no_data_not_errors(self):
        assert is_fetch_error("ok") is False
        assert is_fetch_error("no_data") is False

    def test_broken_codes_are_errors(self):
        assert is_fetch_error("credit_exhausted") is True
        assert is_fetch_error("js_required") is True
        assert is_fetch_error("error: HTTP 500") is True


# ---------------------------------------------------------------------------
# fetch_firecrawl_scrape — records credit_exhausted when credits == 0
# ---------------------------------------------------------------------------


class TestCreditExhaustedOverride:
    def test_credit_gate_records_credit_exhausted(self, monkeypatch):
        org = "British International Investment"
        # Force credits to 0 (short-circuits the balance check, no network) and
        # stub the local fallback so no real HTTP happens.
        monkeypatch.setattr(fetchers, "_firecrawl_credits_remaining", 0)
        monkeypatch.setitem(fetchers._last_scrape_status, org, None)
        monkeypatch.setattr(fetchers, "_fetch_local_scrape", lambda *a, **k: [])

        jobs = fetchers.fetch_firecrawl_scrape(org, "https://isw.example/careers")
        assert jobs == []
        assert fetchers.get_scrape_statuses().get(org) == "credit_exhausted"

        # End-to-end: the status assignment turns that into credit_exhausted.
        resolved = _resolve_fetch_status("ok", bool(jobs), fetchers.get_scrape_statuses().get(org))
        assert resolved == "credit_exhausted"

    def test_quota_error_marks_credit_exhausted(self, monkeypatch):
        org = "CTG"
        # Credits look available, but the SDK raises a quota error mid-scrape.
        monkeypatch.setattr(fetchers, "_firecrawl_credits_remaining", 100)
        monkeypatch.setitem(fetchers._last_scrape_status, org, None)

        class _QuotaClient:
            def scrape(self, *a, **k):
                raise RuntimeError("402 payment required: insufficient credit")

        monkeypatch.setattr(fetchers, "get_firecrawl_client", lambda: _QuotaClient())
        monkeypatch.setattr(fetchers, "_fetch_local_scrape", lambda *a, **k: [])

        jobs = fetchers.fetch_firecrawl_scrape(org, "https://ctg.example/jobs")
        assert jobs == []
        assert fetchers.get_scrape_statuses().get(org) == "credit_exhausted"
