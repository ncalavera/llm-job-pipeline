"""The ONE product language — resolver + the surfaces it drives.

The language is chosen once (profile ``## OUTPUT_LANGUAGE``) and becomes the
language of the whole product. This proves:

  1. the resolution order (env → profile → legacy [dashboard] → en),
  2. the table-driven alias mapping + graceful fallback for an unbundled language,
  3. that flipping the profile flips the Python surfaces — the run banner +
     summary printed by run_daily, via one ``product_language.t()`` helper.

Offline, invented data, a temp profile — never the maintainer's files.
"""

import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import product_language as pl  # noqa: E402
import prompts  # noqa: E402
import settings  # noqa: E402


def _profile(tmp_path, output_language):
    body = "## USER_PROFILE\n\nTest person.\n"
    if output_language is not None:
        body += f"\n## OUTPUT_LANGUAGE\n\n{output_language}\n"
    path = tmp_path / "user_profile.md"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No stray env override; a fresh profile / settings parse each test."""
    monkeypatch.delenv("PRODUCT_LANGUAGE", raising=False)
    monkeypatch.delenv("DASHBOARD_LANGUAGE", raising=False)
    prompts.clear_profile_cache()
    settings.clear_cache()
    yield
    prompts.clear_profile_cache()
    settings.clear_cache()


# ---------------------------------------------------------------------------
# 1. resolution order
# ---------------------------------------------------------------------------


def test_profile_output_language_drives_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, "Russian")))
    prompts.clear_profile_cache()
    assert pl.resolve() == "ru"


def test_english_profile_resolves_en(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, "English")))
    prompts.clear_profile_cache()
    assert pl.resolve() == "en"


@pytest.mark.parametrize(
    "token,code",
    [
        ("en", "en"),
        ("EN", "en"),
        ("english", "en"),
        ("English", "en"),
        ("ru", "ru"),
        ("RU", "ru"),
        ("Russian", "ru"),
        ("русский", "ru"),
    ],
)
def test_alias_table_is_case_insensitive(tmp_path, monkeypatch, token, code):
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, token)))
    prompts.clear_profile_cache()
    assert pl.resolve() == code


def test_unknown_language_falls_back_to_en(tmp_path, monkeypatch):
    # A language with no bundled UI table degrades to English for the chrome.
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, "French")))
    prompts.clear_profile_cache()
    assert pl.resolve() == "en"


def test_missing_section_falls_back_to_en(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, None)))
    prompts.clear_profile_cache()
    assert pl.resolve() == "en"


def test_env_override_beats_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, "English")))
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    prompts.clear_profile_cache()
    assert pl.resolve() == "ru"


def test_legacy_toml_dashboard_language_used_when_profile_silent(tmp_path, monkeypatch):
    toml = tmp_path / "defaults.toml"
    toml.write_text('[dashboard]\nlanguage = "ru"\n', encoding="utf-8")
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(toml))
    settings.clear_cache()
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, None)))
    prompts.clear_profile_cache()
    assert pl.resolve() == "ru"


def test_profile_beats_legacy_toml_head_to_head(tmp_path, monkeypatch):
    """Direct precedence proof (review nit on #46): both sources set, profile wins."""
    toml = tmp_path / "defaults.toml"
    toml.write_text('[dashboard]\nlanguage = "ru"\n', encoding="utf-8")
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(toml))
    settings.clear_cache()
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, "English")))
    prompts.clear_profile_cache()
    assert pl.resolve() == "en"


def test_unbundled_profile_language_degrades_loudly(tmp_path, monkeypatch, capsys):
    """Review nit on #46: a named-but-unbundled language must say it degraded."""
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, "French")))
    monkeypatch.delenv("PRODUCT_LANGUAGE", raising=False)
    prompts.clear_profile_cache()
    assert pl.resolve() == "en"
    assert "French" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 2. the t() helper + label
# ---------------------------------------------------------------------------


def test_t_translates_and_formats(monkeypatch):
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    assert pl.t("banner_title") == "/jobs-new — объём на сегодня"
    assert "5" in pl.t("digest_header", n=5)


def test_t_unknown_key_returns_key():
    assert pl.t("no_such_key_xyz") == "no_such_key_xyz"


def test_t_formatting_mismatch_degrades():
    # A value with placeholders, called with no args, must not raise.
    assert pl.t("banner_active") == pl.strings()["banner_active"]


def test_language_label(monkeypatch):
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    assert pl.language_label() == "Русский"
    assert pl.language_label("en") == "English"


# ---------------------------------------------------------------------------
# 3. run_daily banner + summary + overload advice switch language
# ---------------------------------------------------------------------------


@pytest.fixture()
def rd():
    sys.modules.pop("run_daily", None)
    import run_daily

    importlib.reload(run_daily)
    return run_daily


def test_run_banner_switches_to_russian(rd, monkeypatch, capsys):
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    monkeypatch.setattr(rd, "_scalar", lambda *a, **k: 17)
    monkeypatch.setattr(rd, "_scored_unseen", lambda: 0)
    rd._print_run_banner(rd.Opts(job_boards="idealist"))
    out = capsys.readouterr().out
    assert "объём на сегодня" in out  # banner title in Russian
    assert "17" in out  # active company count still shown
    assert "idealist" in out  # boards passthrough unchanged


def test_boards_summary_switches(rd, monkeypatch):
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    assert "только отслеживаемые" in rd._boards_summary(rd.Opts(job_boards=None))


def test_overload_advice_translates_prose_keeps_commands(rd, monkeypatch):
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    monkeypatch.setattr(rd, "_scored_unseen", lambda: rd.OVERLOAD_BACKLOG + 1)
    advice = rd._overload_advice()
    assert "Очередь на разбор" in advice  # prose translated
    assert "disable-board" in advice  # command path stays literal
    assert "daily_scoring_limit" in advice
    assert "HARD_FILTERS" in advice


def test_summary_line_switches(rd, monkeypatch):
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    from product_language import t

    assert t("summary_done") == "✓ /jobs-new завершён"
    assert "избранном" in t("summary_verdicts", scored_unseen=3, liked=1)


# ---------------------------------------------------------------------------
# 4. the dashboard DEFAULT language follows the profile (feeds config.language)
# ---------------------------------------------------------------------------


def test_dashboard_default_language_follows_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, "Russian")))
    prompts.clear_profile_cache()
    import report

    assert report._resolve_language() == "ru"


def test_dashboard_env_override_beats_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", str(_profile(tmp_path, "Russian")))
    monkeypatch.setenv("DASHBOARD_LANGUAGE", "en")
    prompts.clear_profile_cache()
    import report

    assert report._resolve_language() == "en"
