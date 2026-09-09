import json
import sys
import time
from pathlib import Path

S = str(Path(__file__).resolve().parent.parent / "scripts")
if S not in sys.path:
    sys.path.insert(0, S)
import discovery_runner as runner
import prepare_discovery as d


def test_runner_fake_codex_retries_and_isolates_env(tmp_path, monkeypatch, capsys):
    counter = tmp_path / "counter"
    argv_log = tmp_path / "argv.json"
    env_log = tmp_path / "env.json"
    stub = tmp_path / "fake_codex.py"
    stub.write_text(
        """import json, os, sys\nfrom pathlib import Path\nc=Path(__file__).parent/"counter"; n=int(c.read_text()) if c.exists() else 0; c.write_text(str(n+1))\n(Path(__file__).parent/"argv.json").write_text(json.dumps(sys.argv))\n(Path(__file__).parent/"env.json").write_text(json.dumps({k:os.environ.get(k) for k in ("SUPABASE_DB_URL","ANTHROPIC_API_KEY","OPENAI_API_KEY")}))\nout=sys.argv[sys.argv.index("-o")+1]\nif n == 0: Path(out).write_text("not json")\nelse: Path(out).write_text(json.dumps({"id":"wrong","fingerprint":"wrong","scoring":{"score":12,"reasoning":"valid reasoning","short_summary":""+"Достаточно подробное резюме. "*40,"hard_requirements":[],"country":"","work_mode":"remote","us_eligibility":"unclear","deadline":None},"screening":None}))\n"""
    )
    payload = {
        "payload_kind": "discovery",
        "id": "abc",
        "fingerprint": "fp",
        "score_prompt_fingerprint": d._score_fp(),
        "scoring": {
            "system_prompt": d.scoremod.SYSTEM_PROMPT,
            "user_msg": "**Full Description:**\nA posting.\n",
        },
        "screening": None,
    }
    inp = tmp_path / "input.json"
    inp.write_text(json.dumps(payload))
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("NIGHTLY_CODEX_BIN", f"{sys.executable} {stub}")
    runner._stop.clear()
    monkeypatch.setenv("SUPABASE_DB_URL", "private-test-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-sentinel")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-test-sentinel")
    ok = runner.run_one(inp, out, "test-model", time.monotonic() + 30)
    _logs = capsys.readouterr().out
    assert ok is True
    assert counter.read_text() == "2"
    assert json.loads((out / "input.json").read_text())["id"] == "abc"
    argv = json.loads(argv_log.read_text())
    assert "--disable" in argv and "shell_tool" in argv and 'web_search="disabled"' in argv
    env = json.loads(env_log.read_text())
    assert all(v is None for v in env.values())
