#!/usr/bin/env python3
"""One-off (DHA-1182): set screening.judge.path on unseen keep/unsure roles.

Re-asks the judge model (same codex call, same batches, same retries as the
nightly stage) with ``--brief`` (the v7 brief that adds ``path``), then writes
ONLY ``screening.judge.path``. The verdict, kill_kind, confidence and every
other judge key stay as they are; the fresh verdict is only compared to the
stored one and the disagreements are printed.

    backfill_judge_path.py --brief /path/brief_judge_v7.md --backup /tmp/before.json [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge_roles as jr  # noqa: E402
import settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brief", required=True)
    ap.add_argument("--backup", required=True, help="JSON file: screening before the write")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from db_conn import get_conn

    cfg = {**settings.judge(), "brief_path": args.brief}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, judge_state, screening->'judge'->>'verdict' FROM vacancy "
        "WHERE status = 'unseen' AND judge_state IN ('keep', 'unsure') "
        "AND NOT (screening->'judge' ? 'path') ORDER BY first_seen DESC"
    )
    rows = cur.fetchall()
    cur.close()
    ids = [str(r[0]) for r in rows][: args.limit or None]
    stored = {str(r[0]): r[2] for r in rows}
    roles = jr.select_roles_by_ids(ids)
    print(f"{len(roles)} roles to path", flush=True)

    backup = {}
    got = changed = failed = 0
    scratch = Path(cfg.get("scratch_dir") or Path.cwd() / "judge_scratch")
    for i, batch in enumerate(jr.build_batches(roles, cfg["batch_size"])):
        valid, missing, err = jr.judge_with_retries(batch, cfg, scratch, f"path{i}")
        if err:
            failed += len(batch)
            print(f"batch {i} FAILED: {err}", flush=True)
            continue
        failed += len(missing)
        for rid, v in valid.items():
            path = v.get("path")
            print(f"V {rid} {v['verdict']} path={path}", flush=True)
            if not (isinstance(path, int) and not isinstance(path, bool) and 0 <= path <= 6):
                failed += 1
                print(f"{rid}: no valid path in {json.dumps(v, ensure_ascii=False)[:300]}")
                continue
            if (v["verdict"] or "").lower() != (stored.get(rid) or "").lower():
                changed += 1
                print(
                    f"{rid}: verdict now {v['verdict']} (stored {stored.get(rid)}) - kept as stored"
                )
            cur = conn.cursor()
            cur.execute("SELECT screening FROM vacancy WHERE id = %s", (rid,))
            backup[rid] = cur.fetchone()[0]
            if not args.dry_run:
                cur.execute(
                    "UPDATE vacancy SET screening = jsonb_set(screening, '{judge,path}', %s::jsonb) "
                    "WHERE id = %s AND screening->'judge' IS NOT NULL",
                    (str(path), rid),
                )
            cur.close()
            got += 1
        Path(args.backup).write_text(json.dumps(backup, default=str), encoding="utf-8")
        if not args.dry_run:
            conn.commit()
        print(f"batch {i}: {got} pathed so far", flush=True)
    print(json.dumps({"pathed": got, "verdict_differs": changed, "failed": failed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
