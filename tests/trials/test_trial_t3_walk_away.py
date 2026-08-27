"""Trial T3 — "launch and walk away".

Reproduces the first user-test failure where operating the tool required
stage-order knowledge that lived only in the maintainer's head. The persona
starts the daily cycle and leaves: the driver must own the stage order, ask
nothing mid-pipeline when there is nothing to judge, and end on a summary that
explains every number in words.

The gate/resume state machine is unit-tested in ``test_run_daily.py``; this trial
adds the walk-away guarantees — no question when the judgment surface is empty,
and a self-explanatory end-of-run summary composed against a real seeded DB.
"""

from __future__ import annotations

import json

import trial_harness as h


def test_stage_order_and_handlers_are_owned_by_the_driver(monkeypatch, tmp_path):
    """The canonical order lives in code, and every stage has a handler."""
    h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")
    import run_daily as rd

    # One source of truth for "what happens when" — not a runbook, not memory.
    assert rd.STAGE_ORDER == [
        "validate_profile",
        "preflight",
        "onboarding",
        "learning_review",
        "fetch",
        "enrich",
        "filter",
        "company_scoring",
        "vacancy_scoring",
        "verdicts",
        "digest",
        "publish",
    ]
    # Every stage is executable; no orphan stage the operator must drive by hand.
    assert set(rd.HANDLERS) == set(rd.STAGE_ORDER)


def test_empty_judgment_surface_asks_nothing(monkeypatch, tmp_path):
    """With nothing to decide, the judgment stages advance/skip — no gate emitted."""
    dal = h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")
    # A tracked company but zero scored/liked roles: an established, quiet day.
    dal.ensure_company("Acme Cloud", status="active")
    dal.get_conn().commit()

    import run_daily as rd

    state = rd._new_state(rd.Opts())
    state["first_run"] = False  # companies present — not the onboarding case

    for stage in ("learning_review", "verdicts"):
        action, _note = rd.HANDLERS[stage](state, rd._stage(state, stage), rd.Opts())
        assert action != "gate", f"{stage} asked a question with nothing to decide"

    allowed, reasons = rd.check_publish_gate(state, fetch_stats={"orgs": {}})
    assert allowed and reasons == [], "a clean run must be allowed to publish silently"


def test_final_summary_explains_every_number(monkeypatch, tmp_path, capsys):
    """The one end screen is self-explanatory: numbers carried by words, none '?'."""
    dal = h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")
    h.seed_roles(dal, "Acme Cloud", [("Staff Engineer", "Own the platform. " * 20)])
    h.seed_roles(dal, "Globex Data", [("Data Engineer", "Build the pipeline. " * 20)])
    ids = list(dal.load_vacancies().keys())
    dal.update_vacancy_fields(ids[0], llm_score=78, status="unseen")
    dal.update_vacancy_fields(ids[1], status="liked")
    dal.get_conn().commit()

    import run_daily as rd

    monkeypatch.setattr(rd, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(rd, "FETCH_STATS_PATH", tmp_path / "fetch_stats.json")
    (tmp_path / "fetch_stats.json").write_text(
        json.dumps({"total_new": 6, "orgs": {}}), encoding="utf-8"
    )

    state = rd._new_state(rd.Opts())
    rd._stage(state, "publish")["note"] = "published to public/data.js"
    rd._print_summary(state, rd.Opts())

    out = capsys.readouterr().out
    # Every figure arrives with the word that explains it (English persona).
    assert "new vacancies saved this run" in out
    assert "active companies" in out
    assert "await your verdict" in out
    assert "publish:" in out
    assert "/jobs-review" in out  # what to do next, spelled out
    # No DB read degraded to the "?" placeholder — the numbers are real.
    assert "?" not in out
