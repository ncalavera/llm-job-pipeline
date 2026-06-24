#!/usr/bin/env python3
"""Render one compact progress card from vacancies/run_status.json.

The /jobs-new runbook runs the long step in the background and calls this on a
short cadence; each call prints ONE fresh card line which the assistant relays to
chat. Standalone, no project imports — safe to call while a fetch is mid-flight.

Example output:
    fetch  ▕████████░░░░░░░░▏  18/40 · LinkedIn · +12 new · 6m02s
"""

import json
from datetime import datetime
from pathlib import Path

STATUS_PATH = Path(__file__).resolve().parent.parent / "vacancies" / "run_status.json"
BAR_WIDTH = 16


def _fmt_elapsed(started: str) -> str:
    try:
        delta = datetime.now() - datetime.fromisoformat(started)
        secs = int(delta.total_seconds())
    except Exception:
        return "?"
    m, s = divmod(max(secs, 0), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _bar(done: int, total: int) -> str:
    if total <= 0:
        return "▕" + "░" * BAR_WIDTH + "▏"
    filled = min(BAR_WIDTH, round(BAR_WIDTH * done / total))
    return "▕" + "█" * filled + "░" * (BAR_WIDTH - filled) + "▏"


def main() -> None:
    try:
        s = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        print("no run in progress")
        return

    stage = s.get("stage", "?")
    total = s.get("total", 0)
    done = s.get("done", 0)
    elapsed = _fmt_elapsed(s.get("started_at", ""))
    extra = s.get("extra", {}) or {}

    parts = [f"{stage:<6}", _bar(done, total), f"{done}/{total}"]

    if s.get("finished"):
        parts = [f"{stage:<6}", "✓ done", f"{total} sources"]
    elif s.get("current"):
        parts.append(f"· {s['current']}")

    if extra.get("new") is not None:
        parts.append(f"· +{extra['new']} new")
    elif extra.get("enriched") is not None:
        parts.append(f"· {extra['enriched']} enriched")

    parts.append(f"· {elapsed}")
    print(" ".join(parts))


if __name__ == "__main__":
    main()
