"""Tests for the ``## VOLUME`` cost/volume settings + the per-run scoring cap.

The scoring model and the spike-day per-run cap are per-user knobs tied to the
plan tier, so they live in the profile (``## VOLUME``), read by
``scripts/scoring_settings.py``. Covered here:

  1. ``scoring_model`` / ``max_per_run`` read from a profile fixture.
  2. Missing section / missing key / placeholder / garbage / unknown value all
     fall back to the neutral defaults (sonnet, 150) — never raise.
  3. HTML-comment example lines are not parsed as real values.
  4. With no explicit ``--limit``, the per-run cap CUTS the role list and the
     run's stderr honestly reports "Scoring X of Y" plus a deferral note.

Never reads the maintainer's real ``config/user_profile.md`` — every profile is
a ``tmp_path`` fixture pinned via ``USER_PROFILE_PATH``.

Absorbed tests/test_default_limits.py — the fresh-install guardrail that pins
the shipped per-run cap defaults as finite/positive and proves both scoring
entry points (score_vacancies.cmd_local, score_companies.cmd_local) apply
that default cap when no --limit is given.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

import prompts
import scoring_settings as ss
import settings


# ---------------------------------------------------------------------------
# Profile fixtures
# ---------------------------------------------------------------------------


def _write_profile(tmp_path: Path, volume_block: str | None) -> Path:
    """Write a minimal profile; ``volume_block=None`` omits the VOLUME section.

    Section headers stay at column 0 — the profile parser requires that.
    """
    body = "## USER_PROFILE\n\nTest person.\n\n"
    if volume_block is not None:
        body += f"## VOLUME\n\n{volume_block}\n\n"
    body += "## OUTPUT_LANGUAGE\n\nEnglish\n"
    profile = tmp_path / "user_profile.md"
    profile.write_text(body, encoding="utf-8")
    return profile


@pytest.fixture()
def use_profile(tmp_path, monkeypatch):
    """Return a helper that pins the profile reader at a tmp VOLUME block."""

    def _apply(volume_block: str | None):
        path = _write_profile(tmp_path, volume_block)
        monkeypatch.setenv("USER_PROFILE_PATH", str(path))
        prompts.clear_profile_cache()
        return path

    yield _apply
    prompts.clear_profile_cache()


# ---------------------------------------------------------------------------
# scoring_model
# ---------------------------------------------------------------------------


def test_scoring_model_read_from_profile(use_profile):
    use_profile("scoring_model: opus\nmax_per_run: 150")
    assert ss.scoring_model() == "opus"


def test_scoring_model_case_insensitive(use_profile):
    use_profile("scoring_model: OPUS")
    assert ss.scoring_model() == "opus"


def test_scoring_model_default_when_section_missing(use_profile):
    use_profile(None)
    assert ss.scoring_model() == ss.DEFAULT_SCORING_MODEL == "sonnet"


def test_scoring_model_default_when_key_missing(use_profile):
    use_profile("max_per_run: 200")
    assert ss.scoring_model() == "sonnet"


def test_scoring_model_placeholder_is_default(use_profile):
    use_profile("scoring_model: (none)")
    assert ss.scoring_model() == "sonnet"


def test_scoring_model_unknown_value_is_default(use_profile):
    use_profile("scoring_model: gpt-9")
    assert ss.scoring_model() == "sonnet"


# ---------------------------------------------------------------------------
# screen_model (two-pass cheap screen)
# ---------------------------------------------------------------------------


def test_screen_model_default_is_haiku(use_profile):
    use_profile("scoring_model: opus")
    assert ss.screen_model() == ss.DEFAULT_SCREEN_MODEL == "haiku"


def test_screen_model_read_from_profile(use_profile):
    use_profile("scoring_model: opus\nscreen_model: sonnet")
    assert ss.screen_model() == "sonnet"


def test_screen_model_unknown_value_is_default(use_profile):
    use_profile("scoring_model: opus\nscreen_model: gpt-9")
    assert ss.screen_model() == "haiku"


def test_screen_model_clamped_never_pricier_than_strong(use_profile):
    # A sonnet strong model with an opus screen is nonsensical (screen would cost
    # more than the final pass) — clamp the screen down to the strong tier.
    use_profile("scoring_model: sonnet\nscreen_model: opus")
    assert ss.screen_model() == "sonnet"


def test_screen_model_equal_tier_is_allowed(use_profile):
    use_profile("scoring_model: haiku\nscreen_model: haiku")
    assert ss.screen_model() == "haiku"


# ---------------------------------------------------------------------------
# escalation_threshold (two-pass finalist floor)
# ---------------------------------------------------------------------------


def test_escalation_threshold_default(use_profile):
    use_profile("scoring_model: sonnet")
    assert ss.escalation_threshold() == ss.DEFAULT_ESCALATION_THRESHOLD == 40


def test_escalation_threshold_read_from_profile(use_profile):
    use_profile("escalate_threshold: 55")
    assert ss.escalation_threshold() == 55


def test_escalation_threshold_garbage_is_default(use_profile):
    use_profile("escalate_threshold: lots")
    assert ss.escalation_threshold() == 40


def test_escalation_threshold_clamped_to_0_100(use_profile):
    use_profile("escalate_threshold: 250")
    assert ss.escalation_threshold() == 100
    use_profile("escalate_threshold: -5")
    assert ss.escalation_threshold() == 0


# ---------------------------------------------------------------------------
# escalation_threshold_warning — the clamp accepts 100, but that silently
# disables the strong pass; this is the loud one-liner that says so.
# ---------------------------------------------------------------------------


def test_escalation_threshold_warning_none_for_a_normal_floor():
    assert ss.escalation_threshold_warning(50) is None
    assert ss.escalation_threshold_warning(70) is None
    # Just under the near-ceiling cutoff — still no warning.
    assert ss.escalation_threshold_warning(ss.NEAR_CEILING_THRESHOLD - 1) is None


def test_escalation_threshold_warning_fires_at_and_above_near_ceiling():
    warn_at = ss.escalation_threshold_warning(ss.NEAR_CEILING_THRESHOLD)
    warn_100 = ss.escalation_threshold_warning(100)
    assert warn_at is not None and "escalate nothing" in warn_at
    assert warn_100 is not None and "escalate nothing" in warn_100


# ---------------------------------------------------------------------------
# max_per_run
# ---------------------------------------------------------------------------


def test_max_per_run_read_from_profile(use_profile):
    use_profile("scoring_model: sonnet\nmax_per_run: 120")
    assert ss.max_per_run() == 120


def test_max_per_run_default_when_missing(use_profile):
    use_profile("scoring_model: sonnet")
    assert ss.max_per_run() == ss.DEFAULT_MAX_PER_RUN == 150


def test_max_per_run_garbage_is_default(use_profile):
    use_profile("max_per_run: lots")
    assert ss.max_per_run() == 150


def test_max_per_run_nonpositive_is_default(use_profile):
    use_profile("max_per_run: 0")
    assert ss.max_per_run() == 150


def test_volume_html_comment_examples_ignored(use_profile):
    use_profile(
        "<!-- example: scoring_model: opus\nmax_per_run: 999 -->\n"
        "scoring_model: sonnet\nmax_per_run: 150"
    )
    assert ss.scoring_model() == "sonnet"
    assert ss.max_per_run() == 150


def test_missing_profile_file_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", "/nonexistent/does_not_exist.md")
    prompts.clear_profile_cache()
    try:
        assert ss.scoring_model() == "sonnet"
        assert ss.max_per_run() == 150
    finally:
        prompts.clear_profile_cache()


# ---------------------------------------------------------------------------
# company_paid_min_vacancy_score — the vacancy-first gate floor ([volume] knob).
# Unlike the profile knobs above it lives in defaults.toml, so it is pinned via
# DEFAULTS_TOML_PATH, not USER_PROFILE_PATH.
# ---------------------------------------------------------------------------


def _point_defaults(monkeypatch, tmp_path, volume_block: str) -> None:
    import settings

    p = tmp_path / "defaults.toml"
    p.write_text(f"[volume]\n{volume_block}\n", encoding="utf-8")
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(p))
    settings.clear_cache()


def test_company_paid_min_vacancy_score_default(monkeypatch, tmp_path):
    import settings

    _point_defaults(monkeypatch, tmp_path, "digest_size = 5")
    try:
        assert (
            ss.company_paid_min_vacancy_score() == ss.DEFAULT_COMPANY_PAID_MIN_VACANCY_SCORE == 60
        )
    finally:
        settings.clear_cache()


def test_company_paid_min_vacancy_score_read_from_toml(monkeypatch, tmp_path):
    import settings

    _point_defaults(monkeypatch, tmp_path, "company_paid_min_vacancy_score = 75")
    try:
        assert ss.company_paid_min_vacancy_score() == 75
    finally:
        settings.clear_cache()


def test_company_paid_min_vacancy_score_zero_is_allowed(monkeypatch, tmp_path):
    # Unlike the volume dials, a 0 floor is a legitimate "any scored vacancy earns
    # it" setting and must NOT be coerced up to the default.
    import settings

    _point_defaults(monkeypatch, tmp_path, "company_paid_min_vacancy_score = 0")
    try:
        assert ss.company_paid_min_vacancy_score() == 0
    finally:
        settings.clear_cache()


def test_company_paid_min_vacancy_score_garbage_is_default(monkeypatch, tmp_path):
    import settings

    _point_defaults(monkeypatch, tmp_path, 'company_paid_min_vacancy_score = "lots"')
    try:
        assert ss.company_paid_min_vacancy_score() == 60
    finally:
        settings.clear_cache()


def test_company_paid_min_vacancy_score_clamped_to_100(monkeypatch, tmp_path):
    import settings

    _point_defaults(monkeypatch, tmp_path, "company_paid_min_vacancy_score = 250")
    try:
        assert ss.company_paid_min_vacancy_score() == 100
    finally:
        settings.clear_cache()


# ---------------------------------------------------------------------------
# Per-run cap: cuts the list + honest "Scoring X of Y" message
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_dal(tmp_path, monkeypatch):
    """Fresh SQLite-backed DAL on an isolated temp DB (no Supabase)."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    import database_supabase as db

    yield db
    db.close_conn()


def _seed_vacancies(db, n: int) -> None:
    """Seed ``n`` unscored vacancies under one active company (distinct titles
    so each becomes its own (org, title) role)."""
    db.ensure_company("Acme Robotics", status="active")
    db.save_vacancies(
        "Acme Robotics",
        "A",
        [
            {
                "title": f"Programme Manager {i}",
                "snippet": "Lead programmes.",
                "full_description": "Lead our global programme portfolio. " * 8,
                "location": "Berlin, Germany",
                "url": f"https://acme.example/job/{i}",
            }
            for i in range(n)
        ],
    )
    db.get_conn().commit()


def test_load_and_dedup_reports_available_and_truncates(sqlite_dal):
    _seed_vacancies(sqlite_dal, 5)
    import score_vacancies

    importlib.reload(score_vacancies)

    roles, _fmap, stats = score_vacancies._load_and_dedup(limit=2)
    assert stats["roles_available"] == 5
    assert len(roles) == 2

    roles_all, _f, stats_all = score_vacancies._load_and_dedup(limit=None)
    assert stats_all["roles_available"] == 5
    assert len(roles_all) == 5


def test_cmd_local_default_cap_cuts_list_and_message_honest(sqlite_dal, monkeypatch, capsys):
    """No --limit → the profile cap truncates the batch and the stderr line
    honestly says 'Scoring 2 of 5' plus a deferral note."""
    _seed_vacancies(sqlite_dal, 5)

    import scoring_settings

    monkeypatch.setattr(scoring_settings, "max_per_run", lambda: 2)
    monkeypatch.setattr(scoring_settings, "scoring_model", lambda: "sonnet")

    import score_vacancies

    importlib.reload(score_vacancies)

    args = types.SimpleNamespace(
        limit=None, force=False, include_passed=False, no_candidates=False, offset=0
    )
    score_vacancies.cmd_local(args)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert len(payload) == 2  # cap cut 5 → 2 prepared payloads
    assert "Scoring 2 of 5" in captured.err
    assert "Per-run cap reached (2)" in captured.err
    assert "Scoring model: sonnet" in captured.err


def test_cmd_local_explicit_limit_skips_cap_note(sqlite_dal, monkeypatch, capsys):
    """An explicit --limit wins over the cap and prints no cap-reached note."""
    _seed_vacancies(sqlite_dal, 5)

    import scoring_settings

    monkeypatch.setattr(scoring_settings, "max_per_run", lambda: 2)
    monkeypatch.setattr(scoring_settings, "scoring_model", lambda: "opus")

    import score_vacancies

    importlib.reload(score_vacancies)

    args = types.SimpleNamespace(
        limit=3, force=False, include_passed=False, no_candidates=False, offset=0
    )
    score_vacancies.cmd_local(args)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert len(payload) == 3
    assert "Scoring 3 of 5" in captured.err
    assert "Per-run cap reached" not in captured.err
    assert "Scoring model: opus" in captured.err


# ===========================================================================
# --- from test_default_limits.py ---
#
# Guardrail: a fresh install can never do an UNBOUNDED scoring run.
#
# STRATEGY guardrail 3 makes the per-run cap the spike-day safety net: a burst
# day (hundreds of new roles at once) must not silently burn the plan. That
# net only holds if the shipped defaults are real — a finite, positive cap
# that the scoring entry points actually apply when the user has set no
# override and passed no ``--limit``. This pins exactly that, on a simulated
# fresh install (no profile file, no env override):
#
#   1. The volume/scoring default CONSTANTS are positive integers (never 0,
#      None, or unbounded).
#   2. With no profile, ``scoring_settings.max_per_run()`` resolves to the
#      shipped ``[volume] daily_scoring_limit`` — a positive integer, not
#      None.
#   3. Both scoring entry points (``score_vacancies.cmd_local`` and
#      ``score_companies.cmd_local``) APPLY that default cap when ``--limit``
#      is absent: the vacancy loader is handed the finite cap, and the
#      company list is truncated to it.
#
# Offline, invented data, a nonexistent profile path — never the maintainer's
# real ``config/user_profile.md``.
#
# The ``_clear_caches`` autouse fixture below is scoped to this module, same
# as it was in the source file — it now also wraps the VOLUME-settings tests
# above, which is safe: settings.clear_cache() / prompts.clear_profile_cache()
# are idempotent, and those tests already call the same clears themselves.
# ===========================================================================


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

    def fake_load(*, force, include_passed, include_candidates, limit, offset):
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
