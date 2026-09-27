#!/usr/bin/env bash
# catalog-checked: no catalog entry glues eval-gate's CASE/OUT case format to
# one repo's own matcher function; this is 15 lines calling the project's own
# mail_watch.classify(), not a general tool.
#
# One dry run of scripts/mail_watch.py's matcher against a fixed set of fake
# messages (input/emails.json + input/rules.toml). Calls mw.classify()
# directly — no Gmail client, no Telegram send, nothing written outside $OUT.
set -euo pipefail
python3 - <<'PY'
import json, os, sys
from pathlib import Path

case, out = Path(os.environ["CASE"]), Path(os.environ["OUT"])
sys.path.insert(0, "scripts")
import mail_watch as mw

rules = mw.load_rules(case / "input" / "rules.toml")
messages = sorted(json.loads((case / "input" / "emails.json").read_text()), key=lambda m: m["internalDate"])
reasons = [(m["id"], mw.classify(m["from"], m["subject"], rules)) for m in messages]
sent = min(sum(1 for _, r in reasons if r), mw.MAX_SENDS_PER_RUN)
lines = [f"summary: listed={len(messages)} matched={sum(1 for _, r in reasons if r)} sent={sent}"]
lines += [f"{mid}: {reason or 'no match'}" for mid, reason in reasons]
out.mkdir(parents=True, exist_ok=True)
(out / "result.txt").write_text("\n".join(lines) + "\n")
PY
