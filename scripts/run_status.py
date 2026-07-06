"""Lightweight run-status heartbeat for long pipeline stages.

Long steps (fetch, enrich) update ``vacancies/run_status.json`` after every unit
of work. The ``/jobs-new`` runbook runs these steps in the BACKGROUND and polls
this file to reprint a compact progress card in chat — because a foreground
command's stdout is invisible until it exits, the heartbeat file is the only way
to see progress while a long fetch is still running.

All writers swallow their own errors: a failed heartbeat must NEVER break the
real job. ``vacancies/`` is gitignored, so this is pure runtime state.

Render the file with ``scripts/run_card.py``.
"""

import json
import os
from datetime import datetime
from pathlib import Path

STATUS_PATH = Path(__file__).resolve().parent.parent / "vacancies" / "run_status.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _run_id() -> str | None:
    """The current run's id, stamped by run_daily via the ``JOBS_RUN_ID`` env var.

    Every heartbeat carries it so run_card.py can BIND the card to the CURRENT
    run and refuse to render a prior run's leftover ``✓ done`` (the stale-card bug):
    a heartbeat whose run_id differs from the live run_state is stale, not live.
    ``None`` when a writer runs outside a driver-launched run (e.g. a unit test).
    """
    return os.environ.get("JOBS_RUN_ID") or None


def mark(stage: str, note: str | None = None, total: int = 0) -> None:
    """Stamp the ACTIVE stage at its start — a fresh heartbeat for THIS run.

    The driver calls this as each stage begins, so the live card shows the right
    stage (with a sane, freshly-reset elapsed) even for stages that have no
    heartbeat of their own, and so a previous run's finished heartbeat can never
    linger as the visible state (the stale-card bug). Long stages (fetch, enrich) then
    overwrite it with their own ``begin`` / ``step`` detail."""
    _write(
        {
            "stage": stage,
            "total": total,
            "done": 0,
            "current": note,
            "started_at": _now(),
            "updated_at": _now(),
            "finished": False,
            "extra": {},
        }
    )


def begin(stage: str, total: int) -> None:
    """Start a stage with a known number of work units (sources, vacancies …)."""
    _write(
        {
            "stage": stage,
            "total": total,
            "done": 0,
            "current": None,
            "started_at": _now(),
            "updated_at": _now(),
            "finished": False,
            "extra": {},
        }
    )


def step(current, done: int, **extra) -> None:
    """Mark the in-flight unit. ``done`` = units already FINISHED before this one."""
    s = _read()
    s["current"] = current
    s["done"] = done
    s["updated_at"] = _now()
    if extra:
        s.setdefault("extra", {}).update(extra)
    _write(s)


def finish(**extra) -> None:
    s = _read()
    s["finished"] = True
    s["current"] = None
    s["done"] = s.get("total", s.get("done", 0))
    s["updated_at"] = _now()
    if extra:
        s.setdefault("extra", {}).update(extra)
    _write(s)


def _read() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(s: dict) -> None:
    try:
        s["run_id"] = _run_id()  # bind every heartbeat to the current run
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_PATH)  # atomic — poller never reads a half-written file
    except Exception:
        pass  # a broken heartbeat must never abort the real job
