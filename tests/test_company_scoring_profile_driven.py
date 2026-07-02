"""The company-scoring rubric is built from the USER PROFILE, not baked in.

The company scorer must take its desirability rubric — which domains are TOP /
MID / LOW, the reference organisations, the salary benchmark — from the user
profile, exactly the way vacancy scoring does. This suite proves the shipped
template is owner-agnostic: TWO different synthetic profiles (a software engineer
and a nurse) render MEANINGFULLY DIFFERENT rubrics from the SAME template with
zero template edits, and no foreign sector/org/money anchor survives.

Fully offline: it renders the prompt from fixture profiles via the same
`prompts._render` path production uses; it never calls an LLM.
"""

import sys
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import prompts  # noqa: E402
from test_no_hardcoded_data import WORLDVIEW_TOKEN  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ENGINEER = FIXTURES / "profile_engineer.md"
MEDIC = FIXTURES / "profile_medic.md"


def _render_company_prompt(monkeypatch, profile_path: Path) -> str:
    """Render scripts/prompts/company-scoring.md against a given profile using
    the production template + substitution path."""
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile_path))
    prompts.clear_profile_cache()
    sections = prompts._load_user_profile()
    template = prompts._load_template("company-scoring.md")
    return prompts._render(template, sections)


# ---------------------------------------------------------------------------
# The rubric follows the profile
# ---------------------------------------------------------------------------


def test_engineer_profile_injects_its_own_domains(monkeypatch):
    rendered = _render_company_prompt(monkeypatch, ENGINEER)
    # The engineer's stated domains reach the rubric.
    assert "developer tools" in rendered
    assert "cloud infrastructure" in rendered
    # Their target roles and anti-list are injected too.
    assert "Staff Engineer" in rendered
    assert "Adtech" in rendered


def test_medic_profile_injects_its_own_domains(monkeypatch):
    rendered = _render_company_prompt(monkeypatch, MEDIC)
    assert "public health delivery" in rendered
    assert "hospital" in rendered.lower()
    assert "Clinical Nurse Specialist" in rendered
    assert "Tobacco" in rendered


def test_two_profiles_render_different_rubrics(monkeypatch):
    """Same template, different profiles → different rubric text. This is the
    core promise: no template edit is needed to re-target the scorer."""
    eng = _render_company_prompt(monkeypatch, ENGINEER)
    med = _render_company_prompt(monkeypatch, MEDIC)
    assert eng != med
    # Each rubric carries the OTHER's domains nowhere.
    assert "developer tools" in eng and "developer tools" not in med
    assert "Clinical Nurse Specialist" in med and "Clinical Nurse Specialist" not in eng


# ---------------------------------------------------------------------------
# The domain tiers are generated from the profile, not a fixed list
# ---------------------------------------------------------------------------


def test_domain_tiers_are_profile_generated(monkeypatch):
    rendered = _render_company_prompt(monkeypatch, ENGINEER)
    # The instruction tells the model to build the tiers from the candidate's
    # own profile rather than a fixed sector list.
    assert "BUILD THE TIERS FROM THE CANDIDATE'S OWN PROFILE" in rendered
    # TOP tier is defined by the candidate's own "want to work in" / target
    # sectors, and OFF tier by their EXCLUDE PATTERNS — not by hardcoded sectors.
    assert "TARGET ROLES" in rendered
    assert "EXCLUDE PATTERNS" in rendered


def test_all_profile_placeholders_substituted(monkeypatch):
    """No raw {{...}} placeholder survives for the profile-sourced sections."""
    for profile in (ENGINEER, MEDIC):
        rendered = _render_company_prompt(monkeypatch, profile)
        for ph in ("{{USER_PROFILE}}", "{{TARGET_ROLES}}", "{{EXCLUDE_PATTERNS}}"):
            assert ph not in rendered, f"{ph} left unsubstituted for {profile.name}"


# ---------------------------------------------------------------------------
# No foreign anchors survive in the rendered rubric
# ---------------------------------------------------------------------------

# Owner-specific reference organisations, network-prestige tokens and fixed
# sector labels that must never appear in an owner-agnostic template.
_FORBIDDEN_ANCHORS = [
    "givewell",
    "gavi",
    "malengo",
    "coefficient",
    "google.org",
    "fundraiseup",
    "global fund",
    "open phil",
    "rockefeller",
    "gwwc",
    "80k",
    "longtermism",
    "€7k",
]


def test_no_foreign_anchor_survives_for_a_neutral_profile(monkeypatch):
    """Neither synthetic profile mentions impact-sector orgs, so none may appear
    in the rendered rubric — proving they are no longer baked into the template."""
    for profile in (ENGINEER, MEDIC):
        rendered = _render_company_prompt(monkeypatch, profile).lower()
        leaked = [tok for tok in _FORBIDDEN_ANCHORS if tok in rendered]
        assert not leaked, f"foreign anchor(s) leaked for {profile.name}: {leaked}"


# ---------------------------------------------------------------------------
# The dimension DEFINITIONS themselves carry no social-good/NGO worldview
# ---------------------------------------------------------------------------


def test_rendered_engineer_prompt_has_no_worldview_tokens(monkeypatch):
    """Reuses the same WORLDVIEW_TOKEN denylist as the static guard in
    test_no_hardcoded_data.py, checked here against the fully RENDERED prompt
    (post profile-substitution) for an engineer profile — proving a devtools
    company can score high on mission_authenticity without the template
    implying charity work is the reference point."""
    rendered = _render_company_prompt(monkeypatch, ENGINEER)
    hit = WORLDVIEW_TOKEN.search(rendered)
    assert hit is None, f"worldview token leaked into the rendered prompt: {hit.group(0)!r}"
