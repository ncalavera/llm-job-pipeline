"""Trial T1 — "engineer from scratch".

Reproduces the first user-test failure where the maintainer's public-good job
boards shipped as product defaults and steered an engineer tester into the wrong
domain, with the scoring rubric carrying an effective-altruism worldview.

The persona is a synthetic software engineer (``profile_engineer.md``) driven
through the real loaders. The trial fails if the product regresses to that
failure: any board auto-enabled on a fresh clone, an impact/EA board proposed to
an engineer, or an EA worldview frame surviving in the rendered scoring prompt.

Board-recommendation MECHANICS are unit-tested in ``test_profile_targeting.py``
and the EA-free COMPANY prompt in ``test_company_scoring_profile_driven.py``;
this trial is the persona-level integration that ties the real fixture file to
the shipped board catalogue and the rendered prompt.
"""

from __future__ import annotations

import trial_harness as h

# Boards whose audience is purely effective-altruism / humanitarian / nonprofit,
# with no engineering overlap. An engineer must never be steered onto these.
IMPACT_ONLY_BOARDS = {
    "80k_hours",
    "reliefweb",
    "impactpool",
    "idealist",
    "consultants_for_impact",
}


def test_fresh_clone_auto_enables_no_board(monkeypatch, tmp_path):
    """A first-time clone (migrated, empty) fetches nothing until the user opts in."""
    dal = h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")

    assert dal.get_enabled_boards() == [], "no board may be enabled on a fresh clone"
    assert dal.get_company_fitness_map() == {}, "no companies tracked before onboarding"

    import config

    assert config.JOB_BOARDS == {}, "JOB_BOARDS unset must fetch zero boards"


def test_engineer_recommendations_skip_impact_only_boards(monkeypatch, tmp_path):
    """Board proposals come from the engineer's own profile, not an impact default."""
    h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")

    import profile_targeting as pt
    import prompts

    ids = [r["id"] for r in pt.recommend_boards(prompts._load_user_profile())]

    assert "linkedin" in ids, "the general board (queries from the profile) always fits"
    assert "arbeitnow" in ids, "an engineering board should match an engineer"
    leaked = IMPACT_ONLY_BOARDS.intersection(ids)
    assert not leaked, f"impact/EA boards proposed to an engineer: {sorted(leaked)}"


def test_engineer_scoring_prompt_has_no_ea_frame(monkeypatch, tmp_path):
    """The rendered vacancy prompt is built from the engineer's field, EA-free."""
    h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")

    import prompts
    from test_no_hardcoded_data import WORLDVIEW_TOKEN

    vacancy_prompt = prompts.VACANCY_SCORING_PROMPT
    company_prompt = prompts.COMPANY_SCORING_PROMPT

    # Profile-driven, not sector-fixed: the engineer's own domain rides in, the
    # unrelated persona's clinical vocabulary does not.
    assert "developer tools" in vacancy_prompt
    assert "developer tools" in company_prompt
    assert "Clinical Nurse Specialist" not in vacancy_prompt

    worldview_hits = WORLDVIEW_TOKEN.findall(vacancy_prompt)
    assert not worldview_hits, (
        f"EA/worldview frame leaked into the vacancy prompt: {worldview_hits}"
    )


def test_prompt_tracks_a_third_disjoint_field(monkeypatch, tmp_path):
    """A designer's prompt is built from design, not the engineer's or medic's field.

    The same render path must resolve THREE independent fields — engineer above,
    a product designer here, and (in a sibling guard) a nurse — so "scored against
    the candidate's own field" is not a two-way special case. This is where the
    designer persona genuinely earns its keep: board targeting cannot cleanly
    separate a designer from an engineer (they share the remote-software boards),
    but the rendered rubric does.
    """
    h.use_persona(monkeypatch, profile="profile_designer.md", db_path=tmp_path / "db")

    import prompts

    vacancy_prompt = prompts.VACANCY_SCORING_PROMPT

    assert "product design" in vacancy_prompt and "design systems" in vacancy_prompt
    assert "developer tools" not in vacancy_prompt  # not the engineer's field
    assert "distributed systems" not in vacancy_prompt
    assert "Clinical Nurse Specialist" not in vacancy_prompt  # not the medic's field
