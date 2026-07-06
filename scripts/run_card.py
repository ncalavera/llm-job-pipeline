#!/usr/bin/env python3
"""Render one compact progress card for the CURRENT run.

The /jobs-new runbook runs the long step in the background and calls this on a
short cadence; each call prints ONE fresh card line which the assistant relays to
chat. Standalone, no project imports — safe to call while a fetch is mid-flight.

Two files feed the card, and the card BINDS them by run id (DHA-438 BUG-1):

* ``run_state.json`` — the driver's authoritative stage board (which run, which
  stage, finished or not). It carries the current ``run_id``.
* ``run_status.json`` — the live heartbeat a long stage writes as it works. It is
  stamped with the run id of whoever wrote it.

If the heartbeat's run id does NOT match the live run's, it is a leftover from a
PRIOR run — the card refuses to render its ``✓ done`` / nonsensical elapsed and
falls back to the driver's own stage board instead. This is the fix for the bug
where a mid-fetch card showed ``fetch ✓ done … 6829m23s`` from days earlier.

Example output:
    fetch  ▕████████░░░░░░░░▏  18/85 · LinkedIn · +12 new total · 6m02s
    fetch  … starting · 3s                       (heartbeat not written yet)
    company_scoring  ⏸ paused at gate · 1m10s    (waiting on your judgment)
"""

import json
from datetime import datetime
from pathlib import Path

VAC_DIR = Path(__file__).resolve().parent.parent / "vacancies"
STATUS_PATH = VAC_DIR / "run_status.json"
STATE_PATH = VAC_DIR / "run_state.json"
BAR_WIDTH = 16
_DONE_STATUSES = {"done", "skipped"}


def _fmt_elapsed(started: str) -> str:
    try:
        delta = datetime.now() - datetime.fromisoformat(started)
        secs = int(delta.total_seconds())
    except Exception:
        return "?"
    if secs < 0:
        return "?"
    m, s = divmod(secs, 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _bar(done: int, total: int) -> str:
    if total <= 0:
        return "▕" + "░" * BAR_WIDTH + "▏"
    filled = min(BAR_WIDTH, round(BAR_WIDTH * done / total))
    return "▕" + "█" * filled + "░" * (BAR_WIDTH - filled) + "▏"


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _active_stage(state: dict) -> dict | None:
    """The stage the driver is on now: the first one running, gated, or errored.

    "error" counts as active: it is where the run STOPPED, and hiding it would
    make a crashed run read as "no run in progress". At most one stage can be in
    any of these states — an error/gate halts the driver."""
    for s in state.get("stages", []):
        if s.get("status") in ("running", "blocked_gate", "error"):
            return s
    return None


def _live_card(s: dict) -> str:
    """Render the heartbeat detail (bar, counts, elapsed) for a bound run."""
    stage = s.get("stage", "?")
    total = s.get("total", 0)
    done = s.get("done", 0)
    elapsed = _fmt_elapsed(s.get("started_at", ""))
    extra = s.get("extra", {}) or {}

    if s.get("finished"):
        parts = [f"{stage:<6}", "✓ done", f"{total} sources"]
    else:
        parts = [f"{stage:<6}", _bar(done, total), f"{done}/{total}"]
        if s.get("current"):
            parts.append(f"· now: {s['current']}")

    if extra.get("new") is not None:
        parts.append(f"· +{extra['new']} new total")
    elif extra.get("enriched") is not None:
        parts.append(f"· {extra['enriched']} enriched")

    parts.append(f"· {elapsed}")
    return " ".join(parts)


def _driver_card(state: dict) -> str | None:
    """Fallback card straight from the stage board — used when no live heartbeat
    is bound to this run yet (stage just started) or the only heartbeat on disk
    is stale (belongs to a prior run). Never shows a prior run's ``done``."""
    active = _active_stage(state)
    if not active:
        return None
    stage = active.get("name", "?")
    elapsed = _fmt_elapsed(active.get("started_at", ""))
    if active.get("status") == "blocked_gate":
        return f"{stage:<6} ⏸ paused at gate · {elapsed}"
    if active.get("status") == "error":
        return f"{stage:<6} ✗ error · {elapsed}"
    return f"{stage:<6} … starting · {elapsed}"


def render() -> str:
    state = _load(STATE_PATH)
    status = _load(STATUS_PATH)

    if not state and not status:
        return "no run in progress"

    run_id = (state or {}).get("run_id")
    bound = bool(status) and status.get("run_id") is not None and status.get("run_id") == run_id

    if state and state.get("finished"):
        return f"run {run_id} · ✓ complete"

    # A heartbeat is only trustworthy when it belongs to THIS run. Otherwise it
    # is a leftover from a previous run — ignore it and show the driver's board.
    if bound:
        # If the driver has moved on to a gate — or a stage crashed — the stage
        # board is more current than the stage's own heartbeat; prefer it.
        active = _active_stage(state) if state else None
        if active and (
            active.get("status") == "error"
            or (status.get("finished") and active.get("status") == "blocked_gate")
        ):
            return _driver_card(state)
        return _live_card(status)

    if state:
        card = _driver_card(state)
        if card:
            return card
    return "no run in progress"


def main() -> None:
    print(render())


if __name__ == "__main__":
    main()
