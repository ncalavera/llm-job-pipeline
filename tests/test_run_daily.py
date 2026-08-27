"""Tests for the deterministic /jobs-new driver (scripts/run_daily.py).

Covers the four things the driver MUST get right (acceptance criteria):
  1. stage order is fixed and cannot be violated,
  2. checkpoint + resume continues from where it stopped,
  3. the publish gate flags a dirty run (warn-only — publish still refreshes),
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
    # BoardResolveError subclasses RuntimeError and preserves the message, so
    # this "a real failure propagates" contract still holds now that it is routed
    # through a typed abort.
    with pytest.raises(RuntimeError, match="connection refused"):
        rd._resolve_boards("idealist")


def test_new_run_aborts_cleanly_on_board_load_db_outage(rd, monkeypatch, capsys):
    """A durably-unreachable DB during board resolution must abort through the
    documented EXIT_ABORT path with a clear message, NOT crash main() with a raw
    traceback + undocumented exit 1 before the stage machine's preflight DB-outage
    stop can run."""
    import database_supabase

    def _outage():
        raise RuntimeError("could not connect to server: connection refused")

    monkeypatch.setattr(database_supabase, "get_enabled_boards", _outage)
    monkeypatch.delenv("JOB_BOARDS", raising=False)

    rc = rd.main(["--new", "--no-publish"])

    assert rc == rd.EXIT_ABORT
    err = capsys.readouterr().err.lower()
    assert "aborted" in err and "unreachable" in err


# ---------------------------------------------------------------------------
# 3. Publish gate
# ---------------------------------------------------------------------------


def _clean_state(rd):
    state = rd._new_state(rd.Opts())
    for s in state["stages"]:
        s["status"] = "done"
    return state


def test_publish_gate_reports_a_clean_run(rd):
    clean, reasons = rd.check_publish_gate(_clean_state(rd), fetch_stats={"orgs": {}})
    assert clean is True
    assert reasons == []


def test_publish_gate_flags_a_stage_error(rd):
    state = _clean_state(rd)
    rd._stage(state, "enrich")["status"] = "error"
    clean, reasons = rd.check_publish_gate(state, fetch_stats={"orgs": {}})
    assert clean is False
    assert any("enrich" in r for r in reasons)


def test_publish_gate_flags_mass_gone_archive(rd):
    """A truncated fetch that archived most of an org's live roles dirties the run."""
    stats = {"orgs": {"Globex": {"gone": 40, "live": 5}}}
    clean, reasons = rd.check_publish_gate(_clean_state(rd), fetch_stats=stats)
    assert clean is False
    assert any("Globex" in r for r in reasons)


def test_publish_gate_ignores_tiny_and_normal_orgs(rd):
    # gone=2 is below the min-count floor; gone=5/25 is a normal ~20% share.
    stats = {"orgs": {"Tiny": {"gone": 2, "live": 0}, "Normal": {"gone": 5, "live": 20}}}
    clean, reasons = rd.check_publish_gate(_clean_state(rd), fetch_stats=stats)
    assert clean is True, reasons


def test_publish_still_refreshes_on_a_dirty_run_and_warns(rd, monkeypatch):
    """Warn-only gate: a dirty run publishes anyway, and the note says so loudly."""
    calls = []
    monkeypatch.setattr(rd, "_run", lambda cmd, opts: (calls.append(cmd), 0)[1])
    state = _clean_state(rd)
    rd._stage(state, "enrich")["status"] = "error"

    kind, note = rd._h_publish(state, rd._stage(state, "publish"), rd.Opts())

    assert kind == "advance"  # stage lands as "done", not skipped
    assert calls, "the dashboard refresh must still run on a dirty run"
    assert "WITH WARNINGS" in note and "run not clean" in note and "enrich" in note


def test_publish_clean_run_refreshes_without_warning(rd, monkeypatch):
    calls = []
    monkeypatch.setattr(rd, "_run", lambda cmd, opts: (calls.append(cmd), 0)[1])

    kind, note = rd._h_publish(_clean_state(rd), {}, rd.Opts())

    assert kind == "advance"
    assert calls
    assert note == "dashboard refreshed (clean run)"


def test_no_publish_suppresses_the_refresh_even_on_a_dirty_run(rd, monkeypatch):
    monkeypatch.setattr(rd, "_run", lambda cmd, opts: pytest.fail("must not publish"))
    state = _clean_state(rd)
    rd._stage(state, "enrich")["status"] = "error"

    kind, note = rd._h_publish(state, rd._stage(state, "publish"), rd.Opts(no_publish=True))

    assert kind == "skip"
    assert "--no-publish" in note and "run not clean" in note


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
        "already_strong": 0,
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


def test_two_pass_resume_errors_on_corrupt_screen_payload(rd, monkeypatch, tmp_path):
    """A truncated/unreadable score payload on --resume must NOT be read as
    'scoring complete': when the DB still shows unscored targets but the payload
    can't supply the gate tasks, fail loudly (stage error) instead of silently
    skipping them into a publish."""
    _stub_two_pass(rd, monkeypatch, tmp_path)
    # DB ground truth: v1 is still unscored...
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: {"v1"})
    # ...but the payload file was truncated (a partial write / disk hiccup).
    rd.VAC_PAYLOAD_PATH.write_text('[{"member_ids": ["v1"', encoding="utf-8")

    state = rd._new_state(rd.Opts())
    entry = rd._stage(state, "vacancy_scoring")
    entry["emitted"] = True
    entry["phase"] = "screen"
    entry["target_ids"] = ["v1"]

    kind, msg = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "error"
    assert "payload" in msg.lower() and "unscored" in msg.lower()


def test_two_pass_resume_errors_on_corrupt_escalate_payload(rd, monkeypatch, tmp_path):
    """Same guard on the ESCALATE resume: a lost payload with finalists still
    unscored must error, not fall through to a false 'complete' advance."""
    _stub_two_pass(rd, monkeypatch, tmp_path)
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: {"v2"})
    rd.VAC_PAYLOAD_PATH.write_text("", encoding="utf-8")  # empty/unreadable

    state = rd._new_state(rd.Opts())
    entry = rd._stage(state, "vacancy_scoring")
    entry["emitted"] = True
    entry["phase"] = "escalate"
    entry["escalate_target_ids"] = ["v2"]
    entry["screen_counts"] = {"screened": 3, "escalated": 1, "kept_cheap": 2, "threshold": 40}

    kind, msg = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "error"
    assert "payload" in msg.lower()


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


# ---------------------------------------------------------------------------
# 4b. Same-model guard — no role re-scored by the same model
# ---------------------------------------------------------------------------


def test_select_escalation_payloads_skips_role_already_scored_by_escalate_model(rd):
    """A role that cleared the floor but whose current score already came
    from the strong model must NOT be sent back to it — same model, same
    result, paid twice."""
    payloads = [{"member_ids": ["a"]}, {"member_ids": ["b"]}]
    scores = {"a": 80, "b": 90}
    scored_by = {"a": "sonnet", "b": "haiku"}  # a already strong-scored, b is cheap
    out = rd.select_escalation_payloads(payloads, scores, 40, scored_by, "sonnet")
    assert out == [payloads[1]]  # only b escalates; a is skipped


def test_select_escalation_payloads_guard_is_opt_in(rd):
    """Without scored_by/escalate_model args, behaviour is unchanged (the
    guard is additive, not a breaking change to the existing call sites)."""
    payloads = [{"member_ids": ["a"]}]
    scores = {"a": 80}
    assert rd.select_escalation_payloads(payloads, scores, 40) == payloads


def test_two_pass_single_pass_when_screen_equals_scoring_model(rd, monkeypatch, tmp_path):
    """screen_model == scoring_model -> ONE pass, no escalate gate at all,
    and the gate text says so explicitly."""
    import json as _json
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path, strong="sonnet", screen="sonnet")
    payloads = [_payload("v1"), _payload("v2")]
    monkeypatch.setattr(
        rd,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, _json.dumps(payloads), ""),
    )

    state = rd._new_state(rd.Opts())
    entry = rd._stage(state, "vacancy_scoring")

    kind, info = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "gate"
    assert "SCORE pass" in info["instructions"]
    assert "ONE pass" in info["instructions"]
    assert "no escalate gate" in info["instructions"]
    assert '"sonnet"' in info["instructions"]
    assert entry["phase"] == "screen"
    assert entry["single_pass"] is True
    entry["emitted"] = True

    # Screen (single-pass) complete -> advance directly, no escalate gate ever.
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: set())
    kind, info = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "advance"
    assert "single-pass" in info
    assert "screen_model == scoring_model" in info
    assert '"sonnet"' in info
    # Durable run history (observability) reads screen_counts off this stage
    # entry — a single-pass run must record its scored count too.
    assert entry["screen_counts"] == {
        "screened": 2,
        "escalated": 0,
        "kept_cheap": 2,
        "threshold": None,
        "already_strong": 0,
    }


def test_two_pass_escalate_reports_skipped_same_model_roles(rd, monkeypatch, tmp_path):
    """When every finalist that cleared the floor was already scored by the
    strong model (a stale run-state edge case), the run advances straight
    through with zero escalated and says why."""
    import json as _json
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path, strong="sonnet", screen="haiku", floor=40)
    payloads = [_payload("v1")]
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

    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: set())
    monkeypatch.setattr(rd, "_vacancy_scores", lambda ids: {"v1": 80})
    monkeypatch.setattr(rd, "_vacancy_scored_by", lambda ids: {"v1": "sonnet"})

    kind, info = rd._h_vacancy_scoring(state, entry, rd.Opts())
    assert kind == "advance"
    assert "0 escalated" in info
    assert "already scored" in info
    assert '"sonnet"' in info
    assert resets == []  # nothing nulled — nothing was actually escalated


# ---------------------------------------------------------------------------
# 5. A corrupt checkpoint is never silently ignored
# ---------------------------------------------------------------------------


def test_corrupt_state_resume_does_not_lie_no_run(rd, monkeypatch, capsys):
    """--resume on a present-but-unreadable checkpoint must NOT report "No run
    to resume" (which would send the user off to start fresh and lose it)."""
    rd.STATE_PATH.write_text("{ corrupt json ", encoding="utf-8")
    monkeypatch.setattr(rd, "drive", lambda state, opts, observe=False: rd.EXIT_DONE)

    rc = rd.main(["--resume"])
    out = capsys.readouterr()
    assert rc == rd.EXIT_ABORT
    assert "No run to resume" not in (out.out + out.err)
    assert "corrupt" in (out.out + out.err).lower()
    # The file is left untouched for the user to inspect.
    assert rd.STATE_PATH.read_text(encoding="utf-8") == "{ corrupt json "


def test_corrupt_state_bare_run_does_not_clobber(rd, monkeypatch, capsys):
    """A BARE invocation on a corrupt checkpoint must abort, not silently start a
    fresh run and overwrite the (possibly recoverable) file."""
    rd.STATE_PATH.write_text("{ corrupt json ", encoding="utf-8")
    # If the guard fails and it proceeds, drive is a no-op so no real work runs.
    monkeypatch.setattr(rd, "drive", lambda state, opts, observe=False: rd.EXIT_DONE)

    rc = rd.main([])
    out = capsys.readouterr()
    assert rc == rd.EXIT_ABORT
    assert rd.STATE_PATH.read_text(encoding="utf-8") == "{ corrupt json "  # not clobbered
    assert "corrupt" in (out.out + out.err).lower()


def test_corrupt_state_new_run_is_allowed_to_discard(rd, monkeypatch, capsys):
    """--new is the documented escape hatch: it must NOT abort on a corrupt
    checkpoint — it proceeds past the guard to build a fresh run. drive is a
    no-op so no real pipeline runs."""
    rd.STATE_PATH.write_text("{ corrupt json ", encoding="utf-8")
    captured = {}

    def fake_drive(state, opts, observe=False):
        captured["state"] = state  # the corrupt guard let us build a fresh state
        return rd.EXIT_DONE

    monkeypatch.setattr(rd, "drive", fake_drive)
    monkeypatch.setattr(rd, "_resolve_boards", lambda b, skip=None: None)
    monkeypatch.setattr(rd, "_print_run_banner", lambda opts: None)
    monkeypatch.setattr(rd, "_print_summary", lambda state, opts: None)

    rc = rd.main(["--new"])
    out = capsys.readouterr()
    assert rc != rd.EXIT_ABORT
    assert "corrupt" not in (out.out + out.err).lower()
    assert captured["state"]["finished"] is False  # a brand-new, fresh run state


def test_missing_state_still_reads_as_no_run(rd):
    """A genuinely ABSENT checkpoint (no file) is not corruption — --resume still
    reports "No run to resume" as before."""
    assert not rd.STATE_PATH.exists()
    assert rd._state_file_corrupt() is False


# ---------------------------------------------------------------------------
# 8. Registry-load preflight fails TOWARD the abort on an unexpected error
# ---------------------------------------------------------------------------


def test_registry_load_check_reports_ok_when_underlying_says_ok(rd, monkeypatch):
    """Happy path is unchanged: the wrapper passes through the real answer."""
    import company_registry

    monkeypatch.setattr(company_registry, "registry_load_failed", lambda: False)
    assert rd._registry_load_failed() is False
    monkeypatch.setattr(company_registry, "registry_load_failed", lambda: True)
    assert rd._registry_load_failed() is True


def test_registry_load_check_fails_toward_abort_on_unexpected_error(rd, monkeypatch, capsys):
    """An unexpected failure in the check must NOT be read as "registry OK"
    (which would let destructive first-run onboarding run during a DB outage).
    It counts as a load failure, with the cause surfaced on stderr."""
    import company_registry

    def _boom():
        raise RuntimeError("connection reset mid-check")

    monkeypatch.setattr(company_registry, "registry_load_failed", _boom)

    assert rd._registry_load_failed() is True
    err = capsys.readouterr().err.lower()
    assert "registry-load check failed" in err
    assert "connection reset mid-check" in err


def test_preflight_aborts_when_registry_check_errors(rd, monkeypatch):
    """End to end: an erroring registry check drives preflight to abort, not to
    advance into onboarding."""
    monkeypatch.setattr(rd, "_registry_load_failed", lambda: True)
    state = rd._new_state(rd.Opts())
    kind, msg = rd._h_preflight(state, rd._stage(state, "preflight"), rd.Opts())
    assert kind == "abort"
    assert "unreachable" in msg.lower()


# ---------------------------------------------------------------------------
# 9. A bare invocation that auto-resumes must be LOUD, and must not silently
#    swallow flags that only a fresh run could honour.
# ---------------------------------------------------------------------------


def _unfinished_run_on_disk(rd):
    state = rd._new_state(rd.Opts(job_boards="idealist"))
    rd._save_state(state)
    return state


def test_resume_banner_shows_run_id_stage_and_age(rd, capsys):
    state = rd._new_state(rd.Opts())
    state["cursor"] = rd.STAGE_ORDER.index("filter")
    rd._print_resume_banner(state)
    out = capsys.readouterr().out
    assert "RESUMING" in out
    assert state["run_id"] in out
    assert "filter" in out
    assert "ago" in out  # the age line rendered


def test_bare_run_auto_resumes_with_a_loud_banner(rd, monkeypatch, capsys):
    """A bare invocation over an unfinished run still resumes (by design) — but
    now announces it unmissably instead of a one-line whisper."""
    state = _unfinished_run_on_disk(rd)
    seen = {}

    def fake_drive(s, o, observe=False):
        seen["state"] = s
        return rd.EXIT_DONE

    monkeypatch.setattr(rd, "drive", fake_drive)
    monkeypatch.setattr(rd, "_print_summary", lambda s, opts: None)

    rc = rd.main([])
    out = capsys.readouterr().out
    assert rc == rd.EXIT_DONE
    assert seen["state"]["run_id"] == state["run_id"]  # the SAME run, resumed
    assert "RESUMING" in out
    assert state["run_id"] in out


def test_bare_run_with_full_rescore_aborts_instead_of_silently_ignoring(rd, monkeypatch, capsys):
    """Passing --full-rescore to a bare invocation that would auto-resume is a
    contradiction (resume can't honour it). Refuse loudly instead of running a
    stale resume with the flag silently dropped."""
    state = _unfinished_run_on_disk(rd)
    ran = {"drive": False}
    monkeypatch.setattr(
        rd, "drive", lambda s, o, observe=False: ran.__setitem__("drive", True) or rd.EXIT_DONE
    )

    rc = rd.main(["--full-rescore"])
    out = capsys.readouterr()
    assert rc == rd.EXIT_ABORT
    assert ran["drive"] is False  # no work started
    assert "--full-rescore" in (out.out + out.err)
    # The checkpoint is left exactly as it was — neither resumed nor clobbered.
    assert rd._load_state()["run_id"] == state["run_id"]


def test_bare_run_with_boards_aborts_instead_of_silently_ignoring(rd, monkeypatch, capsys):
    _unfinished_run_on_disk(rd)
    monkeypatch.setattr(rd, "drive", lambda s, o, observe=False: rd.EXIT_DONE)

    rc = rd.main(["--boards", "80k_hours"])
    out = capsys.readouterr()
    assert rc == rd.EXIT_ABORT
    assert "--boards" in (out.out + out.err)


def test_bare_run_no_publish_still_auto_resumes(rd, monkeypatch, capsys):
    """--no-publish IS honoured on resume, so it must NOT trip the abort — the
    run resumes and applies it."""
    _unfinished_run_on_disk(rd)
    captured = {}
    monkeypatch.setattr(
        rd, "drive", lambda s, o, observe=False: captured.__setitem__("opts", o) or rd.EXIT_DONE
    )
    monkeypatch.setattr(rd, "_print_summary", lambda s, opts: None)

    rc = rd.main(["--no-publish"])
    assert rc == rd.EXIT_DONE
    assert captured["opts"].no_publish is True


# ---------------------------------------------------------------------------
# Per-run scoping: --tier (fetch scope) + --skip-boards (board subtraction).
# One-run knobs only: the persisted board.enabled set and stored company tiers
# are never modified.
# ---------------------------------------------------------------------------


def test_resolve_boards_skip_subtracts_from_the_effective_set(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, ["idealist", "80k_hours", "linkedin"])
    # --skip-boards drops one persisted board for THIS run only.
    assert rd._resolve_boards(None, "linkedin") == "idealist,80k_hours"


def test_resolve_boards_skip_applies_after_the_union(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, ["linkedin"])
    # --boards adds idealist, --skip-boards removes the persisted linkedin.
    assert rd._resolve_boards("idealist", "linkedin") == "idealist"


def test_resolve_boards_skip_of_everything_is_none(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, ["linkedin"])
    # Subtracting the only board leaves boards-off (None), never an empty string.
    assert rd._resolve_boards(None, "linkedin") is None


def test_resolve_boards_all_with_skip_expands_and_subtracts(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, [])
    import config

    first = next(iter(config._ALL_JOB_BOARDS))
    result = rd._resolve_boards("all", first)
    # "all" is materialised to the explicit known-board list so the skip bites.
    assert result is not None
    ids = result.split(",")
    assert first not in ids
    assert len(ids) == len(config._ALL_JOB_BOARDS) - 1


def test_resolve_boards_all_without_skip_stays_the_wildcard(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, ["idealist"])
    # No subtraction -> "all" stays the compact wildcard (byte-identical default).
    assert rd._resolve_boards("all") == "all"


def test_resolve_boards_no_skip_is_unchanged(rd, monkeypatch):
    _patch_persisted(rd, monkeypatch, ["idealist", "80k_hours"])
    # The added skip_boards parameter defaults to no subtraction: identical output.
    assert rd._resolve_boards("reliefweb") == "idealist,80k_hours,reliefweb"


def test_resolve_boards_is_read_only_never_persists(rd, monkeypatch):
    """Resolving a scoped board set must NEVER write the persisted enabled set —
    the core acceptance that scoping affects one run only."""
    _patch_persisted(rd, monkeypatch, ["idealist", "linkedin"])
    import database_supabase

    def _boom(*a, **k):
        raise AssertionError("set_board_enabled must not be called during resolution")

    monkeypatch.setattr(database_supabase, "set_board_enabled", _boom)
    assert rd._resolve_boards("80k_hours", "linkedin") == "idealist,80k_hours"


def test_skip_boards_unknown_token_warns_loudly(rd, monkeypatch, capsys):
    """A typo'd skip must never silently no-op — the board would still run and
    the user would never learn. Unknown ids warn (not abort), consistent with
    how config warns on an unknown --boards id downstream."""
    _patch_persisted(rd, monkeypatch, ["linkedin"])
    result = rd._resolve_boards(None, "linkedln")  # typo: missing 'i'
    err = capsys.readouterr().err
    assert "WARNING" in err and "--skip-boards" in err and "linkedln" in err
    assert result == "linkedin"  # the real board still runs; only the typo warned


def test_skip_boards_known_token_does_not_warn(rd, monkeypatch, capsys):
    _patch_persisted(rd, monkeypatch, ["linkedin", "idealist"])
    assert rd._resolve_boards(None, "linkedin") == "idealist"
    assert "WARNING" not in capsys.readouterr().err


def test_skip_boards_all_turns_boards_off_for_the_run(rd, monkeypatch, capsys):
    """--skip-boards all mirrors --boards all: every board is dropped for THIS
    run. The resolved value is None — exactly the boards-off state in which
    fetch still pulls all active companies (see _h_fetch: --no-boards is only
    ever added on first_run; a None board set simply leaves JOB_BOARDS unset)."""
    _patch_persisted(rd, monkeypatch, ["idealist", "linkedin"])
    assert rd._resolve_boards(None, "all") is None
    assert rd._resolve_boards("80k_hours", "all") is None  # skip-all beats the union
    assert rd._resolve_boards("all", "all") is None
    assert "WARNING" not in capsys.readouterr().err  # 'all' is not an unknown id


def test_skip_boards_all_companies_still_fetch(rd, monkeypatch):
    """Boards-off scoping must not suppress the company fetch: the fetch command
    carries no --no-boards / --boards-only flag on a normal (non-first) run —
    boards stay off purely because JOB_BOARDS is unset in the child env."""
    monkeypatch.delenv("JOB_BOARDS", raising=False)
    seen = _capture_fetch_cmd(rd, monkeypatch)
    opts = rd.Opts(job_boards=None)  # what --skip-boards all resolves to
    state = rd._new_state(opts)
    state["first_run"] = False
    kind, _ = rd._h_fetch(state, rd._stage(state, "fetch"), opts)
    assert kind == "advance"
    assert "--no-boards" not in seen["cmd"] and "--boards-only" not in seen["cmd"]
    assert "JOB_BOARDS" not in rd._child_env(opts)


def _capture_fetch_cmd(rd, monkeypatch):
    """Run the fetch handler with the subprocess stubbed, returning its argv."""
    seen = {}

    def _fake_run(cmd, opts):
        seen["cmd"] = cmd
        return 0

    monkeypatch.setattr(rd, "_run", _fake_run)
    monkeypatch.setattr(rd, "_read_fetch_stats", lambda: {"total_new": 3})
    return seen


def test_fetch_command_includes_tier_when_scoped(rd, monkeypatch):
    seen = _capture_fetch_cmd(rd, monkeypatch)
    state = rd._new_state(rd.Opts(tier="S"))
    kind, _ = rd._h_fetch(state, rd._stage(state, "fetch"), rd.Opts(tier="S"))
    assert kind == "advance"
    cmd = seen["cmd"]
    assert "--tier" in cmd and cmd[cmd.index("--tier") + 1] == "S"


def test_fetch_command_has_no_tier_by_default(rd, monkeypatch):
    seen = _capture_fetch_cmd(rd, monkeypatch)
    state = rd._new_state(rd.Opts())
    rd._h_fetch(state, rd._stage(state, "fetch"), rd.Opts())
    assert "--tier" not in seen["cmd"]


def test_new_state_persists_tier_and_resume_restores_it(rd):
    state = rd._new_state(rd.Opts(tier="A", job_boards="idealist"))
    assert state["options"]["tier"] == "A"
    restored = rd._opts_from_state(state)
    assert restored.tier == "A"
    assert restored.job_boards == "idealist"


def test_fetch_scope_phrase_reflects_tier_and_board_count(rd):
    phrase = rd._fetch_scope_phrase(rd.Opts(tier="S", job_boards="idealist,80k_hours"))
    assert "S-tier" in phrase and "2 board(s)" in phrase
    default = rd._fetch_scope_phrase(rd.Opts())
    assert "tier" not in default and "boards off" in default


def test_tier_flag_parses_and_uppercases(rd):
    args = rd._parser().parse_args(["--new", "--tier", "s"])
    assert args.tier == "S"


def test_tier_flag_rejects_an_unknown_tier(rd):
    with pytest.raises(SystemExit):
        rd._parser().parse_args(["--new", "--tier", "Z"])


def test_tier_and_skip_boards_are_ignored_on_resume(rd):
    args = rd._parser().parse_args(["--resume", "--tier", "S", "--skip-boards", "linkedin"])
    ignored = rd._ignored_resume_flags(args)
    assert "--tier" in ignored and "--skip-boards" in ignored


# ---------------------------------------------------------------------------
# Loud failures + report card (R5 / R7): warnings, publish gate, stage verdicts.
# ---------------------------------------------------------------------------


def test_new_state_seeds_empty_warnings(rd):
    assert rd._new_state(rd.Opts())["warnings"] == []


def test_add_warning_records_structured_entry(rd):
    state = rd._new_state(rd.Opts())
    rd._add_warning(state, "company_scoring", "evidence failed")
    rd._add_warning(state, "company_scoring", "screen crashed", blocking=True)
    assert [w["message"] for w in state["warnings"]] == ["evidence failed", "screen crashed"]
    assert state["warnings"][0]["blocking"] is False
    assert state["warnings"][1]["blocking"] is True


def test_add_warning_tolerates_missing_state(rd):
    rd._add_warning(None, "company_scoring", "no state here")  # must not raise


def test_prefilter_records_warning_on_nonzero_exit(rd, monkeypatch):
    monkeypatch.setattr(rd, "_run", lambda cmd, opts: 1)
    state = rd._new_state(rd.Opts())
    rd._prefilter_junk_companies(rd.Opts(), state)
    assert any("junk pre-filter" in w["message"] for w in state["warnings"])
    assert not state["warnings"][0]["blocking"]  # degraded, not publish-blocking


def test_publish_gate_flags_a_blocking_warning(rd):
    state = _clean_state(rd)
    rd._add_warning(
        state, "company_scoring", "screen failed — paid enrichment withheld", blocking=True
    )
    clean, reasons = rd.check_publish_gate(state, fetch_stats={"orgs": {}})
    assert clean is False
    assert any("screen failed" in r for r in reasons)


def test_publish_gate_ignores_nonblocking_warning(rd):
    state = _clean_state(rd)
    rd._add_warning(state, "company_scoring", "evidence collection degraded")
    clean, reasons = rd.check_publish_gate(state, fetch_stats={"orgs": {}})
    assert clean is True and reasons == []


def test_stage_verdict_maps_each_status(rd):
    warned = {"name": "company_scoring", "status": "done"}
    clean = {"name": "fetch", "status": "done"}
    warnings = [{"stage": "company_scoring", "message": "x"}]
    assert rd._stage_verdict(clean, warnings) == "OK"
    assert rd._stage_verdict(warned, warnings) == "OK-BUT"
    assert rd._stage_verdict({"name": "filter", "status": "error"}, []) == "FAILED"
    assert rd._stage_verdict({"name": "enrich", "status": "skipped"}, []) == "SKIPPED"


def test_stage_verdict_blocking_warning_reads_failed(rd):
    # The valve skips paid work on purpose, but a blocking warning is a real
    # failure — it reads FAILED on the card and dirties publish, not a benign SKIP.
    stage = {"name": "company_scoring", "status": "skipped"}
    warnings = [{"stage": "company_scoring", "message": "screen failed", "blocking": True}]
    assert rd._stage_verdict(stage, warnings) == "FAILED"


def test_report_card_renders_four_distinct_verdicts(rd, capsys):
    state = rd._new_state(rd.Opts())
    rd._stage(state, "fetch")["status"] = "done"
    rd._stage(state, "fetch")["note"] = "12 new vacancies"
    rd._stage(state, "company_scoring")["status"] = "done"
    rd._stage(state, "company_scoring")["note"] = "scored 3"
    rd._stage(state, "enrich")["status"] = "skipped"
    rd._stage(state, "enrich")["note"] = "FIRECRAWL_API_KEY unset"
    rd._stage(state, "filter")["status"] = "error"
    rd._stage(state, "filter")["note"] = "exited 1"
    rd._add_warning(state, "company_scoring", "evidence degraded")

    rd._print_stage_board(state, verdict=True)
    out = capsys.readouterr().out
    assert "OK" in out and "OK-BUT" in out and "FAILED" in out and "SKIPPED" in out
    assert "FIRECRAWL_API_KEY unset" in out  # reason travels in the note column


def test_fetch_source_counters_labels_career_sites_and_boards(rd):
    stats = {
        "career_sites": {"total": 8, "yielded": 4},
        "boards": {"total": 7, "fetched": 0, "ttl_skipped": 7, "yielded": 0},
    }
    counters = rd._fetch_source_counters(stats)
    assert counters == "career sites 4/8 · boards 0/7 (TTL)"


def test_fetch_source_counters_no_ttl_tag_when_boards_ran(rd):
    stats = {
        "career_sites": {"total": 3, "yielded": 3},
        "boards": {"total": 2, "fetched": 2, "ttl_skipped": 0, "yielded": 1},
    }
    assert rd._fetch_source_counters(stats) == "career sites 3/3 · boards 1/2"


# ---------------------------------------------------------------------------
# 10. Unattended mode (KTD2) — every gate has an answer; a night run never
#     waits for a human and never stalls twice at a phase with no progress.
# ---------------------------------------------------------------------------


def test_unattended_flag_is_frozen_in_the_checkpoint(rd):
    args = rd._parser().parse_args(["--new", "--unattended"])
    assert args.unattended is True
    state = rd._new_state(rd.Opts(unattended=True))
    assert state["options"]["unattended"] is True
    assert rd._opts_from_state(state).unattended is True
    # Default off, and an old checkpoint without the key reads as off.
    assert rd._new_state(rd.Opts())["options"]["unattended"] is False
    assert rd._opts_from_state({"options": {}}).unattended is False


def test_unattended_is_flagged_as_ignored_on_resume(rd):
    args = rd._parser().parse_args(["--resume", "--unattended"])
    assert "--unattended" in rd._ignored_resume_flags(args)


def test_resume_with_unattended_warns_and_keeps_checkpoint_value(rd, capsys):
    """--resume --unattended after a --new WITHOUT the flag: the repeated flag is
    warned as ignored and the checkpoint's value (off) wins."""
    state = rd._new_state(rd.Opts())  # created without --unattended
    rd._save_state(state)
    rd.HANDLERS = {name: (lambda state, entry, opts: ("advance", "ok")) for name in rd.STAGE_ORDER}

    code = rd.main(["--resume", "--unattended"])
    assert code == rd.EXIT_DONE
    out = capsys.readouterr().out
    assert "--unattended" in out and "IGNORED" in out
    assert rd._load_state()["options"]["unattended"] is False


def test_unattended_onboarding_aborts_instead_of_gating(rd, monkeypatch):
    """An empty company table at night means the wrong database — abort, never
    wait for a human to onboard (R15)."""
    monkeypatch.setattr(rd, "_company_count", lambda: 0)
    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    state["first_run"] = True
    kind, msg = rd._h_onboarding(state, rd._stage(state, "onboarding"), opts)
    assert kind == "abort"
    assert "wrong database" in msg.lower()


def test_unattended_onboarding_abort_is_exit_20_with_reason_in_state(rd, monkeypatch):
    monkeypatch.setattr(rd, "_company_count", lambda: 0)

    def preflight(state, entry, opts):
        state["first_run"] = True
        return ("advance", "empty table")

    rd.HANDLERS = dict(rd.HANDLERS)
    rd.HANDLERS["validate_profile"] = lambda s, e, o: ("advance", "ok")
    rd.HANDLERS["preflight"] = preflight

    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    assert rd.drive(state, opts) == rd.EXIT_ABORT
    assert rd.EXIT_ABORT == 20
    entry = rd._stage(state, "onboarding")
    assert entry["status"] == "aborted"
    assert "wrong database" in entry["note"].lower()


def test_unattended_drive_aborts_on_a_gate_with_no_answer(rd):
    """R15: a gate that reaches drive() with no unattended answer aborts with
    the reason in state instead of waiting forever."""

    def advancer(s, e, o):
        return ("advance", "ok")

    def rogue_gate(s, e, o):
        return ("gate", {"action": "verdicts", "instructions": "x", "payload_path": None})

    rd.HANDLERS = {name: advancer for name in rd.STAGE_ORDER}
    rd.HANDLERS["verdicts"] = rogue_gate
    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    assert rd.drive(state, opts) == rd.EXIT_ABORT
    entry = rd._stage(state, "verdicts")
    assert entry["status"] == "aborted"
    assert "no unattended answer" in entry["note"]


def test_attended_drive_still_gates_on_the_same_payload(rd):
    """Unattended off (default): the identical gate still stops the run."""

    def advancer(s, e, o):
        return ("advance", "ok")

    def rogue_gate(s, e, o):
        return ("gate", {"action": "verdicts", "instructions": "x", "payload_path": None})

    rd.HANDLERS = {name: advancer for name in rd.STAGE_ORDER}
    rd.HANDLERS["verdicts"] = rogue_gate
    state = rd._new_state(rd.Opts())
    assert rd.drive(state, rd.Opts()) == rd.EXIT_GATE


def test_unattended_learning_review_rolls_over_without_applying(rd, monkeypatch):
    """AE8: pending proposals advance unapplied; the rollover cursor is never
    touched, so the same verdicts are still pending next run."""
    _stub_learning(monkeypatch, has_content=True)
    seeded = []
    monkeypatch.setattr(sys.modules["learning"], "mark_reviewed", lambda **kw: seeded.append(kw))

    opts = rd.Opts(unattended=True)
    state = _live_state(rd)
    entry = rd._stage(state, "learning_review")
    kind, note = rd._h_learning_review(state, entry, opts)

    assert kind == "advance"
    assert entry["rolled_over"] == 4  # recorded for the digest stage
    assert seeded == []  # cursor untouched -> proposals roll over, nothing applied
    assert "roll" in note.lower()


def test_unattended_verdicts_advance_and_record_the_count(rd, monkeypatch):
    monkeypatch.setattr(rd, "_scored_unseen", lambda: 5)
    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    entry = rd._stage(state, "verdicts")
    kind, note = rd._h_verdicts(state, entry, opts)
    assert kind == "advance"
    assert entry["pending_verdicts"] == 5
    assert "5" in note


def test_attended_verdicts_still_gate(rd, monkeypatch):
    monkeypatch.setattr(rd, "_scored_unseen", lambda: 5)
    state = rd._new_state(rd.Opts())
    kind, info = rd._h_verdicts(state, rd._stage(state, "verdicts"), rd.Opts())
    assert kind == "gate"
    assert info["action"] == "verdicts" and info["count"] == 5


def test_unattended_scorer_command_carries_the_unattended_flag(rd, monkeypatch, tmp_path):
    """The scorer loads oldest-unscored-first in unattended mode (U1), so
    carried-over roles never sink under the next night's arrivals."""
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path)
    seen = {}

    def cap(cmd, opts):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    monkeypatch.setattr(rd, "_run_capture", cap)

    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    rd._h_vacancy_scoring(state, rd._stage(state, "vacancy_scoring"), opts)
    assert "--unattended" in seen["cmd"]

    state = rd._new_state(rd.Opts())
    rd._h_vacancy_scoring(state, rd._stage(state, "vacancy_scoring"), rd.Opts())
    assert "--unattended" not in seen["cmd"]


def test_unattended_vacancy_scoring_carries_over_after_no_progress(rd, monkeypatch, tmp_path):
    """AE1: 30 roles, the headless session dies after 12 — a resume with
    progress re-emits the gate; a resume with NO progress advances with
    carried_over = 18 and the run reaches publish."""
    import json as _json
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path, strong="opus", screen="opus")  # single pass
    payloads = [_payload(f"v{i}") for i in range(30)]
    monkeypatch.setattr(
        rd,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, _json.dumps(payloads), ""),
    )
    monkeypatch.setattr(rd, "_scored_unseen", lambda: 12)
    monkeypatch.setattr(rd, "_run", lambda cmd, opts: 0)  # publish refresh

    def advancer(s, e, o):
        return ("advance", "ok")

    handlers = {name: advancer for name in rd.STAGE_ORDER}
    handlers["vacancy_scoring"] = rd._h_vacancy_scoring
    handlers["verdicts"] = rd._h_verdicts
    handlers["publish"] = rd._h_publish
    rd.HANDLERS = handlers

    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)

    # Night pass 1: the scoring gate emits for the headless session.
    assert rd.drive(state, opts) == rd.EXIT_GATE
    entry = rd._stage(state, "vacancy_scoring")
    assert entry["single_pass"] is True  # Opus both models -> one gate only

    # 12 saved -> progress -> the gate emits again for another attempt.
    remaining18 = {f"v{i}" for i in range(12, 30)}
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: set(remaining18))
    assert rd.drive(state, opts) == rd.EXIT_GATE

    # Nothing new saved -> no progress -> carry over and finish the run.
    assert rd.drive(state, opts) == rd.EXIT_DONE
    assert entry["carried_over"] == 18
    assert entry["status"] == "done"
    assert rd._stage(state, "verdicts")["pending_verdicts"] == 12
    assert rd._stage(state, "publish")["status"] == "done"
    assert state["finished"] is True


def test_unattended_two_pass_escalate_gate_still_emits_once(rd, monkeypatch, tmp_path):
    """Two-model profile: keying on phase + progress (not entry['emitted'])
    keeps the escalate phase alive — after the cheap screen saves every score,
    the resume emits the escalate gate once before any advance."""
    import json as _json
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path, strong="sonnet", screen="haiku", floor=40)
    payloads = [_payload("v1"), _payload("v2")]
    monkeypatch.setattr(
        rd,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, _json.dumps(payloads), ""),
    )
    monkeypatch.setattr(rd, "_reset_escalation_scores", lambda ids: None)
    monkeypatch.setattr(rd, "_vacancy_scored_by", lambda ids: {})

    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    entry = rd._stage(state, "vacancy_scoring")

    kind, info = rd._h_vacancy_scoring(state, entry, opts)
    assert kind == "gate"
    assert "SCREEN pass" in info["instructions"]
    entry["emitted"] = True

    # Cheap scores all saved -> the escalate gate must still emit (once).
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: set())
    monkeypatch.setattr(rd, "_vacancy_scores", lambda ids: {"v1": 80, "v2": 30})
    kind, info = rd._h_vacancy_scoring(state, entry, opts)
    assert kind == "gate"
    assert "ESCALATION pass" in info["instructions"]
    assert entry["phase"] == "escalate"

    # Finalist scored -> advance with the two-pass report.
    kind, info = rd._h_vacancy_scoring(state, entry, opts)
    assert kind == "advance"
    assert "escalated 1" in info


def test_unattended_escalate_carries_over_when_stuck(rd, monkeypatch, tmp_path):
    """An escalate resume with no progress carries the finalists over instead of
    stalling the night."""
    import json as _json
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path, strong="sonnet", screen="haiku", floor=40)
    payloads = [_payload("v1"), _payload("v2")]
    monkeypatch.setattr(
        rd,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, _json.dumps(payloads), ""),
    )
    monkeypatch.setattr(rd, "_reset_escalation_scores", lambda ids: None)
    monkeypatch.setattr(rd, "_vacancy_scored_by", lambda ids: {})

    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    entry = rd._stage(state, "vacancy_scoring")
    rd._h_vacancy_scoring(state, entry, opts)  # emit screen
    entry["emitted"] = True

    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: set())
    monkeypatch.setattr(rd, "_vacancy_scores", lambda ids: {"v1": 80, "v2": 30})
    kind, _ = rd._h_vacancy_scoring(state, entry, opts)  # emit escalate (1 finalist)
    assert kind == "gate"

    # The session died: the finalist is still unscored -> no progress -> carry.
    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: {"v1"})
    kind, note = rd._h_vacancy_scoring(state, entry, opts)
    assert kind == "advance"
    assert entry["carried_over"] == 1
    assert "carried over" in note


def test_attended_scoring_gate_still_re_emits_without_progress(rd, monkeypatch, tmp_path):
    """Unattended off: a resume with nothing new saved re-emits the gate (the
    human is re-prompted), exactly as before."""
    import json as _json
    import subprocess

    _stub_two_pass(rd, monkeypatch, tmp_path, strong="opus", screen="opus")
    payloads = [_payload("v1"), _payload("v2")]
    monkeypatch.setattr(
        rd,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, _json.dumps(payloads), ""),
    )
    state = rd._new_state(rd.Opts())
    entry = rd._stage(state, "vacancy_scoring")
    rd._h_vacancy_scoring(state, entry, rd.Opts())
    entry["emitted"] = True

    monkeypatch.setattr(rd, "_unscored_vacancy_ids", lambda ids: {"v1", "v2"})
    for _ in range(2):  # no progress twice -> still a gate every time
        kind, info = rd._h_vacancy_scoring(state, entry, rd.Opts())
        assert kind == "gate"


def test_unattended_company_scoring_carries_over_without_progress(rd, monkeypatch, tmp_path):
    import json as _json

    monkeypatch.setattr(rd, "CO_PAYLOAD_PATH", tmp_path / "co_payload.json")
    rd.CO_PAYLOAD_PATH.write_text(_json.dumps([{"id": "a"}, {"id": "b"}]), encoding="utf-8")
    monkeypatch.setattr(rd, "_unscored_company_ids", lambda ids: {"a", "b"})

    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    entry = rd._stage(state, "company_scoring")
    entry["emitted"] = True
    entry["phase"] = "screened"
    entry["target_ids"] = ["a", "b"]

    # First unattended visit at this gate: no baseline yet -> emit.
    kind, info = rd._h_company_scoring(state, entry, opts)
    assert kind == "gate"
    assert info["action"] == "score_companies"

    # Re-entry with no progress -> carry over, run continues.
    kind, note = rd._h_company_scoring(state, entry, opts)
    assert kind == "advance"
    assert entry["carried_over"] == 2
    assert "carried over" in note


def test_unattended_company_scoring_re_emits_on_progress(rd, monkeypatch, tmp_path):
    import json as _json

    monkeypatch.setattr(rd, "CO_PAYLOAD_PATH", tmp_path / "co_payload.json")
    rd.CO_PAYLOAD_PATH.write_text(_json.dumps([{"id": "a"}, {"id": "b"}]), encoding="utf-8")
    monkeypatch.setattr(rd, "_unscored_company_ids", lambda ids: {"a", "b"})

    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    entry = rd._stage(state, "company_scoring")
    entry["emitted"] = True
    entry["phase"] = "screened"
    entry["target_ids"] = ["a", "b"]

    kind, _ = rd._h_company_scoring(state, entry, opts)  # baseline 2
    assert kind == "gate"

    monkeypatch.setattr(rd, "_unscored_company_ids", lambda ids: {"b"})
    kind, info = rd._h_company_scoring(state, entry, opts)  # 1 < 2 -> progress
    assert kind == "gate"
    assert info["count"] == 1

    kind, note = rd._h_company_scoring(state, entry, opts)  # stuck at 1 -> carry
    assert kind == "advance"
    assert entry["carried_over"] == 1


def test_unattended_company_scoring_without_firecrawl_still_skips(rd, monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(rd, "_candidates_to_score", lambda: 2)
    opts = rd.Opts(unattended=True)
    state = rd._new_state(opts)
    kind, note = rd._h_company_scoring(state, rd._stage(state, "company_scoring"), opts)
    assert kind == "skip"
    assert "FIRECRAWL_API_KEY" in note


def test_summary_prints_carried_over_and_rolled_over_counts(rd, capsys):
    state = rd._new_state(rd.Opts())
    rd._stage(state, "vacancy_scoring")["carried_over"] = 18
    rd._stage(state, "learning_review")["rolled_over"] = 4
    rd._print_summary(state, rd.Opts())
    out = capsys.readouterr().out
    assert "carried over" in out and "18" in out
    assert "rolled over" in out and "4" in out


def test_summary_stays_silent_without_rollovers(rd, capsys):
    rd._print_summary(rd._new_state(rd.Opts()), rd.Opts())
    out = capsys.readouterr().out
    assert "carried over" not in out
    assert "rolled over" not in out
