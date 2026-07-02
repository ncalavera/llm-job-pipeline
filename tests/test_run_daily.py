"""Tests for the deterministic /jobs-new driver (scripts/run_daily.py).

Covers the four things the driver MUST get right (acceptance criteria):
  1. stage order is fixed and cannot be violated,
  2. checkpoint + resume continues from where it stopped,
  3. the publish gate blocks a dirty run,
  4. the per-run scoring cap is a fuse; a full re-score is an explicit opt-in.

Plus a network-free integration slice over the first real stages (validate
profile -> preflight -> onboarding gate) on an isolated temp SQLite DB.

Everything is place- and person-agnostic: invented orgs, a throwaway DB, no
network and no real model.
"""

import importlib
import sys

import pytest


@pytest.fixture()
def rd(monkeypatch, tmp_path):
    """Fresh run_daily module with its state file redirected to a temp path."""
    sys.modules.pop("run_daily", None)
    import run_daily

    importlib.reload(run_daily)
    monkeypatch.setattr(run_daily, "STATE_PATH", tmp_path / "run_state.json")
    monkeypatch.setattr(run_daily, "FETCH_STATS_PATH", tmp_path / "fetch_stats.json")
    monkeypatch.setattr(run_daily, "LEARNING_PAYLOAD_PATH", tmp_path / "learning_review.json")
    return run_daily


# ---------------------------------------------------------------------------
# 1. Stage order
# ---------------------------------------------------------------------------


def test_stage_order_is_the_documented_sequence(rd):
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
        "publish",
    ]


def test_every_stage_has_exactly_one_handler(rd):
    assert set(rd.HANDLERS) == set(rd.STAGE_ORDER)
    assert len(rd.STAGE_ORDER) == len(set(rd.STAGE_ORDER)), "no duplicate stages"


def test_driver_runs_stages_strictly_in_order(rd):
    """With every stage stubbed to 'advance', the driver visits them in order."""
    seen = []

    def recorder(name):
        def handler(state, entry, opts):
            seen.append(name)
            return ("advance", "ok")

        return handler

    rd.HANDLERS = {name: recorder(name) for name in rd.STAGE_ORDER}
    state = rd._new_state(rd.Opts())
    code = rd.drive(state, rd.Opts())

    assert code == rd.EXIT_DONE
    assert seen == rd.STAGE_ORDER
    assert state["finished"] is True
    assert state["cursor"] == len(rd.STAGE_ORDER)


# ---------------------------------------------------------------------------
# 2. Checkpoint + resume
# ---------------------------------------------------------------------------


def test_gate_stops_the_run_and_resume_continues(rd):
    """A gate halts the driver mid-sequence; --resume picks up from the SAME
    stage and finishes — no earlier stage is ever re-run."""
    calls = []

    def advancer(name):
        def h(state, entry, opts):
            calls.append(name)
            return ("advance", "ok")

        return h

    # vacancy_scoring gates on the first pass, then advances on resume.
    def gating(state, entry, opts):
        calls.append("vacancy_scoring")
        if not entry.get("emitted"):
            return ("gate", {"action": "score", "instructions": "score them", "payload_path": None})
        return ("advance", "done")

    rd.HANDLERS = {name: advancer(name) for name in rd.STAGE_ORDER}
    rd.HANDLERS["vacancy_scoring"] = gating

    state = rd._new_state(rd.Opts())

    # First pass: stop at the gate.
    code = rd.drive(state, rd.Opts())
    assert code == rd.EXIT_GATE
    gate_idx = rd.STAGE_ORDER.index("vacancy_scoring")
    assert state["cursor"] == gate_idx
    assert rd._stage(state, "vacancy_scoring")["status"] == "blocked_gate"
    assert calls == rd.STAGE_ORDER[: gate_idx + 1]

    # State survives a reload from disk (checkpoint is durable).
    reloaded = rd._load_state()
    assert reloaded is not None
    assert reloaded["cursor"] == gate_idx

    # Resume: continue from the gate to the end; earlier stages are NOT re-run.
    calls.clear()
    code = rd.drive(reloaded, rd.Opts())
    assert code == rd.EXIT_DONE
    assert calls == rd.STAGE_ORDER[gate_idx:]  # gate re-checked, then the tail
    assert reloaded["finished"] is True


def test_stage_error_stops_with_error_code_and_is_resumable(rd):
    def advancer(name):
        def h(state, entry, opts):
            return ("advance", "ok")

        return h

    calls = {"fetch": 0}

    def flaky_fetch(state, entry, opts):
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            return ("error", "boom")
        return ("advance", "recovered")

    rd.HANDLERS = {name: advancer(name) for name in rd.STAGE_ORDER}
    rd.HANDLERS["fetch"] = flaky_fetch

    state = rd._new_state(rd.Opts())
    assert rd.drive(state, rd.Opts()) == rd.EXIT_ERROR
    fetch_idx = rd.STAGE_ORDER.index("fetch")
    assert state["cursor"] == fetch_idx  # did not advance past the failed stage
    assert rd._stage(state, "fetch")["status"] == "error"

    # Resume re-runs the failed stage (idempotent) and completes.
    assert rd.drive(state, rd.Opts()) == rd.EXIT_DONE


# ---------------------------------------------------------------------------
# 2b. Resume silently freezes options — flag the ones the CLI can't change
# ---------------------------------------------------------------------------


def test_resume_with_full_rescore_warns_it_is_ignored(rd, capsys):
    args = rd._parser().parse_args(["--resume", "--full-rescore"])
    rd._warn_ignored_resume_flags(args)
    out = capsys.readouterr().out
    assert "--full-rescore" in out
    assert "IGNORED" in out


def test_resume_with_boards_warns_it_is_ignored(rd, capsys):
    args = rd._parser().parse_args(["--resume", "--boards", "80k_hours"])
    rd._warn_ignored_resume_flags(args)
    out = capsys.readouterr().out
    assert "--boards" in out
    assert "IGNORED" in out


def test_resume_without_ignored_flags_stays_silent(rd, capsys):
    args = rd._parser().parse_args(["--resume"])
    rd._warn_ignored_resume_flags(args)
    assert capsys.readouterr().out == ""


def test_resume_keeps_checkpointed_opts_even_with_cli_overrides(rd):
    """The warning is cosmetic — resume must still replay the checkpoint's
    options, never the CLI's, so behaviour is unchanged."""
    state = rd._new_state(rd.Opts(job_boards="idealist", full_rescore=False))
    rd._save_state(state)

    rd.HANDLERS = {name: (lambda state, entry, opts: ("advance", "ok")) for name in rd.STAGE_ORDER}
    code = rd.main(["--resume", "--boards", "80k_hours", "--full-rescore"])
    assert code == rd.EXIT_DONE

    reloaded = rd._load_state()
    assert reloaded["options"]["job_boards"] == "idealist"
    assert reloaded["options"]["full_rescore"] is False


# ---------------------------------------------------------------------------
# 2c. Effective board set = persisted enabled UNION manual override
# ---------------------------------------------------------------------------


def _patch_persisted(rd, monkeypatch, ids):
    """Stub the DB read _resolve_boards does lazily, so these are DB-free unit
    tests of the union math (the DAL round-trip is covered in the parity suite)."""
    import database_supabase

    monkeypatch.setattr(database_supabase, "get_enabled_boards", lambda: list(ids))
    monkeypatch.delenv("JOB_BOARDS", raising=False)


def test_resolve_boards_persisted_alone_needs_no_flag(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, ["idealist", "80k_hours"])
    # Sorted DAL output is preserved; no --boards flag required to keep them on.
    assert rd._resolve_boards(None) == "idealist,80k_hours"


def test_resolve_boards_override_unions_on_top_without_duplicating(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, ["idealist"])
    # --boards adds a NEW board and must not duplicate one already persisted.
    assert rd._resolve_boards("reliefweb,idealist") == "idealist,reliefweb"


def test_resolve_boards_env_var_is_an_override_on_top(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, ["idealist"])
    monkeypatch.setenv("JOB_BOARDS", "reliefweb")  # the shell-env override path
    assert rd._resolve_boards(None) == "idealist,reliefweb"


def test_resolve_boards_all_short_circuits(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, ["idealist"])
    assert rd._resolve_boards("all") == "all"


def test_resolve_boards_nothing_selected_is_none(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, [])
    assert rd._resolve_boards(None) is None  # boards stay off, unchanged default


def test_resolve_boards_survives_unmigrated_schema(rd, monkeypatch):
    """A fresh clone has no board table until onboarding runs migrate.py, yet a
    --boards run must still work: the typed schema-missing signal falls back to
    the override."""
    import database_supabase

    def _unmigrated():
        raise database_supabase.BoardPersistenceUnavailable("run: python3 scripts/migrate.py")

    monkeypatch.setattr(database_supabase, "get_enabled_boards", _unmigrated)
    monkeypatch.delenv("JOB_BOARDS", raising=False)
    assert rd._resolve_boards("idealist") == "idealist"
    assert rd._resolve_boards(None) is None


def test_resolve_boards_propagates_real_db_failures(rd, monkeypatch):
    """Only the schema-missing case degrades to override-only. A genuine DB
    failure must propagate — silently running boards-off would be
    indistinguishable from the fresh-clone case (review finding on #39)."""
    import database_supabase

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(database_supabase, "get_enabled_boards", _boom)
    monkeypatch.delenv("JOB_BOARDS", raising=False)
    with pytest.raises(RuntimeError, match="connection refused"):
        rd._resolve_boards("idealist")


# ---------------------------------------------------------------------------
# 3. Publish gate
# ---------------------------------------------------------------------------


def _clean_state(rd):
    state = rd._new_state(rd.Opts())
    for s in state["stages"]:
        s["status"] = "done"
    return state


def test_publish_gate_allows_a_clean_run(rd):
    allowed, reasons = rd.check_publish_gate(_clean_state(rd), fetch_stats={"orgs": {}})
    assert allowed is True
    assert reasons == []


def test_publish_gate_blocks_on_a_stage_error(rd):
    state = _clean_state(rd)
    rd._stage(state, "enrich")["status"] = "error"
    allowed, reasons = rd.check_publish_gate(state, fetch_stats={"orgs": {}})
    assert allowed is False
    assert any("enrich" in r for r in reasons)


def test_publish_gate_blocks_on_mass_gone_archive(rd):
    """A truncated fetch that archived most of an org's live roles blocks publish."""
    stats = {"orgs": {"Globex": {"gone": 40, "live": 5}}}
    allowed, reasons = rd.check_publish_gate(_clean_state(rd), fetch_stats=stats)
    assert allowed is False
    assert any("Globex" in r for r in reasons)


def test_publish_gate_ignores_tiny_and_normal_orgs(rd):
    # gone=2 is below the min-count floor; gone=5/25 is a normal ~20% share.
    stats = {"orgs": {"Tiny": {"gone": 2, "live": 0}, "Normal": {"gone": 5, "live": 20}}}
    allowed, reasons = rd.check_publish_gate(_clean_state(rd), fetch_stats=stats)
    assert allowed is True, reasons


# ---------------------------------------------------------------------------
# 4. Scoring cap / full re-score opt-in
# ---------------------------------------------------------------------------


def test_default_run_lets_the_script_apply_the_cap(rd):
    limit, warn = rd.resolve_scoring_limit(full_rescore=False)
    assert limit is None  # no --limit -> score_vacancies.py applies max_per_run()
    assert warn is None


def test_full_rescore_lifts_the_cap_loudly(rd):
    limit, warn = rd.resolve_scoring_limit(full_rescore=True)
    assert isinstance(limit, int) and limit >= 1000
    assert warn and "FULL RE-SCORE" in warn


# ---------------------------------------------------------------------------
# 5. Integration slice — real early stages on an isolated temp SQLite DB
# ---------------------------------------------------------------------------


def _force_sqlite(monkeypatch, db_file):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE


def test_validate_profile_aborts_on_the_example(rd, monkeypatch):
    from prompts import EXAMPLE_PROFILE_PATH

    monkeypatch.setenv("USER_PROFILE_PATH", str(EXAMPLE_PROFILE_PATH))
    state = rd._new_state(rd.Opts())
    kind, msg = rd._h_validate_profile(state, rd._stage(state, "validate_profile"), rd.Opts())
    assert kind == "abort"
    assert "EXAMPLE" in msg


def test_validate_profile_accepts_a_personalised_profile(rd, monkeypatch, tmp_path):
    prof = tmp_path / "user_profile.md"
    prof.write_text(
        "## SUMMARY\nOperations lead, 8y, remote-first.\n\n## HARD_FILTERS\nban_regions:\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("USER_PROFILE_PATH", str(prof))
    state = rd._new_state(rd.Opts())
    kind, _ = rd._h_validate_profile(state, rd._stage(state, "validate_profile"), rd.Opts())
    assert kind == "advance"


def test_preflight_then_onboarding_gate_on_empty_db(rd, monkeypatch, tmp_path):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    state = rd._new_state(rd.Opts())

    kind, note = rd._h_preflight(state, rd._stage(state, "preflight"), rd.Opts())
    assert kind == "advance"
    assert state["first_run"] is True

    kind, payload = rd._h_onboarding(state, rd._stage(state, "onboarding"), rd.Opts())
    assert kind == "gate"
    assert payload["action"] == "onboard"


def test_onboarding_skipped_when_companies_exist(rd, monkeypatch, tmp_path):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    import database_supabase as db

    db.ensure_company("Acme Foundation", status="active")
    db.get_conn().commit()

    state = rd._new_state(rd.Opts())
    kind, _ = rd._h_preflight(state, rd._stage(state, "preflight"), rd.Opts())
    assert kind == "advance"
    assert state["first_run"] is False

    kind, note = rd._h_onboarding(state, rd._stage(state, "onboarding"), rd.Opts())
    assert kind == "skip"
    db.close_conn()


# ---------------------------------------------------------------------------
# 6. Learning-review gate — trigger, skip/rollover, no-content, guards.
#
# The gate decision is DB-free here: we inject a stub ``learning`` module so the
# driver's WIRING (when to stop, when to advance) is asserted in isolation from
# the learning mechanics (those are covered in test_learning.py).
# ---------------------------------------------------------------------------

import types  # noqa: E402


def _stub_learning(monkeypatch, *, table_ready=True, has_content=True, cursor_ts="2026-01-01"):
    review = {
        "ready": True,
        "has_content": has_content,
        "cursor_at": None,
        "verdicts_since_last_review": 4,
        "garbage_count": 2,
        "agreement": {
            "value": None,
            "previous": None,
            "measured": False,
            "note": "not yet measured — harness absent",
        },
        "proposals": {
            "filter_words": [
                {
                    "word": "casino",
                    "garbage_hits": 2,
                    "backtest": {
                        "clean": True,
                        "checked_liked": 1,
                        "checked_high": 1,
                        "collisions": [],
                    },
                }
            ],
            "filter_words_rejected": [],
            "factor_moves": [],
        },
        "revision": [],
        "applied_recent": [],
    }
    stub = types.ModuleType("learning")
    stub.table_ready = lambda: table_ready
    stub.build_review = lambda: review
    # cursor_ts=None models the cold-start case (never reviewed); mark_reviewed
    # is the seeding call the driver makes in that case — a no-op spy here.
    stub.cursor_ts = lambda: cursor_ts
    stub.mark_reviewed = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "learning", stub)
    return review


def _live_state(rd, first_run=False):
    state = rd._new_state(rd.Opts())
    state["first_run"] = first_run
    return state


def test_learning_review_gates_when_there_are_verdicts_to_review(rd, monkeypatch):
    _stub_learning(monkeypatch, has_content=True)
    state = _live_state(rd)
    entry = rd._stage(state, "learning_review")
    kind, info = rd._h_learning_review(state, entry, rd.Opts())
    assert kind == "gate"
    assert info["action"] == "learning_review"
    # The gate writes a machine-readable payload for the agent to read.
    import json

    payload = json.loads(rd.LEARNING_PAYLOAD_PATH.read_text())
    assert payload["proposals"]["filter_words"][0]["word"] == "casino"


def test_learning_review_advances_when_nothing_to_review(rd, monkeypatch):
    _stub_learning(monkeypatch, has_content=False)
    state = _live_state(rd)
    kind, note = rd._h_learning_review(state, rd._stage(state, "learning_review"), rd.Opts())
    assert kind == "advance"


def test_learning_review_skips_on_first_run(rd, monkeypatch):
    # A stub is present, but first-run short-circuits before it is consulted.
    _stub_learning(monkeypatch, has_content=True)
    state = _live_state(rd, first_run=True)
    kind, note = rd._h_learning_review(state, rd._stage(state, "learning_review"), rd.Opts())
    assert kind == "skip"
    assert "first run" in note


def test_learning_review_skips_when_ledger_table_missing(rd, monkeypatch):
    _stub_learning(monkeypatch, table_ready=False)
    state = _live_state(rd)
    kind, note = rd._h_learning_review(state, rd._stage(state, "learning_review"), rd.Opts())
    assert kind == "skip"
    assert "migrate" in note


def test_learning_review_seeds_cursor_silently_on_cold_start(rd, monkeypatch):
    """cursor_ts() is None — never reviewed — but this is NOT the empty-DB
    first_run case (first_run is False here): it's a fresh deploy of the
    learning cycle over an existing verdict back-catalog. The driver must not
    dump that whole history as a gate; it seeds the rollover cursor silently
    (via mark_reviewed) and skips, so the loop counts from adoption."""
    _stub_learning(monkeypatch, has_content=True, cursor_ts=None)
    seeded = []
    monkeypatch.setattr(sys.modules["learning"], "mark_reviewed", lambda **kw: seeded.append(kw))

    state = _live_state(rd)
    kind, note = rd._h_learning_review(state, rd._stage(state, "learning_review"), rd.Opts())

    assert kind == "skip"
    assert len(seeded) == 1  # the cursor was seeded exactly once
    assert seeded[0]["applied_count"] == 0


def test_learning_review_advances_on_resume_so_skip_rolls_over(rd, monkeypatch):
    """On resume the stage is already 'emitted': it advances WITHOUT touching the
    rollover cursor, so a skipped review's verdicts are still undiscussed next
    run. (The cursor only moves when the agent runs `learning.py complete`.)"""
    _stub_learning(monkeypatch, has_content=True)
    state = _live_state(rd)
    entry = rd._stage(state, "learning_review")
    entry["emitted"] = True
    kind, note = rd._h_learning_review(state, entry, rd.Opts())
    assert kind == "advance"
    assert "roll over" in note


def _minimal_review(agreement):
    return {
        "verdicts_since_last_review": 1,
        "garbage_count": 0,
        "agreement": agreement,
        "proposals": {"filter_words": [], "factor_moves": []},
        "revision": [],
    }


def test_learning_gate_text_shows_the_number_with_its_measured_at_date(rd):
    review = _minimal_review(
        {
            "value": 91.0,
            "measured": True,
            "measured_at": "2026-07-01T12:00:00+00:00",
            "previous": None,
        }
    )
    text = rd._learning_gate_text(review)
    assert "91%" in text
    assert "2026-07-01T12:00:00+00:00" in text


def test_learning_gate_text_points_at_jobs_eval_when_unmeasured(rd):
    review = _minimal_review({"value": None, "measured": False, "note": "not yet measured"})
    text = rd._learning_gate_text(review)
    assert "not yet measured — run /jobs-eval" in text


# ---------------------------------------------------------------------------
# 7. Two-pass vacancy scoring — cheap screen, strong escalation
# ---------------------------------------------------------------------------


def test_select_escalation_payloads_subset_and_floor(rd):
    payloads = [{"member_ids": ["a"]}, {"member_ids": ["b"]}, {"member_ids": ["c1", "c2"]}]
    scores = {"a": 40, "b": 39, "c1": 10, "c2": 72}
    out = rd.select_escalation_payloads(payloads, scores, 40)
    # a exactly at the floor -> in; b below -> out; c role escalates on its max.
    assert out == [payloads[0], payloads[2]]


def test_select_escalation_payloads_missing_score_never_escalates(rd):
    out = rd.select_escalation_payloads([{"member_ids": ["x"]}], {}, 40)
    assert out == []


def test_escalation_set_is_always_a_subset(rd):
    """The strong pass can never be larger than the cheap one (cost invariant)."""
    payloads = [{"member_ids": [str(i)]} for i in range(5)]
    scores = {str(i): i * 20 for i in range(5)}  # 0, 20, 40, 60, 80
    out = rd.select_escalation_payloads(payloads, scores, 40)
    assert len(out) <= len(payloads)
    assert all(o in payloads for o in out)


def _stub_two_pass(rd, monkeypatch, tmp_path, *, strong="sonnet", screen="haiku", floor=40):
    """Redirect the payload file and stub the run_status + scoring-settings I/O so
    the two-pass state machine can be stepped without a DB or a real model."""
    monkeypatch.setattr(rd, "VAC_PAYLOAD_PATH", tmp_path / "vac_payload.json")
    import run_status
    import scoring_settings

    monkeypatch.setattr(run_status, "begin", lambda *a, **k: None)
    monkeypatch.setattr(run_status, "finish", lambda *a, **k: None)
    monkeypatch.setattr(scoring_settings, "screen_model", lambda: screen)
    monkeypatch.setattr(scoring_settings, "scoring_model", lambda: strong)
    monkeypatch.setattr(scoring_settings, "escalation_threshold", lambda: floor)


def _payload(vid):
    return {
        "payload_kind": "vacancy",
        "id": vid,
        "member_ids": [vid],
        "org": "Org",
        "title": "t",
        "system_prompt": "s",
        "user_msg": "u",
    }


def test_two_pass_screen_then_escalate_flow(rd, monkeypatch, tmp_path):
    import json as _json
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path)
    payloads = [_payload("v1"), _payload("v2"), _payload("v3")]
    monkeypatch.setattr(
        rd,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, _json.dumps(payloads), ""),
    )
    reset_calls = []
    monkeypatch.setattr(rd, "_reset_escalation_scores", lambda ids: reset_calls.append(list(ids)))

    state = rd._new_state(rd.Opts())
    entry = rd._stage(state, "vacancy_scoring")

    # 1) First entry emits the SCREEN pass with the cheap model.
    kind, info = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "gate"
    assert "SCREEN pass" in info["instructions"]
    assert 'model "haiku"' in info["instructions"]
    assert "1 vacancy = 1 subagent" in info["instructions"]  # no-batching invariant
    assert entry["phase"] == "screen"
    entry["emitted"] = True  # drive() flips this after a gate

    # 2) Screen resume: one role still unscored -> screen gate again, cheap model.
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: {"v3"})
    kind, info = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "gate" and info["count"] == 1
    assert "SCREEN pass" in info["instructions"]

    # 3) Screen complete: v1=80 & v3=45 clear the floor, v2=30 stays cheap.
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: set())
    monkeypatch.setattr(rd, "_vacancy_scores", lambda ids: {"v1": 80, "v2": 30, "v3": 45})
    kind, info = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "gate"
    assert "ESCALATION pass" in info["instructions"]
    assert 'model "sonnet"' in info["instructions"]
    assert "1 vacancy = 1 subagent" in info["instructions"]  # invariant holds both passes
    assert entry["phase"] == "escalate"
    assert entry["screen_counts"] == {
        "screened": 3,
        "escalated": 2,
        "kept_cheap": 1,
        "threshold": 40,
    }
    assert reset_calls == [["v1", "v3"]]  # only the finalists are nulled
    assert info["count"] == 2

    # 4) Escalate resume: finalists scored -> advance with the report counts.
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: set())
    kind, info = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "advance"
    assert "screened 3" in info and "escalated 2" in info and "1 kept" in info


def test_two_pass_advances_when_nothing_escalates(rd, monkeypatch, tmp_path):
    import json as _json
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path)
    payloads = [_payload("v1"), _payload("v2")]
    monkeypatch.setattr(
        rd,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, _json.dumps(payloads), ""),
    )
    resets = []
    monkeypatch.setattr(rd, "_reset_escalation_scores", lambda ids: resets.append(list(ids)))

    state = rd._new_state(rd.Opts())
    entry = rd._stage(state, "vacancy_scoring")
    rd._h_vacancy_scoring(state, entry, rd.Opts())  # emit screen
    entry["emitted"] = True

    # Both roles score below the floor -> zero escalations, all kept cheap.
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: set())
    monkeypatch.setattr(rd, "_vacancy_scores", lambda ids: {"v1": 10, "v2": 25})
    kind, info = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "advance"
    assert "0 escalated" in info
    assert resets == []  # nothing nulled when nothing escalates


def test_full_rescore_stays_single_pass_oneshot(rd, monkeypatch, tmp_path):
    import json as _json
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path, strong="opus")
    payloads = [_payload("v1")]
    monkeypatch.setattr(
        rd,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, _json.dumps(payloads), ""),
    )
    opts = rd.Opts(full_rescore=True)
    state = rd._new_state(opts)
    entry = rd._stage(state, "vacancy_scoring")

    kind, info = rd._h_vacancy_scoring(state, entry, opts)
    assert kind == "gate"
    assert "Full re-score" in info["instructions"]
    assert 'model "opus"' in info["instructions"]  # strong model, cap lifted
    assert entry.get("oneshot") is True
    assert "phase" not in entry  # never enters the two-pass handshake

    entry["emitted"] = True
    kind, info = rd._h_vacancy_scoring(state, entry, opts)
    assert kind == "advance"
    assert "one-shot" in info
