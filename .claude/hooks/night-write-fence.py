#!/usr/bin/env python3
"""PreToolUse fence for the unattended night session.

Active only when NIGHTLY_NIGHT_DIR is set (scripts/nightly_run.py exports it
into the headless Claude child). Then reads are confined to payload/results, shell/unknown tools are denied,
and every Write/Edit must land inside
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
    if not isinstance(data, dict) or not isinstance(data.get("tool_input"), dict):
        print("night-write-fence: malformed hook input", file=sys.stderr)
        return 2
    tool = data.get("tool_name", "")
    tool_input = data["tool_input"]
    if tool in ("Agent", "Task"):
        allowed = {"description", "prompt", "subagent_type", "model", "run_in_background", "name", "max_turns"}
        if tool_input.get("subagent_type") == "night-scorer" and not tool_input.keys() - allowed:
            return 0
        print("night-write-fence: only night-scorer may be spawned", file=sys.stderr)
        return 2
    if tool in ("TaskOutput", "TaskStop"):
        return 0
    if not isinstance(tool, str) or tool not in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob"):
        print("night-write-fence: tool unavailable in scoring session", file=sys.stderr)
        return 2
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if tool == "Glob":
        raw = tool_input.get("path")
        pattern = tool_input.get("pattern", "")
        if not isinstance(pattern, str) or "/" in pattern or ".." in pattern:
            print("night-write-fence: glob must stay in one payload directory", file=sys.stderr)
            return 2
    if not isinstance(raw, str) or not raw:
        print("night-write-fence: no file path in tool input", file=sys.stderr)
        return 2
    night_dir = Path(night).resolve()
    target = Path(raw)
    if not target.is_absolute():
        target = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()) / target
    target = target.resolve()
    allowed_dir = night_dir / "score_out"
    allowed_file = night_dir / "scoring_log.md"
    inputs = night_dir / "score_in"
    if tool == "Glob":
        if target in (inputs, allowed_dir):
            return 0
    elif tool == "Read":
        if target in (allowed_file, night_dir / "session.json") or inputs in target.parents or allowed_dir in target.parents:
            return 0
    elif target == allowed_file or allowed_dir in target.parents:
        return 0
    print(
        f"night-write-fence: refused write to {target}; only {allowed_dir}/ "
        f"and {allowed_file} are writable during the night run",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
