"""Tests for the factor model (scripts/factors.py).

Locks the ticket's core invariant: every factor is declared with a strength
(filter / penalty / note), and a NOTE never reaches the scorer — otherwise a
display-only reminder would act as an implicit penalty.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import factors  # noqa: E402


_SECTIONS = {
    "HARD_FILTERS": "exclude_title_keywords: engineer, nurse\nban_regions: (none)\n",
    "EXCLUDE_PATTERNS": "- Gambling and tobacco.\n- Weekend on-call rotations.",
    "NOTES": "- Flag frequent travel.\n- SENTINEL_NOTE_XYZ prefer a written culture.",
}


def test_three_strengths_parse_from_their_sections():
    fs = factors.load_factors(_SECTIONS)
    filt = [f.text for f in factors.by_strength(fs, factors.FILTER)]
    pen = [f.text for f in factors.by_strength(fs, factors.PENALTY)]
    note = [f.text for f in factors.by_strength(fs, factors.NOTE)]
    assert filt == ["engineer", "nurse"]
    assert pen == ["Gambling and tobacco.", "Weekend on-call rotations."]
    assert note == ["Flag frequent travel.", "SENTINEL_NOTE_XYZ prefer a written culture."]


def test_summary_counts_per_strength():
    assert factors.summary(_SECTIONS) == {"filter": 2, "penalty": 2, "note": 2}


def test_notes_are_not_scorer_visible():
    fs = factors.load_factors(_SECTIONS)
    for f in fs:
        assert f.scorer_visible == (f.strength != factors.NOTE)
        assert f.is_note == (f.strength == factors.NOTE)


#: The one profile section that must never reach a scorer template — see
#: factors.py's module docstring. Hardcoded here rather than read off a
#: runtime constant: the guarantee IS this test, checked against every real
#: template file so a newly added one is covered automatically.
_SCORER_HIDDEN_SECTION = "NOTES"


def test_scoring_prompt_never_carries_a_note():
    """The load-bearing guarantee: rendering EVERY real scorer template (not a
    hardcoded pair — the whole prompts dir, so a future template is covered
    too) with a NOTES section present must NOT surface any note text, because
    none of them reference a {{NOTES}} placeholder. That is what keeps a note
    from ever becoming an implicit penalty."""
    from prompts import _PROMPTS_DIR, _render

    templates = sorted(_PROMPTS_DIR.glob("*.md"))
    assert templates, "expected at least one scorer template in scripts/prompts/"
    for tpl_path in templates:
        template = tpl_path.read_text(encoding="utf-8").strip()
        placeholder = "{{" + _SCORER_HIDDEN_SECTION + "}}"
        assert placeholder not in template.replace(" ", ""), tpl_path.name
        rendered = _render(template, _SECTIONS)
        assert "SENTINEL_NOTE_XYZ" not in rendered, tpl_path.name


def test_empty_profile_declares_nothing():
    assert factors.load_factors({}) == []


def test_bundled_example_profile_declares_notes():
    """The shipped example must declare the new NOTE strength so onboarding sees
    all three strengths (neutral, invented factors only)."""
    from prompts import EXAMPLE_PROFILE_PATH, _render  # noqa: F401

    text = EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8")
    assert "## NOTES" in text
