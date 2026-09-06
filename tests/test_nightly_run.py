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
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Fidelity: like the real driver, each gate step writes its payload file anew
# (run_daily.py rewrites the payload to the remaining subset on re-emission)
# and stamps the gate's phase into the checkpoint's gate dict.
FAKE_DRIVER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    vac, steps_path = sys.argv[1], sys.argv[2]
    flags = sys.argv[3:]
    with open(os.path.join(vac, "driver_env.json"), "w") as fh:
        json.dump(dict(os.environ), fh)
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
        state["gate"] = {"stage": action, "action": action, "phase": step.get("phase")}
        if step.get("payload") is not None and action in PAYLOADS:
            with open(os.path.join(vac, PAYLOADS[action]), "w") as fh:
                json.dump(step["payload"], fh)
    elif code == 0:
        state["finished"] = True
        state["gate"] = None
        if step.get("stages"):
            state["stages"] = step["stages"]
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
    _, action, night, phase = prompt.split()
    with open(os.path.join(night, "claude_env-" + action + ".json"), "w") as fh:
        json.dump(dict(os.environ), fh)
    with open(os.path.join(night, "claude_args-" + action + ".json"), "w") as fh:
        json.dump(args, fh)
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
    elif mode == "score_slow":
        for n in ins:
            write(n, json.dumps(result(n)))
        time.sleep(5.2)
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
    elif mode.startswith("rate_limit"):
        # The real 2026-08-27 shape: utilization warnings, then the rejected
        # event, then a result line with api_error_status 429. Everything the
        # session managed before the limit is already in score_out.
        # "rate_limit_once" hits the limit on its first session only, so the
        # retry after the wait can score the rest.
        reset = int(open(%(reset_file)r).read().strip())
        flag = os.path.join(night, "limit_hit")
        if mode == "rate_limit_once" and os.path.exists(flag):
            for n in ins:
                write(n, json.dumps(result(n)))
            sys.exit(0)
        open(flag, "w").close()
        if mode in ("rate_limit_half", "rate_limit_once"):
            for n in ins[: max(1, len(ins) // 2)]:
                write(n, json.dumps(result(n)))
        for util in (0.97, 0.98, 0.99):
            sys.stdout.write(json.dumps({"type": "rate_limit_event", "rate_limit_info":
                {"status": "allowed_warning", "utilization": util,
                 "resetsAt": reset, "rateLimitType": "five_hour"}}) + "\\n")
        sys.stdout.write(json.dumps({"type": "rate_limit_event", "rate_limit_info":
            {"status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour",
             "overageStatus": "rejected", "overageDisabledReason": "out_of_credits",
             "isUsingOverage": False}}) + "\\n")
        sys.stdout.write(json.dumps({"type": "result", "subtype": "error_during_execution",
            "is_error": True, "api_error_status": 429,
            "result": "You've hit your session limit - resets 10:40pm (UTC)"}) + "\\n")
        sys.stdout.flush()
        sys.exit(1)
    elif mode == "malformed_one":
        write(ins[0], json.dumps(result(ins[0])))
        write(ins[1], "{{{ this is not json")
    elif mode == "all_failed":
        for n in ins:
            write(n, json.dumps({"failed": "page unreachable, no evidence"}))
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
    # The usage-limit modes read their resetsAt stamp from here: six minutes
    # ahead, as on the real 2026-08-27 night.
    reset_file = tmp_path / "claude_reset.txt"
    reset_file.write_text(str(int(time.time()) + 360), encoding="utf-8")
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        FAKE_CLAUDE
        % {
            "mode_file": str(mode_file),
            "reset_file": str(reset_file),
            "resume_cmd": repr(
                [sys.executable, str(driver), str(vac), str(steps_path), "--resume"]
            ),
        },
        encoding="utf-8",
    )
    monkeypatch.setenv("NIGHTLY_CLAUDE_BIN", f"{sys.executable} {claude}")

    # Fake clock: the wait computes its real duration but never spends it.
    slept = []
    monkeypatch.setattr(nightly_run, "_sleep", lambda s: slept.append(s))

    def set_steps(steps):
        steps_path.write_text(json.dumps({"steps": steps, "i": 0}), encoding="utf-8")

    set_steps([])  # driver_calls() must work even when a test never sets steps

    def set_mode(mode):
        mode_file.write_text(mode, encoding="utf-8")

    def set_reset(epoch):
        reset_file.write_text(str(int(epoch)), encoding="utf-8")

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
        set_reset=set_reset,
        slept=slept,
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


def test_all_failed_result_files_count_as_no_progress(nr):
    # A night-scorer that cannot score writes {"failed": ...} — the save script
    # saves nothing for it, so a session of only such files made no progress.
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("all_failed")
    assert nr.mod.run_night() == 0
    state = json.loads((nr.vac / "run_state.json").read_text(encoding="utf-8"))
    assert state["no_progress"] is True
    assert any("no progress" in t for t in nr.texts())
    assert "2 result file(s) failed or malformed" in nr.wrapper_log()


def test_company_gate_stall_alerts_but_sets_no_flag(nr):
    # The digest's no-progress header line is about vacancy scoring only — a
    # stalled company gate keeps its alert + carry-over without the flag.
    nr.set_steps(
        [{"exit": 10, "action": "screen_companies", "payload": _co_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("nothing")
    assert nr.mod.run_night() == 0
    state = json.loads((nr.vac / "run_state.json").read_text(encoding="utf-8"))
    assert "no_progress" not in state
    assert any("no progress" in t for t in nr.texts())


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


def test_fast_other_exit_alerts_in_plain_words(nr):
    """A dead session alerts with a sentence and a log pointer — never a raw
    stream-json blob (the transcript itself stays in the night log)."""
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("other_error")
    assert nr.mod.run_night() == 0
    alert = next(t for t in nr.texts() if "score_vacancies" in t)
    assert "the session stopped early (exit 1)" in alert
    assert "Unscored roles carry over" in alert
    assert "Log: nightly/" in alert
    assert "{" not in alert  # no JSON reached the phone


def test_rate_limited_session_pauses_instead_of_alerting_a_failure(nr):
    """A usage limit is no longer a failure. The night waits it out and retries
    the same gate, so the phone gets the pause sentence — never a failure alert,
    and never JSON. The transcript stays on disk for a real investigation."""
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("rate_limit")
    assert nr.mod.run_night() == 0
    texts = nr.texts()
    # The old behaviour: "Night run failed at score_vacancies: ...". Gone.
    assert not any("failed at" in t for t in texts), texts
    paused = next(t for t in texts if "usage limit" in t)
    assert "nothing is lost" in paused or "carry over" in paused
    # None of the machine output leaks into any message on the phone.
    for t in texts:
        assert "is_error" not in t and "api_error_status" not in t and "{" not in t
    assert (nr.night_dir() / "claude-score_vacancies.jsonl").exists()


def test_reset_clock_accepts_milliseconds(nr):
    assert nr.mod._reset_clock(1756400000000) == nr.mod._reset_clock(1756400000)


# ---------------------------------------------------------------------------
# The Claude usage limit: wait it out and retry the same gate (2026-08-27)
# ---------------------------------------------------------------------------

_ALERT_PREFIX = "🌙 Night run: "


def _rate_limit_alert(nr):
    return next(t for t in nr.texts() if "usage limit" in t)


def test_reset_is_read_from_a_real_shaped_transcript(nr, tmp_path):
    """The rejected event's resetsAt wins over the warnings that led up to it,
    and a session that died of anything else reads as no limit at all."""
    out = tmp_path / "t.jsonl"
    err = tmp_path / "t.err"
    err.write_text("", encoding="utf-8")
    out.write_text(
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "allowed_warning",
                    "utilization": 0.99,
                    "resetsAt": 1787860000,
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "resetsAt": 1787870400,
                    "rateLimitType": "five_hour",
                    "overageDisabledReason": "out_of_credits",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 429,
                "result": "You've hit your session limit",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert nr.mod._rate_limit_reset(out, err) == 1787870400

    out.write_text('{"type":"result","is_error":true,"result":"the model fell over"}\n')
    assert nr.mod._rate_limit_reset(out, err) is None


def test_usage_limit_waits_then_retries_the_same_gate_without_a_trip(nr):
    """The 2026-08-27 bug: the wrapper burned a gate trip on a nine-second 429
    and ended the night three hours early. It now sleeps out the limit and
    re-runs the same gate on the trip it already paid for."""
    nr.set_steps(
        [
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(4)},
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(4, prefix="w")},
            {"exit": 0},
        ]
    )
    nr.set_mode("rate_limit_once")
    assert nr.mod.run_night() == 0
    log = nr.wrapper_log()
    assert "the Claude usage limit stopped the session" in log
    assert "gate trip 1/8 again: score_vacancies (after the usage-limit wait)" in log
    assert "gate trip 2/8" not in log  # the wait cost no trip
    assert len(nr.slept) == 1 and nr.slept[0] > 0  # it really waited (fake clock)
    # The retry scored the whole re-emitted payload — the night went on.
    assert len(list((nr.night_dir() / "score_out").glob("*.json"))) == 4
    assert len(nr.texts()) == 1  # the pause is the ONLY message; no failure alert


def test_the_wait_is_logged_with_start_reset_and_resume(nr):
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("rate_limit_once")
    assert nr.mod.run_night() == 0
    log = nr.wrapper_log()
    assert "usage limit wait 1/2: sleeping" in log
    assert "limit lifts" in log and "scoring resumes" in log
    assert "usage limit wait over at" in log


def test_the_pause_alert_is_one_plain_sentence_within_the_cap(nr):
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("rate_limit_once")
    assert nr.mod.run_night() == 0
    alert = _rate_limit_alert(nr)
    assert "scoring paused" in alert and "starts again at" in alert
    assert "nothing is lost" in alert
    assert len(alert) <= nr.mod.ALERT_MSG_CHARS + len(_ALERT_PREFIX)
    for jargon in ("{", "429", "resetsAt", "api_error_status", "HTTP", "rate_limit"):
        assert jargon not in alert


def test_wait_is_skipped_when_the_limit_lifts_after_the_deadline(nr):
    """Past the night's own deadline the wrapper must not sleep: it carries the
    work over exactly as it does today, and says so once."""
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}, {"exit": 0}]
    )
    nr.set_mode("rate_limit")
    nr.set_reset(time.time() + 60 * 60 * 24)  # tomorrow: far past a 225-minute night
    assert nr.mod.run_night() == 0
    assert nr.slept == []  # nothing slept
    alert = _rate_limit_alert(nr)
    assert "after tonight's deadline" in alert and "carry over" in alert
    assert len(alert) <= nr.mod.ALERT_MSG_CHARS + len(_ALERT_PREFIX)
    assert "after the deadline" in nr.wrapper_log()
    assert nr.driver_calls()[-1] == ["--resume"]  # the night still ran out to the digest


def test_two_waits_a_night_and_no_more(nr):
    """A pathological night that keeps hitting the limit stops waiting after
    RATE_LIMIT_WAITS — otherwise a stale reset stamp would spin the loop."""
    nr.set_steps(
        [
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)},
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2, prefix="w")},
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2, prefix="x")},
            {"exit": 0},
        ]
    )
    nr.set_mode("rate_limit_half")
    assert nr.mod.run_night() == 0
    assert len(nr.slept) == nr.mod.RATE_LIMIT_WAITS == 2
    last = nr.texts()[-1]
    assert "spent again after 2 waits tonight" in last and "carry over" in last
    assert len(last) <= nr.mod.ALERT_MSG_CHARS + len(_ALERT_PREFIX)
    assert "gate trip 2/8" not in nr.wrapper_log()  # both waits rode trip 1


def test_a_sigterm_during_the_wait_is_not_swallowed(nr):
    """R4: systemd stops the unit mid-sleep — the handler's SystemExit must
    leave through the wait, not be caught on the way out."""
    import settings

    night = nr.night_dir()
    night.mkdir(parents=True, exist_ok=True)
    ctx = nr.mod._Ctx(settings.nightly(), night, datetime.now() + timedelta(hours=1))
    nr.mod._CURRENT = ctx

    def stopped(seconds):
        nr.mod._handle_sigterm(signal.SIGTERM, None)

    nr.mod._sleep = stopped
    try:
        with pytest.raises(SystemExit) as exc:
            nr.mod._wait_out_rate_limit(ctx, int(time.time()) + 300, 0)
    finally:
        nr.mod._CURRENT = None
    assert exc.value.code == 0
    assert any("SIGTERM" in t for t in nr.texts())


def test_claude_resuming_mid_batch_reports_the_shortfall(nr):
    # MISBEHAVIOR tolerance: the session must never resume the driver (the
    # wrapper owns the loop, and jobs-night.md forbids run_daily.py), but a
    # session that does anyway must not derail the night — the wrapper's own
    # follow-up resume just lands on the re-emitted gate and carries on.
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
    # Credentials remain in the trusted Python stages.
    env = json.loads((nr.night_dir() / "claude_env-screen_companies.json").read_text())
    assert "FIRECRAWL_API_KEY" not in env
    assert "SUPABASE_DB_URL" not in env


def test_gate_phase_reaches_the_session_prompt(nr):
    # The session picks its subagent model tier by phase (jobs-night.md
    # override 2), so the wrapper must pass the gate's live phase through.
    nr.set_steps(
        [
            {
                "exit": 10,
                "action": "score_vacancies",
                "phase": "escalate",
                "payload": _vac_payload(1),
            },
            {"exit": 10, "action": "score_companies", "payload": _co_payload(1)},
            {"exit": 0},
        ]
    )
    assert nr.mod.run_night() == 0
    args = json.loads((nr.night_dir() / "claude_args-score_vacancies.json").read_text())
    prompt = args[args.index("-p") + 1]
    assert prompt.split() == ["/jobs-night", "score_vacancies", str(nr.night_dir()), "escalate"]
    # A gate without a phase (older checkpoint, non-two-pass gate) gets "score".
    args = json.loads((nr.night_dir() / "claude_args-score_companies.json").read_text())
    assert args[args.index("-p") + 1].split()[-1] == "score"


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


def test_claude_child_never_carries_an_anthropic_api_key(nr, monkeypatch):
    """The night scores on the Claude subscription through the headless login.
    Claude Code prefers an API key whenever it finds one, so an inherited key
    would silently move the whole night's spend to per-token billing — the
    child must not see one even when the wrapper's own environment has it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key-must-not-pass")
    assert "ANTHROPIC_API_KEY" not in nr.mod._CHILD_ENV_ALLOWLIST
    assert "ANTHROPIC_API_KEY" in nr.mod._SECRET_ENV_KEYS  # still masked in logs
    nr.set_steps(
        [{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(1)}, {"exit": 0}]
    )
    assert nr.mod.run_night() == 0
    env = json.loads((nr.night_dir() / "claude_env-score_vacancies.json").read_text())
    assert "ANTHROPIC_API_KEY" not in env
    assert "api-key-must-not-pass" not in json.dumps(env)
    # The driver keeps the full environment: its Python stages still see the
    # key if one is ever configured.
    driver_env = json.loads((nr.vac / "driver_env.json").read_text())
    assert driver_env["ANTHROPIC_API_KEY"] == "api-key-must-not-pass"


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
                    "OperationalError: could not connect to postgres://user:secretpass@db.example/x"
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


def test_digest_stage_error_alerts_even_though_the_run_finished(nr):
    # The digest fails SOFT inside the driver (error_continue keeps publish
    # alive) — the wrapper must still surface it, or the night ends silent.
    nr.set_steps(
        [
            {"exit": 10, "action": "score_vacancies", "payload": _vac_payload(1)},
            {
                "exit": 0,
                "stages": [
                    {"name": "digest", "status": "error", "note": "digest send exited with code 1"}
                ],
            },
        ]
    )
    assert nr.mod.run_night() == 0  # the run itself still counts as done
    texts = nr.texts()
    assert any("digest" in t and "exited with code 1" in t for t in texts)


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
        json.dumps({"run_id": "manual", "finished": False, "options": {"unattended": False}}),
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


def test_sigterm_terminates_whichever_child_is_running(nr):
    # _CHILD tracks the live subprocess — a driver or a Claude session — so a
    # systemd stop kills it instead of leaving it to run past the unit.
    import settings

    night = nr.night_dir()
    night.mkdir(parents=True, exist_ok=True)
    ctx = nr.mod._Ctx(settings.nightly(), night, datetime.now() + timedelta(hours=1))
    child = SimpleNamespace(calls=[])
    child.terminate = lambda: child.calls.append("terminate")
    nr.mod._CURRENT = ctx
    nr.mod._CHILD = child
    try:
        with pytest.raises(SystemExit) as exc:
            nr.mod._handle_sigterm(signal.SIGTERM, None)
    finally:
        nr.mod._CURRENT = None
        nr.mod._CHILD = None
    assert exc.value.code == 0
    assert child.calls == ["terminate"]


def test_wrapper_startup_crash_sends_the_alert_and_exits_zero(nr, monkeypatch):
    # A crash before the gate loop (settings, lock, night dir) has no ctx —
    # the module-level alert still goes out, and the exit stays zero because
    # the only wrapper failure is a lost alert.
    def boom():
        raise OSError("read-only file system")

    monkeypatch.setattr(nr.mod, "_make_night_dir", boom)
    assert nr.mod.run_night() == 0
    texts = nr.texts()
    assert any("wrapper startup" in t and "OSError" in t for t in texts)
    assert nr.driver_calls() == []  # nothing ran


def test_wrapper_startup_crash_with_dead_telegram_exits_nonzero(nr, monkeypatch):
    def boom():
        raise OSError("read-only file system")

    monkeypatch.setattr(nr.mod, "_make_night_dir", boom)

    def dead(token, method, payload, timeout=15, retries=2):
        raise RuntimeError("chat not found")

    nr.mod.tg_call = dead
    assert nr.mod.run_night() == 1


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
        "scripts/run_daily.py` (any invocation",  # the wrapper is the only resumer
        "learning.py apply",
        "gh issue create",
        "--full-rescore",
        "--archive",
    ):
        assert forbidden in text  # each is named in the forbidden list
    assert "--resume --unattended" not in text  # the session never runs the driver
    # Override 2 maps every gate+phase to its settings function.
    for fn in ("screen_model()", "scoring_model()", "company_screen_model()"):
        assert fn in text
    agent = (PROJECT_ROOT / ".claude" / "agents" / "night-scorer.md").read_text(encoding="utf-8")
    assert "tools: Read, Write" in agent


def test_dry_run_prints_the_dispatch_table_with_masked_commands(nr, capsys):
    import scoring_settings

    assert nr.mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    for action in ("screen_companies", "score_companies", "score_vacancies"):
        assert action in out
    assert "--dangerously-skip-permissions" not in out
    assert "--permission-mode dontAsk" in out
    assert "--tools Read,Write,Glob,Agent,TaskOutput,TaskStop" in out
    assert "--strict-mcp-config" in out
    assert "--disallowed-tools Bash,PowerShell,WebFetch,WebSearch,Edit,NotebookEdit" in out
    assert (
        "--setting-sources project,local" in out
    )  # user settings (shared with the Mac) never load
    # The orchestrator model is the configured setting, never a hardcoded name.
    assert f"--model {scoring_settings.scoring_model()}" in out
    # One session line per phase, phase as the prompt's third argument.
    for phase in ("screen", "escalate", "score"):
        assert f"session ({phase}):" in out
    assert "score_vacancies vacancies/nightly/<date> escalate" in out
    assert "TELEGRAM_* never passed" in out
    assert "tok-123456-secret" not in out  # the token value never prints
    assert nr.driver_calls() == []  # dry run launches nothing


# ---------------------------------------------------------------------------
# The dated pause (config [nightly] paused_until / NIGHTLY_PAUSED_UNTIL)
# ---------------------------------------------------------------------------


def _pause(monkeypatch, nr, value):
    import settings

    monkeypatch.setattr(settings, "nightly_paused_until", lambda: value)


def test_paused_night_does_no_work_at_all(nr, monkeypatch):
    """A paused night must cost nothing: no driver, no Claude session, no night
    directory, no lock. It is a budget fuse, so 'cheap' is the whole point."""
    _pause(monkeypatch, nr, "2999-01-01")
    nr.set_steps([{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(2)}])
    assert nr.mod.run_night() == 0
    assert nr.driver_calls() == [], "a paused night still ran the driver"
    text = " ".join(nr.texts())
    assert "paused until" in text and "nothing fetched or scored" in text
    assert not (nr.mod.VACANCIES_DIR / "nightly.lock").exists()


def test_the_pause_lifts_by_itself_on_the_day(nr, monkeypatch):
    """Yesterday's date is not a pause — the rule expires without anyone
    remembering to remove it."""
    _pause(monkeypatch, nr, (datetime.now().date() - timedelta(days=1)).isoformat())
    nr.set_steps([{"exit": 0}])
    assert nr.mod.run_night() == 0
    assert nr.driver_calls(), "an expired pause still blocked the night"


def test_todays_date_runs(nr, monkeypatch):
    """paused_until is the resume day, not the last paused day."""
    _pause(monkeypatch, nr, datetime.now().date().isoformat())
    nr.set_steps([{"exit": 0}])
    assert nr.mod.run_night() == 0
    assert nr.driver_calls()


def test_a_typo_in_the_pause_date_runs_and_says_so(nr, monkeypatch):
    """A misspelt date must not pause the pipeline forever in silence."""
    _pause(monkeypatch, nr, "1st September")
    nr.set_steps([{"exit": 0}])
    assert nr.mod.run_night() == 0
    assert nr.driver_calls(), "a typo silently paused the night"
    assert any("is not a date" in t for t in nr.texts())


def test_no_pause_configured_runs(nr, monkeypatch):
    _pause(monkeypatch, nr, "")
    nr.set_steps([{"exit": 0}])
    assert nr.mod.run_night() == 0
    assert nr.driver_calls()


def test_wrapper_saves_results_while_file_only_agent_is_still_running(nr):
    nr.set_steps([{"exit": 10, "action": "score_vacancies", "payload": _vac_payload(1)}, {"exit": 0}])
    nr.set_mode("score_slow")
    assert nr.mod.run_night() == 0
    # One periodic save before the child exits, then the idempotent final sweep.
    assert nr.wrapper_log().count("save sweep:") == 2
    config = json.loads((nr.night_dir() / "session.json").read_text())
    assert config["model"]


def test_python_saver_preserves_the_actual_phase_model(nr, monkeypatch):
    import scoring_settings
    monkeypatch.setattr(scoring_settings, "screen_model", lambda: "haiku")
    monkeypatch.setattr(scoring_settings, "scoring_model", lambda: "opus")
    nr.set_steps([{"exit": 10, "action": "score_vacancies", "phase": "screen", "payload": _vac_payload(1)}, {"exit": 0}])
    assert nr.mod.run_night() == 0
    assert "--scored-by haiku" in nr.wrapper_log()
    assert "--scored-by opus" not in nr.wrapper_log()


def test_save_timeout_is_bounded_and_failed_results_remain_retryable(nr, monkeypatch):
    import settings
    night = nr.night_dir()
    night.mkdir(parents=True)
    ctx = nr.mod._Ctx(settings.nightly(), night, datetime.now() + timedelta(hours=1))
    calls = []
    def stalled(cmd, **kwargs):
        calls.append(kwargs['timeout'])
        raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])
    monkeypatch.setattr(nr.mod.subprocess, 'run', stalled)
    assert nr.mod._sweep_save(ctx, 'score_vacancies', [], timeout=0.25) is False
    assert calls == [0.25]
    assert 'TimeoutExpired' in nr.wrapper_log()


def test_save_cmd_uses_prepared_by_for_screening_with_and_without_model():
    """prepare_screening.py only accepts --prepared-by; the sweep always passes a model."""
    import nightly_run as nr

    with_model = nr._save_cmd("prepare_screening", ["a.json"], "opus")
    assert "--prepared-by" in with_model and "--scored-by" not in with_model
    assert with_model[with_model.index("--prepared-by") + 1] == "opus"
    without = nr._save_cmd("prepare_screening", ["a.json"], None)
    assert "--prepared-by" in without and "--scored-by" not in without
    scored = nr._save_cmd("score_vacancies", ["a.json"], "opus")
    assert "--scored-by" in scored and "--prepared-by" not in scored
