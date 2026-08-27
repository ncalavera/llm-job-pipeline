"""Tests for the nightly wrapper (scripts/nightly_run.py) — U4.

The driver and the ``claude`` binary are BOTH replaced by generated fake
scripts (via the NIGHTLY_DRIVER_CMD / NIGHTLY_CLAUDE_BIN seams); Telegram is a
captured in-process fake. No network, no Postgres, no real model. The one
end-to-end slice (the malformed score_out file) runs the real save script
against a migrated temp SQLite DB.
"""

import importlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FAKE_DRIVER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    vac, steps_path = sys.argv[1], sys.argv[2]
    flags = sys.argv[3:]
    with open(steps_path) as fh:
        data = json.load(fh)
    data.setdefault("calls", []).append(flags)
    i = data.get("i", 0)
    steps = data["steps"]
    step = steps[i] if i < len(steps) else {"exit": 0}
    data["i"] = i + 1
    with open(steps_path, "w") as fh:
        json.dump(data, fh)
    if step.get("crash"):
        raise RuntimeError("boom before state write")
    state_path = os.path.join(vac, "run_state.json")
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except Exception:
        state = None
    if "--new" in flags or state is None:
        state = {"run_id": "night-test", "created_at": "2026-01-01T00:00:00",
                 "options": {"unattended": "--unattended" in flags},
                 "stages": [], "finished": False, "gate": None}
    state["updated_at"] = "2026-01-01T00:00:01"
    code = int(step.get("exit", 0))
    PAYLOADS = {"score_vacancies": "score_vacancies_payload.json",
                "score_companies": "score_companies_payload.json",
                "screen_companies": "screen_companies_payload.json"}
    if code == 10:
        action = step["action"]
        state["gate"] = {"stage": action, "action": action}
        if step.get("payload") is not None and action in PAYLOADS:
            with open(os.path.join(vac, PAYLOADS[action]), "w") as fh:
                json.dump(step["payload"], fh)
    elif code == 0:
        state["finished"] = True
        state["gate"] = None
    elif code in (20, 30):
        state["stages"] = [{"name": step.get("stage", "?"),
                            "status": "aborted" if code == 20 else "error",
                            "note": step.get("note", "")}]
        state["gate"] = None
    with open(state_path, "w") as fh:
        json.dump(state, fh)
    sys.exit(code)
    """
)

# Baked per-fixture: %(mode_file)s (the test writes the mode there) and
# %(resume_cmd)s (the driver-resume line for the resume_mid mode).
FAKE_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, subprocess, sys, time
    mode = open(%(mode_file)r).read().strip()
    args = sys.argv[1:]
    prompt = args[args.index("-p") + 1]
    _, action, night = prompt.split()
    with open(os.path.join(night, "claude_env-" + action + ".json"), "w") as fh:
        json.dump(dict(os.environ), fh)
    in_dir = os.path.join(night, "score_in")
    out_dir = os.path.join(night, "score_out")
    ins = sorted(os.listdir(in_dir))

    def result(name):
        with open(os.path.join(in_dir, name)) as fh:
            item = json.load(fh)
        out = {"score": 55, "reasoning": "night fake", "tags": [],
               "hard_requirements": [], "short_summary": "s" * 220,
               "org": item.get("org", "?"), "title": item.get("title", "?")}
        if "member_ids" in item:
            out["member_ids"] = item["member_ids"]
        if "id" in item:
            out["id"] = item["id"]
            out["keep"] = False
        return out

    def write(name, text):
        with open(os.path.join(out_dir, name), "w") as fh:
            fh.write(text)

    if mode == "score_all":
        for n in ins:
            write(n, json.dumps(result(n)))
    elif mode == "score_half":
        for n in ins[: max(1, len(ins) // 2)]:
            write(n, json.dumps(result(n)))
    elif mode == "nothing":
        pass
    elif mode == "hang":
        time.sleep(30)
    elif mode == "auth_error":
        sys.stderr.write("Error: invalid OAuth token, please run /login\\n")
        sys.exit(1)
    elif mode == "other_error":
        sys.stderr.write("Error: the model fell over sideways\\n")
        sys.exit(1)
    elif mode == "malformed_one":
        write(ins[0], json.dumps(result(ins[0])))
        write(ins[1], "{{{ this is not json")
    elif mode == "resume_mid":
        for n in ins[: max(1, len(ins) // 2)]:
            write(n, json.dumps(result(n)))
        subprocess.call(%(resume_cmd)s)
    sys.exit(0)
    """
)


@pytest.fixture()
def nr(monkeypatch, tmp_path):
    """Fresh nightly_run with paths, driver, claude and Telegram all faked."""
    sys.modules.pop("nightly_run", None)
    import nightly_run

    importlib.reload(nightly_run)

    vac = tmp_path / "vacancies"
    vac.mkdir()
    monkeypatch.setattr(nightly_run, "VACANCIES_DIR", vac)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-123456-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    calls = []

    def fake_tg(token, method, payload, timeout=15, retries=2):
        calls.append((method, payload))
        return {}

    monkeypatch.setattr(nightly_run, "tg_call", fake_tg)

    steps_path = tmp_path / "driver_steps.json"
    driver = tmp_path / "fake_driver.py"
    driver.write_text(FAKE_DRIVER, encoding="utf-8")
    driver_cmd = f"{sys.executable} {driver} {vac} {steps_path}"
    monkeypatch.setenv("NIGHTLY_DRIVER_CMD", driver_cmd)

    mode_file = tmp_path / "claude_mode.txt"
    mode_file.write_text("score_all", encoding="utf-8")
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        FAKE_CLAUDE
        % {
            "mode_file": str(mode_file),
            "resume_cmd": repr([sys.executable, str(driver), str(vac), str(steps_path), "--resume"]),
        },
        encoding="utf-8",
    )
    monkeypatch.setenv("NIGHTLY_CLAUDE_BIN", f"{sys.executable} {claude}")

    def set_steps(steps):
        steps_path.write_text(json.dumps({"steps": steps, "i": 0}), encoding="utf-8")

    set_steps([])  # driver_calls() must work even when a test never sets steps

    def set_mode(mode):
        mode_file.write_text(mode, encoding="utf-8")

    def driver_calls():
        return json.loads(steps_path.read_text(encoding="utf-8")).get("calls", [])

    def texts():
        return [p["text"] for m, p in calls if m == "sendMessage"]

    def night_dir():
        return vac / "nightly" / date.today().isoformat()

    def wrapper_log():
        try:
            return (night_dir() / "wrapper.log").read_text(encoding="utf-8")
        except OSError:
            return ""

    yield SimpleNamespace(
        mod=nightly_run,
        vac=vac,
        tmp=tmp_path,
        calls=calls,
        set_steps=set_steps,
        set_mode=set_mode,
        driver_calls=driver_calls,
        texts=texts,
        night_dir=night_dir,
        wrapper_log=wrapper_log,
    )


def _vac_payload(n, prefix="v"):
    return [
        {
            "member_ids": [f"{prefix}{i}"],
            "org": f"Org {i}",
            "title": f"Role {i}",
            "system_prompt": "score it",
            "user_msg": "the role",
        }
        for i in range(n)
    ]


def _co_payload(n, prefix="c"):
    return [
        {"id": f"{prefix}{i}", "system_prompt": "screen it", "user_msg": f"Company {i}"}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# The happy path and the partial path
# ---------------------------------------------------------------------------


def test_ae1_driver_gates_claude_scores_all_no_alert(nr):
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(3)}, {"exit": 0}]
    )
    assert nr.mod.run_night() == 0
    assert nr.texts() == []  # a clean night sends no alert
    out = sorted((nr.night_dir() / "score_out").glob("*.json"))
    assert len(out) == 3
    calls = nr.driver_calls()
    assert calls[0] == ["--new", "--unattended"]
    assert calls[1] == ["--resume"]  # options are frozen in the checkpoint


def test_partial_then_second_session_scores_the_rest(nr):
    nr.set_steps(
        [
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(4)},
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2, prefix="w")},
            {"exit": 0},
        ]
    )
    nr.set_mode("score_half")
    assert nr.mod.run_night() == 0
    log = nr.wrapper_log()
    assert "gate trip 1/8: score_vacancies" in log
    assert "gate trip 2/8: score_vacancies" in log
    assert "carried over" in log  # each session's shortfall is named
    assert nr.texts() == []


# ---------------------------------------------------------------------------
# The ways a session goes wrong (AE7, hang, early exits)
# ---------------------------------------------------------------------------


def test_ae7_no_progress_alerts_and_still_reaches_digest(nr):
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(8)}, {"exit": 0}]
    )
    nr.set_mode("nothing")
    assert nr.mod.run_night() == 0
    state = json.loads((nr.vac / "run_state.json").read_text(encoding="utf-8"))
    assert state["no_progress"] is True
    assert state["finished"] is True  # the run still ran out to the digest
    assert any("no progress" in t for t in nr.texts())
    assert len(nr.driver_calls()) == 2  # --new, then the finishing --resume


def test_hanging_session_is_killed_and_the_run_continues(nr, monkeypatch, tmp_path):
    toml = tmp_path / "defaults.toml"
    toml.write_text("[nightly]\nvacancy_gate_minutes = 0.02\n", encoding="utf-8")
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(toml))
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("hang")
    assert nr.mod.run_night() == 0
    assert "session killed at its" in nr.wrapper_log()
    assert nr.driver_calls()[-1] == ["--resume"]  # the loop went on after the kill


def test_fast_auth_exit_alerts_login_failure(nr):
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("auth_error")
    assert nr.mod.run_night() == 0
    assert any("login failure" in t for t in nr.texts())


def test_fast_other_exit_alerts_see_transcript(nr):
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("other_error")
    assert nr.mod.run_night() == 0
    assert any("exited early, see transcript" in t for t in nr.texts())


def test_claude_resuming_mid_batch_reports_the_shortfall(nr):
    nr.set_steps(
        [
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(4)},
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2, prefix="w")},
            {"exit": 0},
        ]
    )
    nr.set_mode("resume_mid")
    assert nr.mod.run_night() == 0
    assert "unscored — carried over" in nr.wrapper_log()


# ---------------------------------------------------------------------------
# Gate dispatch: company gates, non-scoring gates, the trip cap
# ---------------------------------------------------------------------------


def test_both_company_gates_use_their_own_save_command(nr, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-secret-key")
    nr.set_steps(
        [
            {"exit": 10, "action": "screen_companies", "payload": _co_payload(2)},
            {"exit": 10, "action": "score_companies", "payload": _co_payload(2, prefix="d")},
            {"exit": 0},
        ]
    )
    assert nr.mod.run_night() == 0
    log = nr.wrapper_log()
    assert "screen_candidates.py --save --files" in log
    assert "score_companies.py --save --files" in log
    assert "gate trip 2/8" in log
    # FIRECRAWL_API_KEY reaches the COMPANY gates' sessions (KTD3).
    env = json.loads((nr.night_dir() / "claude_env-screen_companies.json").read_text())
    assert env.get("FIRECRAWL_API_KEY") == "fc-secret-key"


def test_claude_env_is_scrubbed_of_telegram_and_firecrawl_on_vacancy_gate(nr, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-secret-key")
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(1)}, {"exit": 0}]
    )
    assert nr.mod.run_night() == 0
    env = json.loads((nr.night_dir() / "claude_env-score_vacancies.json").read_text())
    assert not any(k.startswith("TELEGRAM") for k in env)
    assert "FIRECRAWL_API_KEY" not in env  # vacancy gate needs no Firecrawl
    assert "PATH" in env and "HOME" in env


def test_claude_env_reads_the_token_from_the_systemd_credentials_dir(nr, monkeypatch, tmp_path):
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "claude-token").write_text("sk-night-token\n", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(1)}, {"exit": 0}]
    )
    assert nr.mod.run_night() == 0
    env = json.loads((nr.night_dir() / "claude_env-score_vacancies.json").read_text())
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-night-token"
    # The token stays out of the wrapper's own environment (KTD7).
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def test_ae8_learning_gate_is_left_to_the_driver_and_scoring_still_runs(nr):
    nr.set_steps(
        [
            {"exit": 10, "action": "learning_review"},
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)},
            {"exit": 0},
        ]
    )
    assert nr.mod.run_night() == 0
    log = nr.wrapper_log()
    assert "gate 'learning_review' needs no Claude session" in log
    assert not (nr.night_dir() / "claude_env-learning_review.json").exists()
    assert len(list((nr.night_dir() / "score_out").glob("*.json"))) == 2
    # R16: nothing anywhere wrote a verdict status.
    assert not (nr.vac / "verdict_writes.log").exists()
    assert nr.texts() == []


def test_gate_loop_cap_of_eight_alerts_and_stops(nr):
    nr.set_steps([{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(1)}] * 12)
    nr.set_mode("nothing")
    assert nr.mod.run_night() == 0
    assert any("cap of 8 gate sessions" in t for t in nr.texts())
    # --new + 8 resumes; the 9th trip fires the alert instead of a session.
    assert len(nr.driver_calls()) == 9


# ---------------------------------------------------------------------------
# Driver failures → the R12 alert (AE5, masking, unknown stage)
# ---------------------------------------------------------------------------


def test_ae5_fetch_abort_alert_names_the_stage_and_error(nr):
    nr.set_steps(
        [{"exit": 20, "stage": "fetch", "note": "fetch exited with code 2: connection refused"}]
    )
    assert nr.mod.run_night() == 0
    texts = nr.texts()
    assert len(texts) == 1
    assert "fetch" in texts[0]
    assert "connection refused" in texts[0]


def test_alert_masks_database_credentials(nr):
    nr.set_steps(
        [
            {
                "exit": 30,
                "stage": "preflight",
                "note": (
                    "OperationalError: could not connect to "
                    "postgres://user:secretpass@db.example/x"
                ),
            }
        ]
    )
    assert nr.mod.run_night() == 0
    texts = nr.texts()
    assert "OperationalError" in texts[0]
    assert "secretpass" not in texts[0]
    assert "://user:***@" in texts[0]


def test_driver_crash_before_state_says_unknown_stage_with_exception_class(nr):
    nr.set_steps([{"exit": 1, "crash": True}])
    assert nr.mod.run_night() == 0
    texts = nr.texts()
    assert "unknown stage" in texts[0]
    assert "RuntimeError" in texts[0]  # the driver.log tail names the class


def test_failed_alert_send_exits_nonzero(nr):
    nr.set_steps(
        [{"exit": 20, "stage": "fetch", "note": "fetch exited with code 2: connection refused"}]
    )

    def boom(token, method, payload, timeout=15, retries=2):
        raise RuntimeError("chat not found")

    nr.mod.tg_call = boom
    assert nr.mod.run_night() == 1


# ---------------------------------------------------------------------------
# Locking, parked manual runs, the deadline, SIGTERM
# ---------------------------------------------------------------------------


def test_manual_checkpoint_parks_the_night(nr):
    (nr.vac / "run_state.json").write_text(
        json.dumps(
            {"run_id": "manual", "finished": False, "options": {"unattended": False}}
        ),
        encoding="utf-8",
    )
    assert nr.mod.run_night() == 0
    assert any("manual run" in t for t in nr.texts())
    assert nr.driver_calls() == []  # --new never ran; the parked run is untouched


def test_lock_held_second_wrapper_notes_and_exits_zero(nr):
    import fcntl

    lock_path = nr.vac / "nightly.lock"
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert nr.mod.run_night() == 0
    finally:
        holder.close()
    assert any("lock" in t for t in nr.texts())
    assert nr.driver_calls() == []


def test_deadline_reached_skips_claude_and_says_scoring_skipped(nr, monkeypatch, tmp_path):
    toml = tmp_path / "defaults.toml"
    # ~0.6 ms: already past by the time the first driver subprocess returns.
    toml.write_text("[nightly]\nrun_deadline_minutes = 0.00001\n", encoding="utf-8")
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(toml))
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(4)}, {"exit": 0}]
    )
    assert nr.mod.run_night() == 0
    assert not (nr.night_dir() / "claude-score_vacancies.jsonl").exists()
    assert any("scoring skipped" in t for t in nr.texts())
    assert nr.driver_calls()[-1] == ["--resume"]  # the run still ran out to the digest


def test_sigterm_sends_the_alert_and_exits(nr):
    import settings

    night = nr.night_dir()
    night.mkdir(parents=True, exist_ok=True)
    ctx = nr.mod._Ctx(settings.nightly(), night, datetime.now() + timedelta(hours=1))
    nr.mod._CURRENT = ctx
    try:
        with pytest.raises(SystemExit) as exc:
            nr.mod._handle_sigterm(signal.SIGTERM, None)
    finally:
        nr.mod._CURRENT = None
    assert exc.value.code == 0  # the alert went out, so the exit is clean
    assert any("SIGTERM" in t for t in nr.texts())


# ---------------------------------------------------------------------------
# The night directory: privacy and retention (R17)
# ---------------------------------------------------------------------------


def test_night_directory_mode_and_seven_day_prune(nr):
    old = nr.vac / "nightly" / (date.today() - timedelta(days=9)).isoformat()
    recent = nr.vac / "nightly" / (date.today() - timedelta(days=2)).isoformat()
    for d in (old, recent):
        d.mkdir(parents=True)
        (d / "wrapper.log").write_text("x", encoding="utf-8")
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(1)}, {"exit": 0}]
    )
    assert nr.mod.run_night() == 0
    night = nr.night_dir()
    assert stat.S_IMODE(night.stat().st_mode) == 0o700
    assert (night / "wrapper.log").exists()
    assert (night / "driver.log").exists()
    assert (night / "claude-score_vacancies.jsonl").exists()
    assert (night / "claude-score_vacancies.err").exists()
    assert not old.exists()  # nine days old → pruned
    assert recent.exists()  # two days old → kept


# ---------------------------------------------------------------------------
# One real end-to-end slice: the defensive save sweep on a migrated SQLite DB
# ---------------------------------------------------------------------------


def test_malformed_score_out_file_does_not_block_the_others(nr, monkeypatch, tmp_path):
    db_path = tmp_path / "night.db"
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_path))
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    res = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "migrate.py")],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    assert res.returncode == 0, res.stdout + res.stderr

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO company (id, canonical_name, status) VALUES ('co1', 'Night Org', 'active')"
    )
    for vid in ("va1", "va2"):
        cur.execute(
            "INSERT INTO vacancy (id, dedup_hash, company_id, title, status, "
            "first_seen, last_seen) "
            "VALUES (?, ?, 'co1', ?, 'unseen', '2026-01-01', '2026-01-01')",
            (vid, f"hash-{vid}", f"Role {vid}"),
        )
    conn.commit()
    conn.close()

    nr.set_steps(
        [
            {
                "exit": 10,
                "action": "score_vacancies",
                "payload": [
                    {"member_ids": ["va1"], "org": "Night Org", "title": "Role va1"},
                    {"member_ids": ["va2"], "org": "Night Org", "title": "Role va2"},
                ],
            },
            {"exit": 0},
        ]
    )
    nr.set_mode("malformed_one")
    assert nr.mod.run_night() == 0
    conn = sqlite3.connect(db_path)
    scores = dict(conn.execute("SELECT id, llm_score FROM vacancy").fetchall())
    conn.close()
    assert scores["va1"] == 55  # the good file saved
    assert scores["va2"] is None  # the malformed one was skipped, not fatal
    assert "malformed" in nr.wrapper_log()


# ---------------------------------------------------------------------------
# Static contracts: the prompt files and --dry-run
# ---------------------------------------------------------------------------


def test_prompt_file_names_only_the_night_scorer_agent():
    text = (PROJECT_ROOT / ".claude" / "commands" / "jobs-night.md").read_text(encoding="utf-8")
    assert "night-scorer" in text
    assert "general-purpose" not in text
    for forbidden in (
        "learning.py apply",
        "gh issue create",
        "--full-rescore",
        "--archive",
    ):
        assert forbidden in text  # each is named in the forbidden list
    agent = (PROJECT_ROOT / ".claude" / "agents" / "night-scorer.md").read_text(encoding="utf-8")
    assert "tools: Read, Write" in agent


def test_dry_run_prints_the_dispatch_table_with_masked_commands(nr, capsys):
    assert nr.mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    for action in ("screen_companies", "score_companies", "score_vacancies"):
        assert action in out
    assert "--dangerously-skip-permissions" in out
    assert "opus" in out
    assert "TELEGRAM_* never passed" in out
    assert "tok-123456-secret" not in out  # the token value never prints
    assert nr.driver_calls() == []  # dry run launches nothing
