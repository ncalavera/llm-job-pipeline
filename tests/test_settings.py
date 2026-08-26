"""Tests for config/defaults.toml: the settings loader, the [volume] dial, and
company-tier cutoffs/weights.

Absorbed from tests/test_settings_loader.py, tests/test_volume_settings.py,
tests/test_company_tier_settings.py. Covers: the loader reads defaults.toml and
degrades to documented neutral fallbacks on a missing file / section / key /
malformed TOML; the single [volume] dial and each key it drives (digest size,
daily scoring limit with profile override, company fetch cap, the run-start
banner and overload advice); and calculate_company_tier reading its weights and
cutoffs from defaults.toml instead of a hardcoded copy.
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


# --- from test_settings_loader.py ---

# ---------------------------------------------------------------------------
# Reads the shipped defaults.toml
# ---------------------------------------------------------------------------


def test_loads_shipped_defaults_non_empty():
    data = settings.load_defaults()
    assert isinstance(data, dict) and data, "shipped defaults.toml should parse non-empty"


def test_thresholds_types_and_values():
    t = settings.thresholds()
    assert isinstance(t["llm_score_threshold"], int)
    assert t["llm_score_threshold"] == 20
    assert t["tier_s"] > t["tier_a"] > t["tier_b"] >= t["tier_c"]
    assert isinstance(t["auto_review"], dict)
    assert t["auto_review"]["enabled"] is False  # opt-in
    # Same default on both backends -- never a derivative of IS_SQLITE.
    assert t["auto_discovery_status"] == "candidate"


# ---------------------------------------------------------------------------
# scoring() — company_evidence_char_cap
# ---------------------------------------------------------------------------


def test_scoring_char_cap_shipped_default():
    assert settings.scoring()["company_evidence_char_cap"] == 25000


def test_scoring_char_cap_reads_from_toml(tmp_path, monkeypatch):
    p = tmp_path / "defaults.toml"
    p.write_text(
        textwrap.dedent(
            """
            [scoring]
            company_evidence_char_cap = 12000
            """
        ),
        encoding="utf-8",
    )
    _point_at(monkeypatch, p)
    assert settings.scoring()["company_evidence_char_cap"] == 12000


def test_scoring_char_cap_missing_section_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "defaults.toml"
    p.write_text("[thresholds]\nllm_score_threshold = 5\n", encoding="utf-8")
    _point_at(monkeypatch, p)
    assert settings.scoring()["company_evidence_char_cap"] == 25000


def test_scoring_char_cap_missing_file_falls_back(tmp_path, monkeypatch):
    _point_at(monkeypatch, tmp_path / "does_not_exist.toml")
    assert settings.scoring()["company_evidence_char_cap"] == 25000


@pytest.mark.parametrize("bad_cap", [0, 999])
def test_scoring_char_cap_below_floor_falls_back(tmp_path, monkeypatch, bad_cap):
    """A cap below the ~1000-char floor is misconfiguration — a value this low
    leaves room for little beyond the "### SOURCE:" labels, so it resets to the
    default instead of silently scoring a company on no content."""
    p = tmp_path / "defaults.toml"
    p.write_text(
        f"[scoring]\ncompany_evidence_char_cap = {bad_cap}\n",
        encoding="utf-8",
    )
    _point_at(monkeypatch, p)
    assert settings.scoring()["company_evidence_char_cap"] == 25000


def test_scoring_char_cap_non_numeric_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "defaults.toml"
    p.write_text('[scoring]\ncompany_evidence_char_cap = "lots"\n', encoding="utf-8")
    _point_at(monkeypatch, p)
    assert settings.scoring()["company_evidence_char_cap"] == 25000


def test_scoring_char_cap_at_floor_is_kept(tmp_path, monkeypatch):
    """The floor itself (1000) is a valid, non-misconfigured value."""
    p = tmp_path / "defaults.toml"
    p.write_text("[scoring]\ncompany_evidence_char_cap = 1000\n", encoding="utf-8")
    _point_at(monkeypatch, p)
    assert settings.scoring()["company_evidence_char_cap"] == 1000


def test_auto_discovery_status_reads_from_toml(tmp_path, monkeypatch):
    toml_path = tmp_path / "defaults.toml"
    toml_path.write_text(
        textwrap.dedent(
            """
            [thresholds]
            auto_discovery_status = "active"
            """
        ),
        encoding="utf-8",
    )
    _point_at(monkeypatch, toml_path)
    assert settings.thresholds()["auto_discovery_status"] == "active"


def test_junk_lists_are_lists_and_neutral():
    j = settings.junk()
    assert isinstance(j["words"], list) and j["words"], "universal junk words present"
    assert isinstance(j["substr"], list)
    assert isinstance(j["desc_substr"], list)


def test_regions_ship_empty():
    r = settings.region_keywords()
    assert r["europe"] == [] and r["us"] == [] and r["remote"] == []


def test_geo_maps_are_sets():
    countries = settings.geo_country_map()
    cities = settings.geo_city_map()
    assert isinstance(countries["uk"], set) and "united kingdom" in countries["uk"]
    assert isinstance(cities["us"], set) and "new york" in cities["us"]
    assert isinstance(settings.geo_city_country(), dict)


def test_boards_have_empty_blacklist():
    boards = settings.boards()
    assert boards, "shipped boards present"
    for cfg in boards.values():
        assert cfg.get("board_blacklist") == [], "every shipped board ships neutral"
    assert boards["hn_whoishiring"]["ttl_days"] == 30


# ---------------------------------------------------------------------------
# Robustness — missing file / section / key → documented neutral fallback
# ---------------------------------------------------------------------------


def test_missing_file_falls_back_to_neutral(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path / "does_not_exist.toml")
    assert settings.load_defaults() == {}
    # Each accessor returns its documented neutral default, no crash.
    assert settings.thresholds()["llm_score_threshold"] == 20
    assert settings.junk()["words"] == []
    assert settings.region_keywords()["europe"] == []
    assert settings.boards() == {}
    assert settings.geo_country_map()["uk"] == set()
    assert settings.parsing_location_hint_cities() == []
    assert settings.digest()["hot_vacancy_score"] == 55


def test_missing_section_falls_back(monkeypatch, tmp_path):
    p = tmp_path / "defaults.toml"
    p.write_text("[thresholds]\nllm_score_threshold = 5\n", encoding="utf-8")
    _point_at(monkeypatch, p)
    # thresholds present...
    assert settings.thresholds()["llm_score_threshold"] == 5
    # ...other sections missing → neutral fallback, not a crash.
    assert settings.junk()["words"] == []
    assert settings.boards() == {}


def test_malformed_toml_does_not_crash(monkeypatch, tmp_path):
    p = tmp_path / "defaults.toml"
    p.write_text("this is = = not valid toml [[[", encoding="utf-8")
    _point_at(monkeypatch, p)
    assert settings.load_defaults() == {}
    assert settings.thresholds()["llm_score_threshold"] == 20


# ---------------------------------------------------------------------------
# A temp defaults.toml drives the loader (config is data, not code)
# ---------------------------------------------------------------------------


def test_temp_toml_overrides_values(monkeypatch, tmp_path):
    p = tmp_path / "defaults.toml"
    p.write_text(
        textwrap.dedent("""
        [thresholds]
        llm_score_threshold = 42
        tier_s = 90

        [junk]
        words = ["custom junk"]

        [boards.myboard]
        strategy = "algolia_api"
        name = "My Board"
        url = "https://example.org"
        ttl_days = 9
    """).strip()
        + "\n",
        encoding="utf-8",
    )
    _point_at(monkeypatch, p)

    assert settings.thresholds()["llm_score_threshold"] == 42
    assert settings.thresholds()["tier_s"] == 90
    assert settings.junk()["words"] == ["custom junk"]
    boards = settings.boards()
    assert set(boards) == {"myboard"}
    assert boards["myboard"]["ttl_days"] == 9
    # board_blacklist auto-defaulted to [] even though the TOML omitted it.
    assert boards["myboard"]["board_blacklist"] == []


# --- from test_volume_settings.py ---
#
# Tests for the single [volume] dial.
#
# The [volume] block in config/defaults.toml is ONE window for "how many vacancies
# do I want to see". Each key is wired to the REAL lever it names — proven here:
#
#   1. settings.volume() reads the block, with neutral / non-positive fallbacks.
#   2. digest_size    → settings.digest()["default_limit"]  (telegram_digest --limit).
#   3. daily_scoring_limit → scoring_settings.max_per_run()  (score_vacancies --limit),
#      with the profile's ## VOLUME max_per_run still overriding it.
#   4. max_active_companies → fetch_vacancies._apply_company_cap (company selection).
#   5. run_daily prints the current volumes at run start, and SUGGESTS (never
#      applies) dialing down when the review backlog is large.
#
# Offline, invented data, temp TOML / temp profile — never the maintainer's files.

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


@pytest.mark.parametrize("bad", [0, "lots"])
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


# --- from test_company_tier_settings.py ---
#
# calculate_company_tier must read its weights/cutoffs from defaults.toml.
#
# The bug: the composite weights (0.70/0.30) and tier cutoffs (65/50/35) were
# hardcoded in scripts/database_supabase.py, so editing config/defaults.toml never
# moved a single tier. These tests point the loader at a temp defaults.toml and
# prove that changing the TOML changes the computed tier + composite.


def _reload_db(monkeypatch, toml_path: Path):
    """Point settings at toml_path and return a fresh database_supabase."""
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(toml_path))
    settings.clear_cache()
    import database_supabase

    importlib.reload(database_supabase)
    return database_supabase


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    monkeypatch.delenv("DEFAULTS_TOML_PATH", raising=False)
    settings.clear_cache()
    import database_supabase

    importlib.reload(database_supabase)


def _write_toml(path: Path, *, s, a, b, alpha, beta):
    path.write_text(
        textwrap.dedent(f"""
        [thresholds]
        tier_s = {s}
        tier_a = {a}
        tier_b = {b}
        tier_c = 0
        composite_alignment_weight = {alpha}
        composite_boost_weight = {beta}
    """).strip()
        + "\n",
        encoding="utf-8",
    )


def test_default_cutoffs_match_shipped(monkeypatch, tmp_path):
    """Sanity: with shipped numbers, alignment 64 → A, 65 → S."""
    toml = tmp_path / "defaults.toml"
    _write_toml(toml, s=65, a=50, b=35, alpha=0.70, beta=0.30)
    db = _reload_db(monkeypatch, toml)
    assert db.calculate_company_tier(64) == ("A", 64.0)
    assert db.calculate_company_tier(65) == ("S", 65.0)
    assert db.calculate_company_tier(49) == ("B", 49.0)
    assert db.calculate_company_tier(34) == ("C", 34.0)


def test_changing_cutoffs_moves_tier(monkeypatch, tmp_path):
    """Lower the S cutoff to 60 → an alignment of 62 becomes S (was A)."""
    toml = tmp_path / "defaults.toml"
    _write_toml(toml, s=60, a=45, b=30, alpha=0.70, beta=0.30)
    db = _reload_db(monkeypatch, toml)
    assert db.calculate_company_tier(62) == ("S", 62.0)  # 62 >= 60 now
    assert db.calculate_company_tier(46) == ("A", 46.0)  # 46 >= 45 now
    assert db.calculate_company_tier(31) == ("B", 31.0)  # 31 >= 30 now


def test_changing_weights_moves_composite(monkeypatch, tmp_path):
    """Custom boost mixing uses the TOML weights, not hardcoded 0.70/0.30."""
    toml = tmp_path / "defaults.toml"
    # Flip the emphasis hard onto the boost: 0.10 alignment + 0.90 boost.
    _write_toml(toml, s=65, a=50, b=35, alpha=0.10, beta=0.90)
    db = _reload_db(monkeypatch, toml)
    # alignment 40, boost 80 → 0.10*40 + 0.90*80 = 4 + 72 = 76.0 → S
    tier, composite = db.calculate_company_tier(40, custom_boost=80)
    assert composite == 76.0
    assert tier == "S"
    # With the shipped 0.70/0.30 this would be 0.70*40 + 0.30*80 = 52.0 → A.


def test_none_alignment_returns_none(monkeypatch, tmp_path):
    toml = tmp_path / "defaults.toml"
    _write_toml(toml, s=65, a=50, b=35, alpha=0.70, beta=0.30)
    db = _reload_db(monkeypatch, toml)
    assert db.calculate_company_tier(None) == (None, None)
