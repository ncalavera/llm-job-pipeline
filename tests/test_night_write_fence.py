"""The night write fence (.claude/hooks/night-write-fence.py) refuses every
Write/Edit outside <night_dir>/score_out/ while NIGHTLY_NIGHT_DIR is set, and
stays silent for interactive sessions (variable unset)."""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK = PROJECT_ROOT / ".claude" / "hooks" / "night-write-fence.py"


def _run(tool_input, night_dir=None, tool="Write"):
    env = {k: v for k, v in os.environ.items() if k != "NIGHTLY_NIGHT_DIR"}
    if night_dir is not None:
        env["NIGHTLY_NIGHT_DIR"] = str(night_dir)
    env["CLAUDE_PROJECT_DIR"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
        capture_output=True,
        text=True,
        env=env,
    )


def test_hook_is_registered_for_write_and_edit():
    settings = json.loads((PROJECT_ROOT / ".claude" / "settings.json").read_text())
    entries = settings["hooks"]["PreToolUse"]
    assert any(e["matcher"] == ".*" for e in entries)
    assert any("night-write-fence.py" in h["command"] for e in entries for h in e["hooks"])


def test_no_op_when_not_a_night_run(tmp_path):
    assert _run({"file_path": str(tmp_path / "anything.py")}).returncode == 0


def test_allows_score_out_and_scoring_log(tmp_path):
    night = tmp_path / "2026-08-27"
    assert _run({"file_path": str(night / "score_out" / "007.json")}, night).returncode == 0
    assert _run({"file_path": str(night / "scoring_log.md")}, night).returncode == 0


def test_refuses_checkout_secrets_and_night_dir_root(tmp_path):
    night = tmp_path / "2026-08-27"
    for bad in (
        PROJECT_ROOT / "scripts" / "nightly_run.py",
        PROJECT_ROOT / ".claude" / "commands" / "jobs-night.md",
        tmp_path / ".env",
        night / "score_in" / "001.json",
        night / "score_out" / ".." / "wrapper.log",
        "relative/path.txt",
    ):
        r = _run({"file_path": str(bad)}, night)
        assert r.returncode == 2, bad
        assert "refused" in r.stderr


def test_refuses_when_path_missing(tmp_path):
    assert _run({}, tmp_path).returncode == 2


def test_night_session_env_has_paths_without_database_credentials(tmp_path):
    """Only Python performs persistence; the model receives no DB connection."""
    import sys

    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import nightly_run

    env = nightly_run._claude_env(False, tmp_path)
    assert "NIGHTLY_PYTHON" not in env
    assert "SUPABASE_DB_URL" not in env
    assert env["NIGHTLY_NIGHT_DIR"] == str(tmp_path.resolve())


def test_rejects_shell_secret_reads_and_other_agents(tmp_path):
    assert _run({"command": "id"}, tmp_path, "Bash").returncode == 2
    assert _run({"file_path": str(Path.home() / ".ssh/id_ed25519")}, tmp_path, "Read").returncode == 2
    assert _run({"subagent_type": "general-purpose"}, tmp_path, "Agent").returncode == 2
    assert _run({"subagent_type": "night-scorer"}, tmp_path, "Agent").returncode == 0
    assert _run({"path": str(tmp_path / "score_in"), "pattern": "../../*"}, tmp_path, "Glob").returncode == 2
    assert _run({"file_path": str(tmp_path / "score_in/001.json")}, tmp_path, "Read").returncode == 0
    assert _run({"path": str(tmp_path / "score_in"), "pattern": "*.json"}, tmp_path, "Glob").returncode == 0


def test_rejects_symlink_to_secret(tmp_path):
    inputs = tmp_path / "score_in"
    inputs.mkdir()
    link = inputs / "001.json"
    link.symlink_to(tmp_path.parent / "outside-secret")
    assert _run({"file_path": str(link)}, tmp_path, "Read").returncode == 2


def test_rejects_agent_context_overrides_and_malformed_hook_input(tmp_path):
    for field, value in [('isolation', 'worktree'), ('resume', 'other-session'), ('cwd', '/'), ('permission_mode', 'bypassPermissions')]:
        assert _run({'subagent_type': 'night-scorer', field: value}, tmp_path, 'Agent').returncode == 2
    for payload in [[], {'tool_name': 'Write', 'tool_input': 'bad'}]:
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), text=True, capture_output=True,
            env={**os.environ, 'NIGHTLY_NIGHT_DIR': str(tmp_path)})
        assert result.returncode == 2
