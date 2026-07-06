"""Stage observability + durable run history.

Covers three surfaces the ticket asks for, all with fixtures — no network, no
real pipeline run:

* run_card.py binds the live card to the CURRENT run id, so a prior run's
  leftover ``✓ done`` heartbeat can never be shown mid-fetch (the stale-card bug);
* run_status stamps every heartbeat with the run id from the env;
* pipeline_run persists one per-stage row per run and reads back a history range
  for the end-of-run expected-range check;
* run_daily's health verdict flags 0-new-while-sources-enabled and out-of-range
  counts.
"""

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


# ---------------------------------------------------------------------------
# run_card — the stale-card fix (bind the card to the current run id)
# ---------------------------------------------------------------------------


@pytest.fixture()
def card(monkeypatch, tmp_path):
    sys.modules.pop("run_card", None)
    import run_card

    importlib.reload(run_card)
    monkeypatch.setattr(run_card, "STATE_PATH", tmp_path / "run_state.json")
    monkeypatch.setattr(run_card, "STATUS_PATH", tmp_path / "run_status.json")
    return run_card


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


def _state(run_id="20260706-090000", finished=False, running="fetch"):
    stages = []
    for name in ("validate_profile", "fetch", "publish"):
        if name == running:
            stages.append({"name": name, "status": "running", "started_at": "2026-07-06T09:00:00"})
        else:
            stages.append({"name": name, "status": "pending"})
    return {"run_id": run_id, "finished": finished, "stages": stages}


def test_card_ignores_a_stale_prior_run_heartbeat(card):
    """The stale-card scenario: fetch is mid-flight this run, but the only heartbeat on
    disk is a prior run's finished one. The card must NOT show its ``✓ done`` /
    nonsensical elapsed — it falls back to the driver's live stage board."""
    _write(card.STATE_PATH, _state(run_id="NEW", running="fetch"))
    _write(
        card.STATUS_PATH,
        {
            "run_id": "OLD",  # different run — stale
            "stage": "fetch",
            "finished": True,
            "total": 10,
            "started_at": "2026-07-01T00:00:00",
        },
    )
    out = card.render()
    assert "done" not in out
    assert "fetch" in out and "starting" in out


def test_card_shows_live_heartbeat_when_run_ids_match(card):
    _write(card.STATE_PATH, _state(run_id="R1", running="fetch"))
    _write(
        card.STATUS_PATH,
        {
            "run_id": "R1",
            "stage": "fetch",
            "finished": False,
            "total": 85,
            "done": 18,
            "current": "LinkedIn",
            "started_at": "2026-07-06T09:00:00",
            "extra": {"new": 12},
        },
    )
    out = card.render()
    assert "18/85" in out
    assert "LinkedIn" in out
    assert "+12 new" in out


def test_card_reports_a_finished_run(card):
    _write(card.STATE_PATH, _state(run_id="R2", finished=True))
    out = card.render()
    assert "complete" in out


def test_card_shows_gate_pause_from_state_when_no_heartbeat(card):
    st = _state(run_id="R3", running="company_scoring")
    st["stages"] = [
        {"name": "company_scoring", "status": "blocked_gate", "started_at": "2026-07-06T09:00:00"}
    ]
    _write(card.STATE_PATH, st)
    out = card.render()
    assert "paused at gate" in out


def test_card_no_run(card):
    assert card.render() == "no run in progress"


def test_card_surfaces_an_errored_stage(card):
    """After a crash the card must say WHICH stage errored, not 'no run in
    progress' — an errored stage is where the run stopped."""
    st = _state(run_id="R4", running=None)
    st["stages"] = [
        {"name": "validate_profile", "status": "done"},
        {"name": "fetch", "status": "error", "started_at": "2026-07-06T09:00:00"},
    ]
    _write(card.STATE_PATH, st)
    out = card.render()
    assert "fetch" in out and "error" in out and "✗" in out


def test_card_prefers_error_over_a_bound_live_heartbeat(card):
    """A stage that crashed mid-heartbeat: the driver board (error) is more
    current than the stage's own last heartbeat — show the error."""
    st = _state(run_id="R5", running=None)
    st["stages"] = [{"name": "fetch", "status": "error", "started_at": "2026-07-06T09:00:00"}]
    _write(card.STATE_PATH, st)
    _write(
        card.STATUS_PATH,
        {
            "run_id": "R5",
            "stage": "fetch",
            "finished": False,
            "total": 85,
            "done": 18,
            "started_at": "2026-07-06T09:00:00",
        },
    )
    out = card.render()
    assert "error" in out and "18/85" not in out


# ---------------------------------------------------------------------------
# run_status — heartbeats carry the run id from the env
# ---------------------------------------------------------------------------


def test_run_status_stamps_run_id_from_env(monkeypatch, tmp_path):
    sys.modules.pop("run_status", None)
    import run_status

    importlib.reload(run_status)
    monkeypatch.setattr(run_status, "STATUS_PATH", tmp_path / "run_status.json")
    monkeypatch.setenv("JOBS_RUN_ID", "RUN-XYZ")

    run_status.mark("fetch", note="pulling roles")
    data = json.loads((tmp_path / "run_status.json").read_text())
    assert data["run_id"] == "RUN-XYZ"
    assert data["stage"] == "fetch"
    assert data["finished"] is False


def test_run_status_run_id_none_without_env(monkeypatch, tmp_path):
    sys.modules.pop("run_status", None)
    import run_status

    importlib.reload(run_status)
    monkeypatch.setattr(run_status, "STATUS_PATH", tmp_path / "run_status.json")
    monkeypatch.delenv("JOBS_RUN_ID", raising=False)

    run_status.begin("fetch", 5)
    data = json.loads((tmp_path / "run_status.json").read_text())
    assert data["run_id"] is None


# ---------------------------------------------------------------------------
# pipeline_run — durable per-stage history + history range for the health check
# ---------------------------------------------------------------------------


def _force_sqlite(monkeypatch, db_file):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "pipeline_run",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE


def _create_pipeline_run_table(db_file):
    sql = (Path(SCRIPTS).parent / "sql" / "migrations" / "0012_pipeline_run.sqlite.sql").read_text()
    con = sqlite3.connect(db_file)
    con.executescript(sql)
    con.commit()
    con.close()


def test_pipeline_run_persists_per_stage_and_upserts(monkeypatch, tmp_path):
    db = tmp_path / "jobsearch.db"
    _force_sqlite(monkeypatch, db)
    _create_pipeline_run_table(str(db))
    import pipeline_run

    state = {
        "run_id": "20260706-1",
        "finished": False,
        "gate": None,
        "stages": [
            {
                "name": "fetch",
                "status": "done",
                "note": "37 new vacancies",
                "started_at": "t0",
                "finished_at": "t1",
            },
            {"name": "filter", "status": "running", "started_at": "t2"},
        ],
    }
    pipeline_run.record(state, boards="linkedin,idealist", counts={"new_vacancies": 37})

    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT run_id, status, new_vacancies, boards, finished FROM pipeline_run"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "20260706-1"
    assert rows[0][2] == 37
    # stages JSON persisted with per-stage granularity
    stages_json = con.execute("SELECT stages FROM pipeline_run").fetchone()[0]
    assert "fetch" in stages_json and "filter" in stages_json
    con.close()

    # Second record with the same run_id UPDATES, never inserts a duplicate row.
    state["finished"] = True
    for s in state["stages"]:
        s["status"] = "done"
    pipeline_run.record(state, boards="linkedin,idealist", counts={"new_vacancies": 37})
    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT status, finished FROM pipeline_run").fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0][0] == "done"


def test_recent_new_vacancies_returns_finished_history(monkeypatch, tmp_path):
    db = tmp_path / "jobsearch.db"
    _force_sqlite(monkeypatch, db)
    _create_pipeline_run_table(str(db))
    import pipeline_run

    con = sqlite3.connect(str(db))
    con.executemany(
        "INSERT INTO pipeline_run (run_id, run_at, finished, status, new_vacancies) VALUES (?,?,?,?,?)",
        [
            ("r1", "2026-07-01", 1, "done", 40),
            ("r2", "2026-07-02", 1, "done", 55),
            ("r3", "2026-07-03", 0, "running", 999),  # unfinished — excluded
            ("cur", "2026-07-04", 1, "done", 3),  # current run — excluded by id
        ],
    )
    con.commit()
    con.close()

    hist = pipeline_run.recent_new_vacancies(limit=10, exclude_run_id="cur")
    assert sorted(hist) == [40, 55]


def test_record_is_best_effort_without_table(monkeypatch, tmp_path, capsys):
    """No pipeline_run table (DB not yet migrated) must NOT raise — a history
    write can never break the daily run. But not SILENTLY either: the first
    failure per process prints one stderr warning (else history would just
    never accumulate with zero trace, quietly killing run history again)."""
    db = tmp_path / "jobsearch.db"
    _force_sqlite(monkeypatch, db)
    import pipeline_run

    # No table created — record must not raise, and must warn exactly ONCE.
    pipeline_run.record({"run_id": "x", "stages": []}, counts={})
    pipeline_run.record({"run_id": "x", "stages": []}, counts={})
    assert pipeline_run.recent_new_vacancies(exclude_run_id="x") == []
    err = capsys.readouterr().err
    assert err.count("pipeline_run history") == 1
    assert "no durable history" in err


# ---------------------------------------------------------------------------
# run_daily — health verdict + gate preview + observe gating
# ---------------------------------------------------------------------------


@pytest.fixture()
def rd(monkeypatch, tmp_path):
    sys.modules.pop("run_daily", None)
    import run_daily

    importlib.reload(run_daily)
    monkeypatch.setattr(run_daily, "STATE_PATH", tmp_path / "run_state.json")
    monkeypatch.setattr(run_daily, "FETCH_STATS_PATH", tmp_path / "fetch_stats.json")
    return run_daily


def test_health_flags_zero_new_with_boards_enabled(rd):
    rd.FETCH_STATS_PATH.write_text(json.dumps({"total_new": 0, "orgs": {}, "errors": {}}))
    state = rd._new_state(rd.Opts())
    lines = rd._health_lines(state, rd.Opts(job_boards="linkedin"))
    assert lines and "OUT OF RANGE" in lines[0]


def test_health_flags_out_of_range_vs_history(rd, monkeypatch):
    rd.FETCH_STATS_PATH.write_text(
        json.dumps({"total_new": 500, "orgs": {"a": {"live": 3}}, "errors": {}})
    )
    import pipeline_run

    monkeypatch.setattr(pipeline_run, "recent_new_vacancies", lambda **k: [40, 55, 60])
    state = rd._new_state(rd.Opts())
    lines = rd._health_lines(state, rd.Opts())
    assert "OUT OF RANGE" in lines[0] and "40-60" in lines[0]


def test_health_in_range(rd, monkeypatch):
    rd.FETCH_STATS_PATH.write_text(
        json.dumps({"total_new": 50, "orgs": {"a": {"live": 3}}, "errors": {}})
    )
    import pipeline_run

    monkeypatch.setattr(pipeline_run, "recent_new_vacancies", lambda **k: [40, 55, 60])
    state = rd._new_state(rd.Opts())
    lines = rd._health_lines(state, rd.Opts())
    assert "in range" in lines[0] and "OUT OF RANGE" not in lines[0]


def test_health_all_sources_errored(rd, monkeypatch):
    rd.FETCH_STATS_PATH.write_text(
        json.dumps({"total_new": 0, "orgs": {"a": {"live": 0}}, "errors": {"a": "500"}})
    )
    import pipeline_run

    monkeypatch.setattr(pipeline_run, "recent_new_vacancies", lambda **k: [40])
    state = rd._new_state(rd.Opts())
    lines = rd._health_lines(state, rd.Opts())
    assert "OUT OF RANGE" in lines[0]


def test_health_silent_when_fetch_never_ran(rd):
    # No fetch_stats file → no new_vacancies → nothing to judge.
    state = rd._new_state(rd.Opts())
    assert rd._health_lines(state, rd.Opts()) == []


def test_gate_preview_names_the_task(rd):
    assert "WANT-score 5" in rd._gate_preview("score_companies", 5)
    assert "score 12 role" in rd._gate_preview("score_vacancies", 12)
    assert "verdict" in rd._gate_preview("verdicts", 3)


def test_drive_without_observe_writes_no_history(rd, monkeypatch):
    """The stage-machine unit path stays side-effect free: observe defaults off,
    so drive() records no history and prints the plain finish note."""
    calls = []
    monkeypatch.setattr(rd, "_record_history", lambda *a, **k: calls.append(1))
    rd.HANDLERS = {name: (lambda s, e, o: ("advance", "ok")) for name in rd.STAGE_ORDER}
    state = rd._new_state(rd.Opts())
    assert rd.drive(state, rd.Opts()) == rd.EXIT_DONE
    assert calls == []


def test_drive_with_observe_records_history(rd, monkeypatch):
    calls = []
    monkeypatch.setattr(rd, "_record_history", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(rd, "_announce_start", lambda name, opts: None)
    rd.HANDLERS = {name: (lambda s, e, o: ("advance", "ok")) for name in rd.STAGE_ORDER}
    state = rd._new_state(rd.Opts())
    assert rd.drive(state, rd.Opts(), observe=True) == rd.EXIT_DONE
    assert len(calls) >= len(rd.STAGE_ORDER)  # one per stage + the final done


def test_run_counts_from_fetch_stats(rd):
    rd.FETCH_STATS_PATH.write_text(json.dumps({"total_new": 37}))
    state = rd._new_state(rd.Opts())
    assert rd._run_counts(state)["new_vacancies"] == 37
