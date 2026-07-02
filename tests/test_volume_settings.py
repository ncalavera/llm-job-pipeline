"""Tests for the single [volume] dial.

The [volume] block in config/defaults.toml is ONE window for "how many vacancies
do I want to see". Each key is wired to the REAL lever it names — proven here:

  1. settings.volume() reads the block, with neutral / non-positive fallbacks.
  2. digest_size    → settings.digest()["default_limit"]  (telegram_digest --limit).
  3. daily_scoring_limit → scoring_settings.max_per_run()  (score_vacancies --limit),
     with the profile's ## VOLUME max_per_run still overriding it.
  4. max_active_companies → fetch_vacancies._apply_company_cap (company selection).
  5. run_daily prints the current volumes at run start, and SUGGESTS (never
     applies) dialing down when the review backlog is large.

Offline, invented data, temp TOML / temp profile — never the maintainer's files.
"""

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    settings.clear_cache()
    yield
    settings.clear_cache()


def _point_at(monkeypatch, path: Path):
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(path))
    settings.clear_cache()


# ---------------------------------------------------------------------------
# 1. settings.volume()
# ---------------------------------------------------------------------------


def test_volume_shipped_defaults():
    v = settings.volume()
    assert v == {"max_active_companies": 200, "daily_scoring_limit": 150, "digest_size": 5}


def test_volume_reads_from_toml(tmp_path, monkeypatch):
    p = tmp_path / "defaults.toml"
    p.write_text(
        textwrap.dedent(
            """
            [volume]
            max_active_companies = 12
            daily_scoring_limit = 7
            digest_size = 2
            """
        ),
        encoding="utf-8",
    )
    _point_at(monkeypatch, p)
    assert settings.volume() == {
        "max_active_companies": 12,
        "daily_scoring_limit": 7,
        "digest_size": 2,
    }


def test_volume_missing_section_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "defaults.toml"
    p.write_text("[thresholds]\nllm_score_threshold = 5\n", encoding="utf-8")
    _point_at(monkeypatch, p)
    assert settings.volume()["daily_scoring_limit"] == 150


@pytest.mark.parametrize("bad", [0, -3, "lots"])
def test_volume_non_positive_or_garbage_falls_back(tmp_path, monkeypatch, bad):
    p = tmp_path / "defaults.toml"
    val = f'"{bad}"' if isinstance(bad, str) else bad
    p.write_text(f"[volume]\ndigest_size = {val}\n", encoding="utf-8")
    _point_at(monkeypatch, p)
    assert settings.volume()["digest_size"] == 5  # never "send zero"


# ---------------------------------------------------------------------------
# 2. digest_size → digest default_limit
# ---------------------------------------------------------------------------


def test_digest_default_limit_comes_from_volume(tmp_path, monkeypatch):
    p = tmp_path / "defaults.toml"
    p.write_text("[volume]\ndigest_size = 9\n", encoding="utf-8")
    _point_at(monkeypatch, p)
    assert settings.digest()["default_limit"] == 9


def test_digest_default_limit_shipped_is_five():
    assert settings.digest()["default_limit"] == 5


# ---------------------------------------------------------------------------
# 3. daily_scoring_limit → scoring_settings.max_per_run (profile overrides)
# ---------------------------------------------------------------------------


def _profile(tmp_path, volume_block):
    body = "## USER_PROFILE\n\nTest person.\n"
    if volume_block is not None:
        body += f"\n## VOLUME\n\n{volume_block}\n"
    path = tmp_path / "user_profile.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_max_per_run_default_reads_daily_scoring_limit(tmp_path, monkeypatch):
    import prompts
    import scoring_settings as ss

    toml = tmp_path / "defaults.toml"
    toml.write_text("[volume]\ndaily_scoring_limit = 33\n", encoding="utf-8")
    _point_at(monkeypatch, toml)
    # No profile VOLUME section → the neutral [volume] default applies.
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, None)))
    prompts.clear_profile_cache()
    assert ss.max_per_run() == 33


def test_profile_max_per_run_overrides_volume(tmp_path, monkeypatch):
    import prompts
    import scoring_settings as ss

    toml = tmp_path / "defaults.toml"
    toml.write_text("[volume]\ndaily_scoring_limit = 33\n", encoding="utf-8")
    _point_at(monkeypatch, toml)
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, "max_per_run: 88")))
    prompts.clear_profile_cache()
    assert ss.max_per_run() == 88  # personal plan-tier knob wins


# ---------------------------------------------------------------------------
# 4. max_active_companies → fetch cap helper
# ---------------------------------------------------------------------------


def test_apply_company_cap_defers_least_overdue():
    import fetch_vacancies as fv

    companies = {"a": 1, "b": 2, "c": 3, "d": 4}
    # ordered most-overdue first; cap 2 keeps the first two, defers the rest.
    kept, deferred = fv._apply_company_cap(["c", "a", "d", "b"], companies, 2)
    assert set(kept) == {"c", "a"}
    assert deferred == 2


def test_apply_company_cap_no_cap_when_within_limit():
    import fetch_vacancies as fv

    companies = {"a": 1, "b": 2}
    assert fv._apply_company_cap(["a", "b"], companies, 5) == (companies, 0)


def test_apply_company_cap_zero_means_unlimited():
    import fetch_vacancies as fv

    companies = {"a": 1, "b": 2}
    kept, deferred = fv._apply_company_cap(["a", "b"], companies, 0)
    assert kept == companies and deferred == 0


# ---------------------------------------------------------------------------
# 5. run-start banner + overload advice
# ---------------------------------------------------------------------------


@pytest.fixture()
def rd(monkeypatch):
    sys.modules.pop("run_daily", None)
    import run_daily

    importlib.reload(run_daily)
    return run_daily


def test_boards_summary_phrasing(rd):
    assert "tracked companies only" in rd._boards_summary(rd.Opts(job_boards=None))
    assert rd._boards_summary(rd.Opts(job_boards="all")) == "all defined boards"
    assert rd._boards_summary(rd.Opts(job_boards="80k_hours,linkedin")) == "80k_hours,linkedin"


def test_overload_advice_triggers_above_threshold(rd, monkeypatch):
    monkeypatch.setattr(rd, "_scored_unseen", lambda: rd.OVERLOAD_BACKLOG + 5)
    advice = rd._overload_advice()
    assert advice is not None
    # Names all three real levers, and is explicitly propose-only.
    assert "disable-board" in advice
    assert "daily_scoring_limit" in advice
    assert "HARD_FILTERS" in advice
    assert "nothing changes unless you do it" in advice


def test_overload_advice_silent_below_threshold(rd, monkeypatch):
    monkeypatch.setattr(rd, "_scored_unseen", lambda: rd.OVERLOAD_BACKLOG - 1)
    assert rd._overload_advice() is None


def test_overload_advice_survives_db_error(rd, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(rd, "_scored_unseen", boom)
    assert rd._overload_advice() is None  # never propagates


def test_run_banner_shows_volumes_and_boards(rd, monkeypatch, capsys):
    monkeypatch.setattr(rd, "_scalar", lambda *a, **k: 17)  # active companies
    monkeypatch.setattr(rd, "_scored_unseen", lambda: 0)  # no overload
    rd._print_run_banner(rd.Opts(job_boards="idealist"))
    out = capsys.readouterr().out
    assert "today's volume" in out
    assert "17" in out  # active company count
    assert "idealist" in out  # boards this run
    assert "150" in out  # shipped daily_scoring_limit / max_per_run
    assert "Digest size: 5" in out


def test_run_banner_never_raises_on_db_failure(rd, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("no db")

    monkeypatch.setattr(rd, "_scalar", boom)
    rd._print_run_banner(rd.Opts(job_boards=None))  # must not raise
    # Degrades to "?" for the DB-derived count rather than crashing.
    assert "?" in capsys.readouterr().out
