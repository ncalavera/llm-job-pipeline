"""Tests for the company registry load-failure signal.

COMPANIES degrades to {} on a DB outage AND on a genuinely empty/fresh table —
identical states. These tests pin that _build_companies_from_db() now exposes a
distinct REGISTRY_LOAD_FAILED / registry_load_failed() signal so /jobs-new can
hard-stop on an outage instead of offering destructive onboarding.

The module eager-loads at import, so we call _build_companies_from_db() directly
with company_registry.get_conn patched rather than relying on import-time state.
Globals are reset after each test so we don't pollute other modules.
"""

import sys

import pytest

import company_registry


@pytest.fixture(autouse=True)
def _reset_registry_globals():
    """Restore the load-failure globals after each test."""
    failed = company_registry.REGISTRY_LOAD_FAILED
    error = company_registry.REGISTRY_LOAD_ERROR
    yield
    company_registry.REGISTRY_LOAD_FAILED = failed
    company_registry.REGISTRY_LOAD_ERROR = error


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        pass

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def commit(self):
        pass


def test_healthy_load_sets_no_failure(monkeypatch):
    """One active company row → COMPANIES non-empty and load did NOT fail."""
    row = ("Acme Corp", "greenhouse", "active", "A",
           "https://acme.example/careers", "acme", None, "tech")
    monkeypatch.setattr(company_registry, "get_conn",
                        lambda: _FakeConn([row]))

    companies = company_registry._build_companies_from_db()

    assert companies  # non-empty
    assert "Acme Corp" in companies
    assert company_registry.registry_load_failed() is False
    assert company_registry.REGISTRY_LOAD_ERROR is None


def test_db_failure_sets_failure_signal(monkeypatch):
    """get_conn raising (DB outage) → {} returned AND failure flag set True."""
    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(company_registry, "get_conn", _boom)

    companies = company_registry._build_companies_from_db()

    assert companies == {}
    assert company_registry.REGISTRY_LOAD_FAILED is True
    assert company_registry.registry_load_failed() is True
    assert "connection refused" in (company_registry.REGISTRY_LOAD_ERROR or "")


def test_failure_then_success_resets_flag(monkeypatch):
    """A failed load followed by a healthy one flips the flag back to False —
    the reset contract /jobs-new relies on (outage → recovery, no stale True)."""
    monkeypatch.setattr(company_registry, "get_conn",
                        lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert company_registry._build_companies_from_db() == {}
    assert company_registry.registry_load_failed() is True

    row = ("Acme Corp", "greenhouse", "active", "A",
           "https://acme.example/careers", "acme", None, "tech")
    monkeypatch.setattr(company_registry, "get_conn", lambda: _FakeConn([row]))
    assert company_registry._build_companies_from_db()  # non-empty
    assert company_registry.registry_load_failed() is False
    assert company_registry.REGISTRY_LOAD_ERROR is None
