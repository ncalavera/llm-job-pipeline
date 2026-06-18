"""The custom boost emitted by the LLM must reach scoring/tiering.

The bug: the company-scoring prompt asks the LLM for a user-configurable key
(CUSTOM_BOOST_FIELD, default 'career_narrative_boost'), but the ingestion code
whitelisted only the legacy 'mpa_narrative_boost'. So a user following the docs
emitted a boost that the code silently dropped — it never moved the tier.

These tests prove:
  1. the configured key and the prompt's placeholder agree;
  2. _extract_enrichment preserves the configured boost key;
  3. _read_custom_boost reads it (and the legacy key for back-compat);
  4. the boost actually reaches calculate_company_tier (moves the composite).
"""
import sys
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import prompts  # noqa: E402
import score_companies as sc  # noqa: E402
from database_supabase import calculate_company_tier  # noqa: E402


def test_prompt_asks_for_configured_key():
    """The rendered company prompt contains the configured boost key, not a
    stale legacy literal."""
    assert prompts.CUSTOM_BOOST_FIELD == "career_narrative_boost"
    assert prompts.CUSTOM_BOOST_FIELD in prompts.COMPANY_SCORING_PROMPT
    # The placeholder must be fully substituted (no raw {{...}} left).
    assert "{{CUSTOM_BOOST_FIELD}}" not in prompts.COMPANY_SCORING_PROMPT


def test_extract_enrichment_keeps_configured_boost():
    """_extract_enrichment preserves the configured boost key + reasoning."""
    result = {
        "about": {"description": "An org that does good work in the world."},
        "mission_fit": {
            "alignment_score": 70,
            "alignment_label": "Good fit",
            "career_narrative_boost": 80,
            "career_narrative_boost_reasoning": "Strong brand for the CV.",
        },
    }
    enr = sc._extract_enrichment(result)
    assert enr is not None
    mf = enr["mission_fit"]
    assert mf["career_narrative_boost"] == 80
    assert mf["career_narrative_boost_reasoning"] == "Strong brand for the CV."


def test_read_custom_boost_configured_key():
    assert sc._read_custom_boost({"career_narrative_boost": 65}) == 65


def test_read_custom_boost_legacy_back_compat():
    """Old payloads using the legacy key still resolve."""
    assert sc._read_custom_boost({"mpa_narrative_boost": 55}) == 55


def test_read_custom_boost_absent_or_bad():
    assert sc._read_custom_boost({}) is None
    assert sc._read_custom_boost({"career_narrative_boost": "high"}) is None
    assert sc._read_custom_boost(None) is None


def test_boost_reaches_tier_calculation():
    """The extracted boost moves the composite vs. alignment-only."""
    result = {
        "about": {"description": "An org that does good work in the world."},
        "mission_fit": {"alignment_score": 40, "career_narrative_boost": 90},
    }
    enr = sc._extract_enrichment(result)
    boost = sc._read_custom_boost(enr["mission_fit"])
    assert boost == 90

    tier_no_boost, comp_no_boost = calculate_company_tier(40)
    tier_boost, comp_boost = calculate_company_tier(40, boost)
    # The boost must change the composite (it actually reached the calc).
    assert comp_boost != comp_no_boost
    assert comp_boost > comp_no_boost  # 90 boost lifts a 40 alignment
