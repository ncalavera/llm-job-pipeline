"""Guardrail: a fresh install can never do an UNBOUNDED scoring run.

STRATEGY guardrail 3 makes the per-run cap the spike-day safety net: a burst day
(hundreds of new roles at once) must not silently burn the plan. That net only
holds if the shipped defaults are real — a finite, positive cap that the scoring
entry points actually apply when the user has set no override and passed no
``--limit``. This test pins exactly that, on a simulated fresh install (no
profile file, no env override):

  1. The volume/scoring default CONSTANTS are positive integers (never 0, None,
     or unbounded).
  2. With no profile, ``scoring_settings.max_per_run()`` resolves to the shipped
     ``[volume] daily_scoring_limit`` — a positive integer, not None.
  3. Both scoring entry points (``score_vacancies.cmd_local`` and
     ``score_companies.cmd_local``) APPLY that default cap when ``--limit`` is
     absent: the vacancy loader is handed the finite cap, and the company list is
     truncated to it.

Offline, invented data, a nonexistent profile path — never the maintainer's real
``config/user_profile.md``.
"""

from __future__ import annotations

import sys
import types

import pytest

import prompts
import scoring_settings as ss
import settings


@pytest.fixture(autouse=True)
def _clear_caches():
    settings.clear_cache()
    prompts.clear_profile_cache()
    yield
    settings.clear_cache()
    prompts.clear_profile_cache()


@pytest.fixture()
def fresh_install(monkeypatch):
    """Simulate a clean clone: no user profile, so every knob falls back to the
    shipped neutral default read from the real config/defaults.toml."""
    monkeypatch.setenv("USER_PROFILE_PATH", "/nonexistent/fresh_install_no_profile.md")
    prompts.clear_profile_cache()
    settings.clear_cache()


@pytest.fixture()
def restore_std():
    """cmd_local swaps sys.stdout↔sys.stderr and does not restore on an early
    raise; keep the swap from leaking into other tests."""
    out, err = sys.stdout, sys.stderr
    yield
    sys.stdout, sys.stderr = out, err


# ---------------------------------------------------------------------------
# 1. The default CONSTANTS are finite, positive integers.
# ---------------------------------------------------------------------------


def test_volume_default_constants_are_positive_ints():
    for key, val in settings._VOLUME_DEFAULTS.items():
        assert isinstance(val, int), f"[volume] {key} default must be an int, got {val!r}"
        assert val > 0, f"[volume] {key} default must be > 0 (never an unbounded run), got {val}"


def test_shipped_volume_dials_are_positive_ints():
    vol = settings.volume()
    for key in ("max_active_companies", "daily_scoring_limit", "digest_size"):
        assert isinstance(vol[key], int) and vol[key] > 0, f"[volume] {key} = {vol[key]!r}"


def test_scoring_default_max_per_run_is_positive_int():
    assert isinstance(ss.DEFAULT_MAX_PER_RUN, int)
    assert ss.DEFAULT_MAX_PER_RUN > 0


# ---------------------------------------------------------------------------
# 2. Fresh install resolves a finite, positive cap (not None / 0 / unbounded).
# ---------------------------------------------------------------------------


def test_fresh_install_scoring_cap_is_finite_and_positive(fresh_install):
    cap = ss.max_per_run()
    assert isinstance(cap, int) and cap > 0
    # With no profile the cap is exactly the shipped neutral volume dial.
    assert cap == settings.volume()["daily_scoring_limit"]


# ---------------------------------------------------------------------------
# 3. Both entry points APPLY the fresh-install default cap when no --limit.
# ---------------------------------------------------------------------------


def test_score_vacancies_entrypoint_applies_default_cap(fresh_install, restore_std):
    """cmd_local with no --limit must hand the loader the finite fresh-install
    cap, never None (an unbounded batch)."""
    import score_vacancies as sv

    default_cap = ss.max_per_run()
    captured: dict[str, object] = {}

    def fake_load(*, force, include_passed, include_candidates, limit, offset, unattended=False):
        captured["limit"] = limit
        return [], {}, {"roles_available": 0}

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(sv, "_load_and_dedup", fake_load)
        args = types.SimpleNamespace(
            limit=None, force=False, include_passed=False, no_candidates=False, offset=0
        )
        sv.cmd_local(args)
    finally:
        monkeypatch.undo()

    assert isinstance(captured["limit"], int) and captured["limit"] > 0
    assert captured["limit"] == default_cap


def test_score_companies_entrypoint_applies_default_cap(fresh_install, restore_std):
    """cmd_local with no --limit must truncate the candidate list to the finite
    fresh-install cap before any scoring work."""
    import score_companies as sc

    default_cap = ss.max_per_run()

    class _Stop(Exception):
        pass

    seen: dict[str, int] = {}

    def spy_evidence(ids):
        seen["n"] = len(ids)
        raise _Stop  # short-circuit before the heavy scoring tail

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            sc,
            "_load_companies",
            lambda **k: [{"id": i, "canonical_name": f"C{i}"} for i in range(default_cap + 25)],
        )
        monkeypatch.setattr(sc, "_load_scrape_cache", lambda: {})
        monkeypatch.setattr(sc, "_load_company_evidence_map", spy_evidence)
        args = types.SimpleNamespace(limit=None, company=None)
        with pytest.raises(_Stop):
            sc.cmd_local(args)
    finally:
        monkeypatch.undo()

    assert seen["n"] == default_cap  # the default_cap + 25 candidates truncate to the finite cap
