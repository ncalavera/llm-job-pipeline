#!/usr/bin/env python3
"""PreToolUse fence for the unattended night session.

Active only when NIGHTLY_NIGHT_DIR is set (scripts/nightly_run.py exports it
into the headless Claude child). Then every Write/Edit must land inside
<night_dir>/score_out/ or on <night_dir>/scoring_log.md — the only files the
session and its night-scorer subagents are meant to produce. Any other path
(the checkout, .claude/, the secrets dir) is refused with exit 2, so a job
posting that tricks a scorer cannot plant code for the next night.
Interactive sessions never set the variable, so the hook is a no-op there.
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    night = os.environ.get("NIGHTLY_NIGHT_DIR")
    if not night:
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("night-write-fence: unreadable hook input", file=sys.stderr)
        return 2
    tool_input = data.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw:
        print("night-write-fence: no file path in tool input", file=sys.stderr)
        return 2
    night_dir = Path(night).resolve()
    target = Path(raw)
    if not target.is_absolute():
        target = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()) / target
    target = target.resolve()
    allowed_dir = night_dir / "score_out"
    allowed_file = night_dir / "scoring_log.md"
    if target == allowed_file or allowed_dir in target.parents:
        return 0
    print(
        f"night-write-fence: refused write to {target}; only {allowed_dir}/ "
        f"and {allowed_file} are writable during the night run",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
