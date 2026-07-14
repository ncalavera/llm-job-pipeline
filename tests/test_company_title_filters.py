"""Tests for the per-company title INCLUDE-filters.

``## COMPANY_TITLE_FILTERS`` is the opposite of the GLOBAL HARD_FILTERS: it keeps
a high-volume company active but lets ONLY profile-relevant titles through to
scoring. For a LISTED company a vacancy survives the filter stage only when its
title matches one of the company's include patterns; UNLISTED companies are
untouched; a missing/empty section is the feature off.

These prove:
  * the parser reads ``- <company> :: <patterns>`` entries and skips malformed
    lines with a loud warning (never a crash);
  * a listed company + non-matching title is DROPPED with a rule-named reason;
  * a listed company + matching title PASSES;
  * an unlisted company is UNTOUCHED;
  * an empty/absent section is a no-op.

Because config.py and filters.py compute their constants at import time, the
end-to-end tests reload that chain under a temporary USER_PROFILE_PATH (mirroring
tests/test_hard_filters.py).
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = str((Path(__file__).resolve().parent.parent / "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import hard_filters as hf


# ---------------------------------------------------------------------------
# Parser unit tests (no module reloading needed)
# ---------------------------------------------------------------------------


def test_parse_basic_entry_lowercases_patterns():
    parsed = hf._parse_company_title_filters(
        "- World Food Programme :: Monitoring, Evaluation, Data"
    )
    assert parsed == {"World Food Programme": ["monitoring", "evaluation", "data"]}


def test_parse_multiple_entries_and_merge_dedup():
    parsed = hf._parse_company_title_filters(
        "- WFP :: monitoring, data\n"
        "* FHI 360 :: research, strategy\n"
        "- WFP :: data, evaluation\n"  # merges into WFP, dedups "data"
    )
    assert parsed["WFP"] == ["monitoring", "data", "evaluation"]
    assert parsed["FHI 360"] == ["research", "strategy"]


def test_parse_ignores_html_comment_examples():
    body = "<!-- - Example Org :: should, not, parse -->\n- Real Org :: policy officer\n"
    parsed = hf._parse_company_title_filters(body)
    assert parsed == {"Real Org": ["policy officer"]}


def test_parse_ignores_plain_prose():
    parsed = hf._parse_company_title_filters("This is just a sentence with no separator.\n")
    assert parsed == {}


def test_parse_empty_body_is_off():
    assert hf._parse_company_title_filters("") == {}


def test_parse_malformed_missing_separator_warns_and_skips(capsys):
    parsed = hf._parse_company_title_filters("- World Food Programme\n- OK Org :: data")
    assert parsed == {"OK Org": ["data"]}
    err = capsys.readouterr().err
    assert "COMPANY_TITLE_FILTERS" in err
    assert "malformed" in err


def test_parse_malformed_empty_patterns_warns_and_skips(capsys):
    parsed = hf._parse_company_title_filters("- Empty Org ::   \n- OK Org :: data")
    assert parsed == {"OK Org": ["data"]}
    assert "malformed" in capsys.readouterr().err


def test_load_missing_section_is_off(monkeypatch):
    monkeypatch.setattr(hf, "_load_user_profile", lambda: {"USER_PROFILE": "x"})
    assert hf.load_company_title_filters() == {}


def test_load_reads_section(monkeypatch):
    monkeypatch.setattr(
        hf,
        "_load_user_profile",
        lambda: {"COMPANY_TITLE_FILTERS": "- WFP :: data, evaluation"},
    )
    assert hf.load_company_title_filters() == {"WFP": ["data", "evaluation"]}


# ---------------------------------------------------------------------------
# End-to-end: reload config + filters under a temp profile
# ---------------------------------------------------------------------------


def _write_profile(tmp_path: Path, company_filters_block: str) -> Path:
    """Write a minimal profile with the given COMPANY_TITLE_FILTERS body.

    Headers stay at column 0 (the profile parser requires that).
    """
    profile = tmp_path / "user_profile.md"
    profile.write_text(
        "## USER_PROFILE\n\n"
        "Test person.\n\n"
        "## COMPANY_TITLE_FILTERS\n\n"
        f"{company_filters_block}\n\n"
        "## OUTPUT_LANGUAGE\n\n"
        "English\n",
        encoding="utf-8",
    )
    return profile


def _reload_pipeline(profile_path: Path):
    """Reload the import chain so module-level constants pick up the temp profile.

    Returns (config, filters). filters is reloaded in place by importing
    database_supabase, so its cached COMPANY_TITLE_FILTERS map reflects the new
    profile.
    """
    os.environ["USER_PROFILE_PATH"] = str(profile_path)
    import prompts
    import hard_filters
    import config
    import database_supabase

    importlib.reload(prompts)
    importlib.reload(hard_filters)
    importlib.reload(config)
    importlib.reload(database_supabase)
    import filters

    return config, filters


@pytest.fixture
def restore_default_profile():
    saved = os.environ.get("USER_PROFILE_PATH")
    yield
    if saved is None:
        os.environ.pop("USER_PROFILE_PATH", None)
    else:
        os.environ["USER_PROFILE_PATH"] = saved
    for mod in ("prompts", "hard_filters", "config", "database_supabase", "filters"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])


def test_listed_company_nonmatching_title_is_dropped(tmp_path, restore_default_profile):
    profile = _write_profile(
        tmp_path,
        "- World Food Programme :: monitoring, evaluation, data",
    )
    config, filters = _reload_pipeline(profile)

    assert config.COMPANY_TITLE_FILTERS == {
        "World Food Programme": ["monitoring", "evaluation", "data"]
    }
    reason = filters.company_title_filter_reason("World Food Programme", "Chief Financial Officer")
    assert reason == "company_title_filter — not in World Food Programme include list"


def test_listed_company_matching_title_passes(tmp_path, restore_default_profile):
    profile = _write_profile(
        tmp_path,
        "- World Food Programme :: monitoring, evaluation, data",
    )
    _config, filters = _reload_pipeline(profile)

    # Whole-word, case-insensitive match anywhere in the title.
    assert (
        filters.company_title_filter_reason("World Food Programme", "Senior Data Analyst") is None
    )
    assert (
        filters.company_title_filter_reason(
            "world food programme", "Monitoring & Evaluation Officer"
        )
        is None
    )


def test_unlisted_company_is_untouched(tmp_path, restore_default_profile):
    profile = _write_profile(
        tmp_path,
        "- World Food Programme :: monitoring, evaluation, data",
    )
    _config, filters = _reload_pipeline(profile)

    # A company with no include-list is never dropped, whatever the title.
    assert filters.company_title_filter_reason("Some Other NGO", "Chief Financial Officer") is None


def test_empty_section_is_a_noop(tmp_path, restore_default_profile):
    profile = _write_profile(tmp_path, "")
    config, filters = _reload_pipeline(profile)

    assert config.COMPANY_TITLE_FILTERS == {}
    assert filters.company_title_filter_reason("World Food Programme", "Anything At All") is None


# ---------------------------------------------------------------------------
# Alias resolution, HTML entities, regex-special patterns, compile-once cache
# ---------------------------------------------------------------------------


@pytest.fixture
def filters_mod():
    import filters

    return filters


def test_alias_spelling_still_hits_include_list(monkeypatch, filters_mod):
    """Profile lists the long board spelling; the org arrives as the short alias.

    Both sides go through resolve_canonical_name, so they land on the same key
    and the include-filter still bites — the exact high-volume-org use case.
    """
    aliases = {
        "wfp - world food programme": "World Food Programme",
        "wfp": "World Food Programme",
    }
    monkeypatch.setattr(
        filters_mod, "resolve_canonical_name", lambda n: aliases.get(n.strip().lower(), n)
    )
    monkeypatch.setattr(
        filters_mod,
        "_COMPANY_TITLE_INCLUDE",
        filters_mod._build_company_title_include(
            {"WFP - World Food Programme": ["programme officer", "data"]}
        ),
    )
    # Alias org spelling + non-matching title → still dropped.
    assert (
        filters_mod.company_title_filter_reason("WFP", "Chief Financial Officer")
        == "company_title_filter — not in WFP include list"
    )
    # Alias org spelling + matching title → passes.
    assert filters_mod.company_title_filter_reason("WFP", "Programme Officer") is None


def test_html_entity_org_spelling_matches(monkeypatch, filters_mod):
    """A board delivering the org with &amp; still hits the include-list."""
    monkeypatch.setattr(
        filters_mod,
        "_COMPANY_TITLE_INCLUDE",
        filters_mod._build_company_title_include({"Health & Hope": ["research"]}),
    )
    assert (
        filters_mod.company_title_filter_reason("Health &amp; Hope", "Fundraising Manager")
        == "company_title_filter — not in Health &amp; Hope include list"
    )
    assert filters_mod.company_title_filter_reason("Health &amp; Hope", "Research Officer") is None


def test_regex_special_pattern_is_escaped(monkeypatch, filters_mod):
    """A regex-special include pattern ('M&E') matches literally, never crashes."""
    monkeypatch.setattr(
        filters_mod,
        "_COMPANY_TITLE_INCLUDE",
        filters_mod._build_company_title_include({"Big NGO": ["m&e"]}),
    )
    assert filters_mod.company_title_filter_reason("Big NGO", "M&E Officer") is None
    assert filters_mod.company_title_filter_reason("Big NGO", "Finance Officer") is not None


def test_include_map_holds_precompiled_patterns(monkeypatch, filters_mod):
    """The map caches COMPILED patterns (dict hit + one search per role)."""
    import re as _re

    built = filters_mod._build_company_title_include({"Org A": ["data"], "org a": ["evaluation"]})
    assert list(built) == ["org a"]  # same canonical key → merged, compiled once
    assert all(isinstance(p, _re.Pattern) for p in built.values())
    # Merged pattern list covers both profile spellings' patterns.
    assert built["org a"].search("data analyst")
    assert built["org a"].search("evaluation officer")
