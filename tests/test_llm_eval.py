"""Live LLM scoring eval — RELATIVE sanity on a tiny golden set.

SCAFFOLD ONLY: skipped unless BOTH ``RUN_LLM_EVAL=1`` and ``ANTHROPIC_API_KEY``
are set (it costs money and needs network). It is deliberately PLACE- and
PERSON-agnostic: it scores generic placeholder vacancies against the bundled
*example* profile (config/user_profile.example.md), and asserts ORDERING /
BUCKETS, never exact numbers — so it stays robust to model drift and prompt
tweaks.

The golden expectation:
  * a junk / non-job posting ("Talent Pool — General Application") scores LOW;
  * a role matching the example profile scores HIGHER than a clearly
    mismatching role.

Run it explicitly:
    RUN_LLM_EVAL=1 ANTHROPIC_API_KEY=sk-... pytest tests/test_llm_eval.py -m eval
"""

import os

import pytest

pytestmark = pytest.mark.eval

_RUN = os.environ.get("RUN_LLM_EVAL") == "1" and bool(
    os.environ.get("ANTHROPIC_API_KEY")
)
skip_reason = "set RUN_LLM_EVAL=1 and ANTHROPIC_API_KEY to run the live LLM eval"

# Model id kept in one place; override via env for cheaper/newer models.
EVAL_MODEL = os.environ.get("LLM_EVAL_MODEL", "claude-sonnet-4-5")


# ---------------------------------------------------------------------------
# Golden set — generic, placeholder roles. No real org, no real person.
# ---------------------------------------------------------------------------

def _vac(title, description, *, org="Placeholder Org"):
    return {
        "org": org,
        "tier": "B",
        "title": title,
        "locations": [{"work_mode": "remote"}],
        "full_description": description,
    }


# A role that matches a generic operations/strategy profile.
MATCH_VAC = _vac(
    "Head of Operations",
    "Lead cross-functional operations for a mission-driven organisation. Own "
    "planning, hiring, vendor and budget coordination, and scale internal "
    "processes. 6+ years in operations or chief-of-staff roles. English working "
    "language.",
)

# A role clearly OUTSIDE a generalist ops profile (deep specialist IC).
MISMATCH_VAC = _vac(
    "Senior Embedded Firmware Engineer",
    "Design and ship real-time firmware in C/C++ for custom silicon. Requires "
    "10+ years low-level embedded development, RTOS internals, and hardware "
    "bring-up. Deep DSP and driver experience mandatory.",
)

# A non-job speculative posting — should score LOW for anyone.
JUNK_VAC = _vac(
    "Talent Pool — General Application",
    "We are always interested in great people. Register your interest and we "
    "will reach out when a relevant role opens. This is not a specific opening.",
)


def _score_one(vac):
    """Score a single vacancy through the real prompt + the Anthropic SDK.

    Reuses score_vacancies._build_user_msg + the loaded VACANCY_SCORING_PROMPT so
    the eval exercises the SAME input the pipeline builds — just with the example
    profile instead of a personal one.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

    from anthropic import Anthropic
    import score_vacancies
    from prompts import VACANCY_SCORING_PROMPT

    user_msg = score_vacancies._build_user_msg(vac)
    client = Anthropic()
    resp = client.messages.create(
        model=EVAL_MODEL,
        max_tokens=1024,
        system=VACANCY_SCORING_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    parsed = score_vacancies._parse_json(text)
    assert "score" in parsed, f"model returned no score: {text[:300]}"
    return int(parsed["score"])


@pytest.fixture(scope="module")
def use_example_profile():
    """Point the prompt loader at the bundled EXAMPLE profile (generic)."""
    from pathlib import Path
    example = Path(__file__).resolve().parent.parent / "config" / "user_profile.example.md"
    assert example.exists(), "example profile must ship for the eval"
    saved = os.environ.get("USER_PROFILE_PATH")
    os.environ["USER_PROFILE_PATH"] = str(example)
    import importlib, sys
    sys.path.insert(0, str(example.parent.parent / "scripts"))
    import prompts
    importlib.reload(prompts)
    yield
    if saved is None:
        os.environ.pop("USER_PROFILE_PATH", None)
    else:
        os.environ["USER_PROFILE_PATH"] = saved
    importlib.reload(prompts)


@pytest.mark.skipif(not _RUN, reason=skip_reason)
def test_junk_scores_low(use_example_profile):
    """A non-job speculative posting scores low (the prompt caps these)."""
    junk = _score_one(JUNK_VAC)
    assert junk <= 35, f"junk scored {junk}, expected <= 35"


@pytest.mark.skipif(not _RUN, reason=skip_reason)
def test_match_outranks_mismatch(use_example_profile):
    """A profile-matching role scores higher than a clearly-mismatching one."""
    match = _score_one(MATCH_VAC)
    mismatch = _score_one(MISMATCH_VAC)
    assert match > mismatch, (
        f"expected match ({match}) > mismatch ({mismatch})"
    )


@pytest.mark.skipif(not _RUN, reason=skip_reason)
def test_full_ordering_buckets(use_example_profile):
    """match > mismatch and junk lands in the low bucket — ordering, not numbers."""
    match = _score_one(MATCH_VAC)
    mismatch = _score_one(MISMATCH_VAC)
    junk = _score_one(JUNK_VAC)
    assert match > mismatch
    assert junk <= 35
    assert match >= 40, f"a clearly matching role should clear 40, got {match}"
