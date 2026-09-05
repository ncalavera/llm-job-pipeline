#!/usr/bin/env python3
"""Nightly wrapper: one command on the server runs the whole pipeline at night.

Script first, Claude only for scoring (KTD1): the deterministic stages run in
``run_daily.py --new --unattended``; this wrapper only answers the three
scoring gates the driver cannot answer alone (``screen_companies``,
``score_companies``, ``score_vacancies``), each with ONE bounded headless
Claude Code session, then resumes the driver. The driver stays the gate
machine — the wrapper never reorders stages and never writes a verdict.

Survival rules (R4, R12, R15):
  * every session is bounded by a per-gate limit AND one whole-run deadline
    (start + [nightly] run_deadline_minutes); past the deadline scoring is
    skipped and the run goes straight to the digest;
  * a stuck / wrong-turning / dead session never kills the night: the driver's
    unattended gate logic carries unscored work over to the next night;
  * a session stopped by the Claude usage limit is the one failure worth
    waiting out: the wrapper sleeps until the limit lifts (never past the
    deadline, at most RATE_LIMIT_WAITS times) and retries the SAME gate
    without spending a gate trip;
  * any failure (driver abort/error, gate-loop overrun, SIGTERM, a wrapper
    crash) sends one short Telegram alert naming the failed stage, the
    exception class and the first 200 masked characters of the message; a
    dead Claude session gets a plain sentence instead (the reason, the reset
    clock on a rate limit, and the night log to open) — never a raw
    stream-json blob on a phone screen;
  * the wrapper exits non-zero ONLY when that alert itself could not be sent.

Privacy (R17, KTD3): all night output lives in ``vacancies/nightly/<date>/``
(mode 700, pruned after seven days) — never in the system journal. Each Claude
child gets a from-scratch environment (PATH, HOME, the database URL, the
Claude token; FIRECRAWL_API_KEY only for the company gates). ``TELEGRAM_*``
never enters a Claude session.

Test seams: ``NIGHTLY_DRIVER_CMD`` / ``NIGHTLY_CLAUDE_BIN`` (shlex-split)
replace the real driver / ``claude`` binary with fakes.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PROJECT_ROOT = SCRIPTS_DIR.parent
# Tests monkeypatch this one attribute; every runtime path derives from it.
VACANCIES_DIR = PROJECT_ROOT / "vacancies"

from telegram_digest import tg_call  # noqa: E402 — the one Telegram entry point

# run_daily.py exit codes (kept as literals so a fake driver needs no import).
EXIT_DONE = 0
EXIT_GATE = 10
EXIT_ABORT = 20
EXIT_ERROR = 30

GATE_CAP = 8  # gate trips per night before the loop is declared runaway (KTD1)
PRUNE_DAYS = 7  # R17: night directories older than this are deleted
ALERT_MSG_CHARS = 200  # R12: masked message excerpt length in an alert

# Waiting out the five-hour Claude usage limit (the 2026-08-27 night lost three
# hours of budget to a limit that lifted six minutes later).
RATE_LIMIT_WAITS = 2  # waits per night: one limit, plus one that lands again
# The limit lifts on a whole-minute boundary from Anthropic's clock; ours can
# sit seconds either side of it. 45 s covers that skew and costs a fraction of
# a percent of a five-hour window — a retry one second early would waste a
# whole wait instead.
RATE_LIMIT_MARGIN_S = 45

# The three gates the wrapper answers with a headless Claude session. Every
# OTHER gate action is the driver's own unattended business (U2) — the wrapper
# just resumes. ``payload`` names the driver's gate payload file under
# vacancies/; ``save_script`` is the idempotent save entrypoint the defensive
# sweep re-runs over score_out/ after the session (the session already saves
# per wave; the sweep only catches work written but unsaved by a dead session).
# ``phases`` lists the phase values the driver can emit for the gate — the
# session gets the live one as its third argument and picks the subagent model
# tier for it (--dry-run prints one session line per phase).
GATES = {
    "screen_companies": {
        "payload": "screen_companies_payload.json",
        "save_script": "screen_candidates.py",
        "limit_key": "company_gate_minutes",
        "firecrawl": True,
        "phases": ("screen",),
    },
    "score_companies": {
        "payload": "score_companies_payload.json",
        "save_script": "score_companies.py",
        "limit_key": "company_gate_minutes",
        "firecrawl": True,
        "phases": ("score",),
    },
    "score_vacancies": {
        "payload": "score_vacancies_payload.json",
        "save_script": "score_vacancies.py",
        "limit_key": "vacancy_gate_minutes",
        "firecrawl": False,
        "phases": ("screen", "escalate"),
    },
    # Screening preparation: extraction + profile comparison per
    # vacancy, no score. Same file-in/file-out session; the strong model.
    "prepare_screening": {
        "payload": "prepare_screening_payload.json",
        "save_script": "prepare_screening.py",
        "limit_key": "vacancy_gate_minutes",
        "firecrawl": False,
        "phases": ("prepare",),
    },
}

# Environment a Claude child is allowed to inherit (KTD3). Everything else —
# TELEGRAM_* above all — is dropped. FIRECRAWL_API_KEY is added per gate.
# ANTHROPIC_API_KEY is deliberately NOT here: the night scores on Nikita's
# Claude subscription through the headless login, and Claude Code prefers an
# API key whenever it finds one — inheriting it would silently move the whole
# night's spend to per-token billing (one session billed $5.15 for 20 roles).
# Python stages that want a direct model call run under the driver, which
# inherits the full environment, so nothing else loses the key.
_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "SUPABASE_DB_URL",
    "JOBSEARCH_DB_PATH",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

# Env values that must never reach Telegram or a log line, masked by value.
_SECRET_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "SUPABASE_DB_URL",
    "SUPABASE_DIRECT_URL",
    "FIRECRAWL_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

_LOGIN_FAILURE_RE = re.compile(
    r"login|log in|not logged in|authentication|unauthori[sz]ed|401"
    r"|invalid api key|api key|oauth|credential|/login",
    re.IGNORECASE,
)

# SIGTERM handler hooks (module globals so the handler sees the live run).
# _CHILD holds whichever subprocess runs right now — a driver or a Claude
# session, never both — so the handler can terminate it.
_CURRENT: "_Ctx | None" = None
_CHILD: "subprocess.Popen | None" = None


def mask_secrets(text: str) -> str:
    """Mask secrets before anything reaches Telegram, stdout or wrapper.log:
    known secret env values, ``scheme://user:password@`` URL credentials, and
    Telegram ``bot<id>:<token>`` fragments. The same masker serves --dry-run
    and the R12 alerts (KTD5)."""
    out = str(text)
    for key in _SECRET_ENV_KEYS:
        val = os.environ.get(key)
        if val and len(val) >= 6:
            out = out.replace(val, "***")
    out = re.sub(r"(://[^/\s@:]+:)[^@\s]+@", r"\1***@", out)
    out = re.sub(r"\bbot\d+:[\w-]+", "bot***", out)
    return out


def _send(text: str) -> bool:
    """One Telegram message to the user. False (never an exception) on any
    failure, including missing TELEGRAM_* config — the caller decides whether a
    lost message fails the night (it does: exit non-zero, so systemd shows it)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        tg_call(token, "sendMessage", {"chat_id": chat, "text": mask_secrets(str(text))})
        return True
    except Exception:
        return False


def _state_path() -> Path:
    return VACANCIES_DIR / "run_state.json"


def _load_state() -> dict | None:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def _driver_cmd() -> list[str]:
    raw = os.environ.get("NIGHTLY_DRIVER_CMD")
    if raw:
        return shlex.split(raw)
    return [sys.executable, str(SCRIPTS_DIR / "run_daily.py")]


def _orchestrator_model() -> str:
    """Model tier for the night session's orchestrator — the same setting that
    picks the strong scoring model (the model tier is a setting, never a
    hardcoded name). Fallback only when the settings module itself is broken."""
    try:
        from scoring_settings import scoring_model

        return scoring_model()
    except Exception:
        return "opus"


def _claude_cmd(action: str, night_dir, cfg: dict, phase: str) -> list[str]:
    base = shlex.split(os.environ.get("NIGHTLY_CLAUDE_BIN") or "claude")
    return base + [
        "-p",
        f"/jobs-night {action} {night_dir} {phase}",
        "--model",
        _orchestrator_model(),
        "--dangerously-skip-permissions",
        # Repo settings only: the user settings.json is shared with the Mac through
        # the wiki, and a Mac-only hook there blocked every Bash call on 2026-09-03.
        "--setting-sources",
        "project,local",
        # No web tools in the night session: posting text is stranger-written,
        # and a fetch/search tool is the only way a hijacked session could send
        # anything out. Writes are fenced by .claude/hooks/night-write-fence.py.
        "--disallowed-tools",
        "WebFetch,WebSearch",
        "--max-turns",
        str(cfg["max_turns"]),
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def _claude_env(firecrawl: bool, night_dir=None) -> dict:
    env = {k: os.environ[k] for k in _CHILD_ENV_ALLOWLIST if os.environ.get(k)}
    # The interpreter the session must use for its per-wave saves. A bare
    # ``python3`` is the SYSTEM interpreter, which has none of the project
    # dependencies (psycopg2) — its saves fail and only the wrapper's
    # end-of-session sweep lands the work, so a session that dies mid-way
    # loses every finished wave. sys.executable is whatever runs the wrapper
    # (the venv under systemd), so the session saves with the same one.
    env["NIGHTLY_PYTHON"] = sys.executable
    if night_dir is not None:
        # Arms .claude/hooks/night-write-fence.py: Write/Edit only under
        # <night_dir>/score_out/ (and scoring_log.md) for this child.
        env["NIGHTLY_NIGHT_DIR"] = str(Path(night_dir).resolve())
    if firecrawl and os.environ.get("FIRECRAWL_API_KEY"):
        env["FIRECRAWL_API_KEY"] = os.environ["FIRECRAWL_API_KEY"]
    # Under systemd the token arrives as a credential FILE (LoadCredential=,
    # KTD7 — `systemctl show` would reveal an Environment= value). It is read
    # here and exported only into the Claude child, never into our own env.
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir and "CLAUDE_CODE_OAUTH_TOKEN" not in env:
        try:
            token = (Path(cred_dir) / "claude-token").read_text(encoding="utf-8").strip()
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        except OSError:
            pass
    return env


def _save_extra(action: str) -> list[str]:
    """Extra save-command flags per gate: the vacancy save records provenance
    with the night's scoring model (R16: never --archive, never a status)."""
    if action not in ("score_vacancies", "prepare_screening"):
        return []
    try:
        from scoring_settings import scoring_model

        model = scoring_model()
    except Exception:
        model = "opus"
    flag = "--prepared-by" if action == "prepare_screening" else "--scored-by"
    return [flag, model]


class _Ctx:
    """One night's runtime context: config, paths, deadline, alert accounting."""

    def __init__(self, cfg: dict, night_dir: Path, deadline: datetime):
        self.cfg = cfg
        self.night_dir = night_dir
        self.deadline = deadline
        self.items_left = int(cfg["max_items_per_night"])
        self.alert_failed = False
        self.deadline_noted = False
        # Set by a session the Claude usage limit stopped (unix second the limit
        # lifts); the gate loop reads it once and decides whether to wait.
        self.rate_limit_reset: int | None = None

    def log(self, msg: str) -> None:
        # wrapper.log lives inside the 700 night directory — never the journal.
        line = f"{datetime.now().isoformat(timespec='seconds')} {mask_secrets(str(msg))}\n"
        try:
            with open(self.night_dir / "wrapper.log", "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass

    def send(self, text: str) -> bool:
        ok = _send(text)
        if not ok:
            self.alert_failed = True
            self.log(f"TELEGRAM SEND FAILED for: {text}")
        return ok

    def alert(self, stage: str, message: str) -> None:
        """The R12 alert: stage + masked first ALERT_MSG_CHARS of the message."""
        msg = mask_secrets(str(message or "see the night logs")).strip()[:ALERT_MSG_CHARS]
        self.log(f"ALERT [{stage}] {msg}")
        self.send(f"🌙 Night run failed at {stage}: {msg}")


def _run_driver(ctx: _Ctx, flags: list[str]) -> int:
    global _CHILD
    cmd = _driver_cmd() + flags
    ctx.log(f"driver {' '.join(flags)}")
    with open(ctx.night_dir / "driver.log", "ab") as fh:
        proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdout=fh, stderr=subprocess.STDOUT)
        _CHILD = proc
        try:
            rc = proc.wait()
        finally:
            _CHILD = None
    ctx.log(f"driver exited {rc}")
    return rc


def _last_nonempty_line(path: Path) -> str | None:
    """Last non-empty line of a possibly MB-scale log: read only the final 8KB
    (driver.log spans a whole night; the stream-json transcripts are huge)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return None


def _driver_tail(ctx: _Ctx) -> str:
    """Last non-empty line of driver.log — a crash before any checkpoint write
    still names its exception class here."""
    return _last_nonempty_line(ctx.night_dir / "driver.log") or "no driver output captured"


def _transcript_tail(out_path: Path, err_path: Path) -> str:
    """Last error-ish line of a dead session's transcript (stderr first)."""
    for path in (err_path, out_path):
        line = _last_nonempty_line(path)
        if line:
            return line
    return "empty transcript"


def _tail_text(path: Path, nbytes: int = 65536) -> str:
    """Final ``nbytes`` of a possibly huge file, as text ('' when unreadable)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - nbytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


_RATE_LIMIT_RESET_RE = re.compile(r'"resetsAt"\s*:\s*(\d{9,16})')
_API_ERROR_STATUS_RE = re.compile(r'"api_error_status"\s*:\s*"?(\d{3})')


def _reset_clock(stamp: int) -> str:
    """'22:40 on 27 Aug' from a unix timestamp in seconds or milliseconds."""
    if stamp > 10**11:  # milliseconds
        stamp //= 1000
    return datetime.fromtimestamp(stamp).strftime("%H:%M on %-d %b")


def _log_ref(path: Path) -> str:
    """Short, retypeable pointer to a night log: ``nightly/<date>/<file>``.
    Kept short on purpose — the whole alert must fit ALERT_MSG_CHARS."""
    return f"nightly/{path.parent.name}/{path.name}"


def _session_failure_message(out_path: Path, err_path: Path, rc: "int | None") -> str:
    """One plain sentence about a session that exited non-zero — never a raw
    JSON blob. The stream-json transcript is machine output; a phone needs the
    reason, the clock and where to look:

      * HTTP 429 in the transcript -> the rate limit, and when it resets
        (the ``resetsAt`` unix stamp of the rate_limit_event line);
      * a login/auth failure -> named as such, with what to do;
      * anything else -> the exit code plus the transcript path.
    """
    tail = _tail_text(out_path) + "\n" + _tail_text(err_path)
    where = _log_ref(out_path)
    status = _API_ERROR_STATUS_RE.search(tail)
    if (status and status.group(1) == "429") or "rate_limit" in tail:
        reset = _rate_limit_reset(out_path, err_path)
        when = f", resets {_reset_clock(reset)}" if reset else ""
        return (
            f"Claude usage limit reached (HTTP 429) — scoring stopped{when}. "
            f"Unscored roles carry over. Log: {where}"
        )
    if status:
        return (
            f"the Claude API answered HTTP {status.group(1)} — the session stopped. "
            f"Unscored roles carry over. Log: {where}"
        )
    if _LOGIN_FAILURE_RE.search(_transcript_tail(out_path, err_path)):
        return (
            "Claude login failure — sign in on the server again. "
            f"Unscored roles carry over. Log: {where}"
        )
    return f"the session stopped early (exit {rc}). Unscored roles carry over. Log: {where}"


def _rate_limit_reset(out_path: Path, err_path: Path) -> int | None:
    """The unix second at which the Claude usage limit lifts, read from the
    session transcript — or None when the session died of something else.

    The transcript carries a ``rate_limit_event`` line per turn: ``allowed_warning``
    while utilization climbs, then ``rejected`` when the limit actually bites.
    Both carry a ``resetsAt``; the rejected one is the event that stopped this
    session, so it wins. The session's result line carries ``api_error_status``
    429 — either that or a rejected event is enough to call it a usage limit.
    One parser serves the wait decision and the alert clock."""
    tail = _tail_text(out_path) + "\n" + _tail_text(err_path)
    rejected: int | None = None
    warned: int | None = None
    for line in tail.splitlines():
        match = _RATE_LIMIT_RESET_RE.search(line)
        if not match:
            continue
        stamp = int(match.group(1))
        if stamp > 10**11:  # a millisecond stamp
            stamp //= 1000
        if '"rejected"' in line:
            rejected = stamp
        else:
            warned = stamp  # the last warning holds the live window
    status = _API_ERROR_STATUS_RE.search(tail)
    if rejected is None and not (status and status.group(1) == "429"):
        return None
    return rejected if rejected is not None else warned


def _sleep(seconds: float) -> None:
    """The one sleep in the wrapper (tests replace it). A SIGTERM lands during
    it: Python runs the handler, the handler raises SystemExit, and it leaves
    through here — nothing between the sleep and run_night swallows it (R4)."""
    if seconds > 0:
        time.sleep(seconds)


def _wait_out_rate_limit(ctx: _Ctx, reset_at: int, waits_used: int) -> bool:
    """Sleep until the Claude usage limit lifts so the SAME gate can be retried;
    True when the night waited. False — with one plain alert — when it must not:
    the limit lifts after tonight's deadline, or the night has already waited
    RATE_LIMIT_WAITS times (a stale reset stamp would otherwise spin the loop).
    On False the work carries over exactly as it does today."""
    resume_at = datetime.fromtimestamp(reset_at + RATE_LIMIT_MARGIN_S)
    if waits_used >= RATE_LIMIT_WAITS:
        ctx.log(
            f"usage limit hit again after {waits_used} waits tonight — not waiting; "
            "the rest carries over to the next night"
        )
        ctx.send(
            "🌙 Night run: "
            + (
                f"the Claude usage limit is spent again after {waits_used} waits tonight "
                "— scoring stops here; the unscored roles carry over to the next night."
            )[:ALERT_MSG_CHARS]
        )
        return False
    if resume_at >= ctx.deadline:
        ctx.log(
            f"usage limit lifts {resume_at.isoformat(timespec='seconds')}, after the "
            f"deadline {ctx.deadline.isoformat(timespec='seconds')} — not waiting; carried over"
        )
        ctx.send(
            "🌙 Night run: "
            + (
                f"the Claude usage limit is spent and only lifts at {_reset_clock(reset_at)}, "
                "after tonight's deadline — scoring stops here; the unscored roles carry "
                "over to the next night."
            )[:ALERT_MSG_CHARS]
        )
        return False
    started = datetime.now()
    seconds = max(0.0, (resume_at - started).total_seconds())
    ctx.log(
        f"usage limit wait {waits_used + 1}/{RATE_LIMIT_WAITS}: sleeping {int(seconds)}s "
        f"from {started.isoformat(timespec='seconds')}; limit lifts "
        f"{datetime.fromtimestamp(reset_at).isoformat(timespec='seconds')}; scoring resumes "
        f"{resume_at.isoformat(timespec='seconds')}"
    )
    ctx.send(
        "🌙 Night run: "
        + (
            "scoring paused — the Claude usage limit is spent. It starts again at "
            f"{_reset_clock(reset_at + RATE_LIMIT_MARGIN_S)} with the same roles; "
            "nothing is lost."
        )[:ALERT_MSG_CHARS]
    )
    _sleep(seconds)
    ctx.log(
        f"usage limit wait over at {datetime.now().isoformat(timespec='seconds')} — "
        "retrying the same gate"
    )
    return True


def _failed_stage_from_state(state: dict | None) -> tuple[str, str | None]:
    """(stage, note) of the first errored/aborted stage in the checkpoint, or
    ("unknown stage", None) when the driver died before recording one."""
    if not state:
        return "unknown stage", None
    for s in state.get("stages", []) or []:
        if s.get("status") in ("error", "aborted"):
            return s.get("name") or "?", s.get("note") or "no note recorded"
    return "unknown stage", None


def _write_no_progress() -> None:
    """Top-level ``no_progress: true`` in the checkpoint — the digest's AE7
    header line reads it. The driver preserves unknown top-level keys on save."""
    state = _load_state()
    if state is None:
        return
    state["no_progress"] = True
    tmp = _state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_state_path())


def _split_results(out_files: list[Path]) -> tuple[list[Path], list[Path]]:
    """Split score_out files into (ok, failed). A night-scorer that cannot
    score writes a valid JSON file with a ``"failed"`` field and no score — the
    save script saves nothing for it, so it must not count as progress. A
    malformed file counts as failed too."""
    ok: list[Path] = []
    failed: list[Path] = []
    for f in out_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            failed.append(f)
            continue
        if isinstance(data, dict) and "failed" not in data:
            ok.append(f)
        else:
            failed.append(f)
    return ok, failed


def _read_payload_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _make_night_dir() -> Path:
    night = VACANCIES_DIR / "nightly" / date.today().isoformat()
    night.mkdir(parents=True, exist_ok=True)
    os.chmod(night, 0o700)  # R17: private to the user
    for sub in ("score_in", "score_out"):
        (night / sub).mkdir(exist_ok=True)
    return night


def _prune_old_nights(ctx: _Ctx) -> None:
    base = VACANCIES_DIR / "nightly"
    cutoff = date.today() - timedelta(days=PRUNE_DAYS)
    try:
        children = list(base.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or child == ctx.night_dir:
            continue
        try:
            day = date.fromisoformat(child.name)
        except ValueError:
            continue  # not a night directory — never delete what we didn't make
        if day < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            ctx.log(f"pruned night directory {child.name} (seven-day retention, R17)")


def _save_cmd(action: str, files: list[str]) -> list[str]:
    """The idempotent save command for a gate — one builder for the real sweep
    and --dry-run, so the printed command can never drift from the real one."""
    spec = GATES[action]
    return (
        [sys.executable, str(SCRIPTS_DIR / spec["save_script"]), "--save"]
        + _save_extra(action)
        + ["--files"]
        + files
    )


def _sweep_save(ctx: _Ctx, action: str, out_files: list[Path]) -> None:
    """Defensive re-save of every score_out file. The session saves per wave;
    this sweep only catches results written by a session that died before its
    own --save. Saves are idempotent, and --files names and skips a malformed
    file so the rest still land (BUG-5). Best-effort: a sweep failure is
    logged, never fatal — the driver re-prompts for whatever is missing."""
    cmd = _save_cmd(action, [str(f) for f in out_files])
    ctx.log("save sweep: " + " ".join(cmd))
    try:
        res = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        tail = (res.stdout or res.stderr or "").strip()[-300:]
        ctx.log(f"save sweep exited {res.returncode}: {tail}")
    except Exception as exc:
        ctx.log(f"save sweep failed: {type(exc).__name__}: {exc}")


def _run_session(ctx: _Ctx, action: str, phase: str) -> None:
    """One headless Claude session for one gate: split the payload into
    score_in/, run the bounded session, sweep score_out/ into the DB, classify
    the outcome (no-progress / shortfall / timeout / early exit). The session
    never resumes the driver — the gate loop owns the one resume per gate; a
    session that resumes anyway is misbehavior the loop tolerates (the extra
    resume just re-emits the same gate with the remaining subset)."""
    global _CHILD
    spec = GATES[action]
    payload = _read_payload_list(VACANCIES_DIR / spec["payload"])
    items = payload[: max(ctx.items_left, 0)]
    if not items:
        ctx.log(
            f"{action}: nothing to dispatch (payload empty or the "
            f"{ctx.cfg['max_items_per_night']}-item night cap is spent) — carried over"
        )
        return
    ctx.items_left -= len(items)

    score_in = ctx.night_dir / "score_in"
    score_out = ctx.night_dir / "score_out"
    for d in (score_in, score_out):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
    for i, item in enumerate(items):
        (score_in / f"{i:03d}.json").write_text(
            json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    budget = min(
        float(ctx.cfg[spec["limit_key"]]) * 60.0,
        (ctx.deadline - datetime.now()).total_seconds(),
    )
    budget = max(budget, 1.0)
    cmd = _claude_cmd(action, ctx.night_dir, ctx.cfg, phase)
    ctx.log(f"claude session for {action} ({phase}): {len(items)} item(s), budget {int(budget)}s")
    ctx.log("claude cmd: " + " ".join(cmd))

    out_path = ctx.night_dir / f"claude-{action}.jsonl"
    err_path = ctx.night_dir / f"claude-{action}.err"
    timed_out = False
    rc: int | None = None
    started = time.monotonic()
    with open(out_path, "ab") as out_fh, open(err_path, "ab") as err_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=_claude_env(spec["firecrawl"], ctx.night_dir),
            stdout=out_fh,
            stderr=err_fh,
        )
        _CHILD = proc
        try:
            rc = proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        finally:
            _CHILD = None
    elapsed = int(time.monotonic() - started)

    out_files = sorted(score_out.glob("*.json"))
    if out_files:
        _sweep_save(ctx, action, out_files)

    # Only ok results count as progress: a "failed"-field file saves nothing.
    ok_files, failed_files = _split_results(out_files)
    n_in, n_out = len(items), len(ok_files)
    if failed_files:
        ctx.log(f"{action}: {len(failed_files)} result file(s) failed or malformed")
    if timed_out:
        ctx.log(
            f"{action}: session killed at its {int(budget)}s limit — {n_out}/{n_in} "
            "result file(s) present; the rest carries over to the next night"
        )
    elif rc != 0 and (reset_at := _rate_limit_reset(out_path, err_path)) is not None:
        # Not a failure to alert about here: the gate loop waits the limit out
        # and retries this same gate, and it sends the one message about it.
        # The night item cap counts work Claude actually did, so give back the
        # roles the limit cut off — otherwise the retry finds the cap spent.
        ctx.items_left += n_in - n_out
        ctx.rate_limit_reset = reset_at
        ctx.log(
            f"{action}: the Claude usage limit stopped the session after {elapsed}s — "
            f"{n_out}/{n_in} scored; the limit lifts {_reset_clock(reset_at)}"
        )
    elif rc != 0:
        # The transcript itself stays in the night log; the alert carries one
        # readable sentence (R12) — never a truncated stream-json blob.
        ctx.alert(action, _session_failure_message(out_path, err_path, rc))
        tail = _transcript_tail(out_path, err_path)
        ctx.log(f"{action}: session exited {rc} after {elapsed}s — {tail}")
    elif n_out == 0:
        # The digest's "no progress" header line is about vacancy scoring only;
        # a stalled company gate keeps its alert + carry-over without it.
        if action == "score_vacancies":
            _write_no_progress()
        ctx.log(f"{action}: no-progress — clean exit, nothing saved; carried over")
        ctx.alert(
            action,
            f"scoring session made no progress (0 of {n_in} saved) — "
            "carried over to the next night",
        )
    elif n_out < n_in:
        ctx.log(f"{action}: {n_in - n_out} of {n_in} item(s) unscored — carried over")
    else:
        ctx.log(f"{action}: {n_out}/{n_in} result file(s) written")


def _gate_loop(ctx: _Ctx) -> None:
    """KTD1: start the driver, then answer gates until it exits with anything
    but 10, the trip cap fires, or the deadline forces a straight run-out."""
    rc = _run_driver(ctx, ["--new", "--unattended"])
    trips = 0
    waits = 0
    retry_gate: str | None = None  # the gate a usage-limit wait must re-run
    while rc == EXIT_GATE:
        state = _load_state()
        gate = (state or {}).get("gate") or {}
        action = gate.get("action")
        free = retry_gate is not None and action == retry_gate
        retry_gate = None
        if free:
            # A retry after a usage-limit wait rides the trip it already paid
            # for: a night that hits the limit twice must not spend its gate
            # budget on waiting instead of on scoring.
            ctx.log(f"gate trip {trips}/{GATE_CAP} again: {action} (after the usage-limit wait)")
        else:
            trips += 1
            if trips > GATE_CAP:
                ctx.alert(
                    "gate loop",
                    f"exceeded the cap of {GATE_CAP} gate sessions in one night — "
                    "stopping; unscored work waits for the next night",
                )
                return
            ctx.log(f"gate trip {trips}/{GATE_CAP}: {action}")
        if action not in GATES:
            ctx.log(f"gate '{action}' needs no Claude session — the driver answers it")
        elif datetime.now() < ctx.deadline:
            _run_session(ctx, action, gate.get("phase") or "score")
            reset_at, ctx.rate_limit_reset = ctx.rate_limit_reset, None
            if reset_at is not None and _wait_out_rate_limit(ctx, reset_at, waits):
                waits += 1
                retry_gate = action
        elif not ctx.deadline_noted:
            ctx.deadline_noted = True
            ctx.log("run deadline reached — scoring skipped, carried over")
            ctx.send(
                "🌙 Night run: the run deadline was reached — scoring skipped; "
                "unscored roles carry over to the next night. The digest still goes out."
            )
        rc = _run_driver(ctx, ["--resume"])

    if rc == EXIT_DONE:
        # The digest stage fails soft inside the driver (error_continue keeps
        # the run alive through publish) — surface that error here, or the
        # night ends silent with no morning message and no alert.
        for s in (_load_state() or {}).get("stages", []) or []:
            if s.get("name") == "digest" and s.get("status") == "error":
                ctx.alert("digest", s.get("note") or "digest stage errored")
                break
        ctx.log("driver finished — the digest and publish ran as driver stages")
        return
    stage, note = _failed_stage_from_state(_load_state())
    if note is None:
        note = _driver_tail(ctx)
    ctx.alert(stage, note)


def _handle_sigterm(signum, frame):
    """systemd stop / unit timeout: kill the session, send the R12 alert, die.
    Exit 0 when the alert went out (the only wrapper failure is a lost alert)."""
    child = _CHILD
    if child is not None:
        try:
            child.terminate()
        except Exception:
            pass
    ctx = _CURRENT
    ok = True
    if ctx is not None:
        ctx.alert(
            "wrapper",
            "killed by SIGTERM (systemd stop or unit timeout) — unscored work carries over",
        )
        ok = not ctx.alert_failed
    raise SystemExit(0 if ok else 1)


def run_night() -> int:
    global _CURRENT
    # Startup (settings, lock, night dir) runs before the ctx exists, so a
    # crash here has no ctx.alert — it still must send the one R12 message.
    try:
        import settings

        cfg = settings.nightly()

        # A dated pause beats everything below: no lock, no night dir, no fetch,
        # no scoring, no digest. It runs before the lock on purpose — a paused
        # night must cost nothing at all, and Persistent=true means a missed
        # night fires a catch-up run at the next boot, which this also stops.
        paused_until = settings.nightly_paused_until()
        if paused_until:
            try:
                resume_on = datetime.strptime(paused_until, "%Y-%m-%d").date()
            except ValueError:
                # A typo must not silently pause forever, nor silently run.
                _send(
                    f"🌙 Night run: the pause date '{paused_until[:20]}' is not a "
                    "date (YYYY-MM-DD) — running tonight as usual."
                )
                resume_on = None
            if resume_on and datetime.now().date() < resume_on:
                ok = _send(
                    f"🌙 Night run paused until {resume_on.strftime('%-d %b')} — "
                    "nothing fetched or scored tonight."
                )
                return 0 if ok else 1

        # A parked MANUAL run is the user's evening work — never discard it (KTD1).
        state = _load_state()
        if (
            state
            and not state.get("finished")
            and not (state.get("options") or {}).get("unattended")
        ):
            ok = _send(
                "🌙 Night run skipped — a manual run is parked at a gate. Finish it "
                "(--resume) or discard it (--new); the next night will run normally."
            )
            return 0 if ok else 1

        # One night at a time. The driver replaces its state file on every save, so
        # the lock lives on its own dedicated file (KTD1).
        lock_path = VACANCIES_DIR / "nightly.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = open(lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fh.close()
            ok = _send("🌙 Night run skipped — another nightly run still holds the lock.")
            return 0 if ok else 1

        night = _make_night_dir()
        deadline = datetime.now() + timedelta(minutes=float(cfg["run_deadline_minutes"]))
        ctx = _Ctx(cfg, night, deadline)
    except Exception as exc:
        msg = mask_secrets(f"{type(exc).__name__}: {exc}").strip()[:ALERT_MSG_CHARS]
        ok = _send(f"🌙 Night run failed at wrapper startup: {msg}")
        return 0 if ok else 1
    _CURRENT = ctx
    prev_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        ctx.log(f"night run started — deadline {deadline.isoformat(timespec='seconds')}")
        _prune_old_nights(ctx)
        _gate_loop(ctx)
    except SystemExit:
        raise
    except Exception as exc:
        ctx.alert("wrapper", f"{type(exc).__name__}: {exc}")
    finally:
        _CURRENT = None
        signal.signal(signal.SIGTERM, prev_handler)
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()
    ctx.log(f"night run finished — alert_failed={ctx.alert_failed}")
    return 1 if ctx.alert_failed else 0


def _print_dry_run() -> None:
    import settings

    cfg = settings.nightly()
    night = "vacancies/nightly/<date>"
    print("Nightly wrapper — gate dispatch table (--dry-run: nothing runs):")
    print(
        "  driver:  "
        + mask_secrets(" ".join(_driver_cmd() + ["--new", "--unattended"]))
        + "  (then --resume after each gate)"
    )
    print(
        f"  bounds:  {GATE_CAP} gate sessions, {cfg['max_items_per_night']} items/night, "
        f"deadline start + {cfg['run_deadline_minutes']:g} min, "
        f"{RATE_LIMIT_WAITS} usage-limit waits (+{RATE_LIMIT_MARGIN_S}s margin)"
    )
    for action, spec in GATES.items():
        minutes = cfg[spec["limit_key"]]
        save = " ".join(_save_cmd(action, [f"{night}/score_out/NNN.json ..."]))
        env_keys = list(_claude_env(spec["firecrawl"]))
        print(f"  {action}:")
        print(f"    payload: vacancies/{spec['payload']} → {night}/score_in/NNN.json")
        for phase in spec["phases"]:
            session = " ".join(_claude_cmd(action, night, cfg, phase))
            print(f"    session ({phase}): {mask_secrets(session)}  [limit {minutes:g} min]")
        print(f"    save:    {mask_secrets(save)}")
        print(f"    env:     {', '.join(env_keys) or '(empty)'} — TELEGRAM_* never passed")
    print("  every other gate: the driver's own unattended answer (no session)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Nightly systemd wrapper for run_daily.py — gates answered headlessly."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the gate dispatch table and the exact (masked) commands; run nothing.",
    )
    args = p.parse_args(argv)
    if args.dry_run:
        _print_dry_run()
        return 0
    return run_night()


if __name__ == "__main__":
    sys.exit(main())
