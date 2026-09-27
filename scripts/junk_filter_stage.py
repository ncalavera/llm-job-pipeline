#!/usr/bin/env python3
"""Junk-filter stage: ask the junk task about each role the scorer would score
tonight, and skip the clear junk before it costs scorer time (plan
2026-09-27-1000-feat-jev-junk-filter, U5; R1, R3, R8, R9, R16; KTD3-KTD5).

catalog-checked: wiki tools catalog searched for "junk", "jev", "filter" - no row; this is the
screener pipeline stage that runs the self-made nikita_tools.junk_filter tool, not a tool itself.

Runs between ``filter`` and ``screening_prep`` in run_daily.py. ``[junk_filter]
mode`` (settings.junk_filter()):

  off     nothing runs (the default);
  shadow  writes ``screening['junk']`` with ``would_skip``; never touches
          ``scoring_excluded_reason``;
  live    also stamps ``scoring_excluded_reason = 'junk_filter: <question> <p>'``
          with a guarded UPDATE (unseen, unscored, no reason yet).

Fails open: an engine error, a missing key or profile, or a crash leaves the role
unskipped, so it goes to the scorer. A role is checked once per task version
(KTD4); a restored role is never checked again.

    junk_filter_stage.py                         # run the stage (run_daily.py calls this)
    junk_filter_stage.py --limit 3               # check at most 3 roles
    junk_filter_stage.py --restore <id>[,<id>]   # undo live skips on these roles
    junk_filter_stage.py --restore-all-since 2026-10-01
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_task  # noqa: E402
import prepare_discovery  # noqa: E402
import settings  # noqa: E402
from judge_roles import _current_screening, select_roles_by_ids  # noqa: E402

SECONDS_PER_SCORE = 55  # one scorer call, measured on the night runs
_PATTERN = junk_task.REASON_PREFIX + "%"  # a parameter: a literal % clashes with psycopg's %s


def engine_label(cfg: dict) -> str:
    if cfg["engine"] == "baseline":
        return f"baseline:{cfg['baseline_model']}:{cfg['cut']}"
    return "jev"


def load_engine(cfg: dict):
    """The engine callable, imported only now: nikita_tools lives on forge only."""
    if cfg["engine"] == "baseline":
        from nikita_tools import junk_baseline

        return junk_baseline.make_engine(junk_baseline.load(cfg["baseline_model"]), cfg["cut"])
    from nikita_tools.junk_filter import ask_jev

    return ask_jev


def _diagnostics(vid: str, exc: Exception) -> dict:
    """Everything a failed call tells us. JevError never carries the key."""
    return {
        "id": vid,
        "error": f"{type(exc).__name__}: {exc}",
        "status": getattr(exc, "status", None),
        "body": getattr(exc, "body", None),
        "headers": getattr(exc, "headers", None),
        "request_id": getattr(exc, "request_id", None),
        "host": socket.gethostname(),
    }


def _meta(conn, ids: list[str]) -> dict:
    """id -> (company_id, screening dict), one query for the whole pool."""
    if not ids:
        return {}
    cur = conn.cursor()
    cur.execute("SELECT id, company_id, screening FROM vacancy WHERE id = ANY(%s::uuid[])", (ids,))
    out = {}
    for vid, cid, screening in cur.fetchall():
        if isinstance(screening, str):
            try:
                screening = json.loads(screening)
            except ValueError:
                screening = None
        out[str(vid)] = (str(cid) if cid is not None else None,
                         screening if isinstance(screening, dict) else {})
    cur.close()
    return out


def _save_junk(conn, vid: str, record: dict) -> None:
    """Python-side merge (save_audit pattern): judge/audit/north_star survive."""
    from db_backend import Json

    screening = _current_screening(conn, vid)
    screening["junk"] = record
    cur = conn.cursor()
    cur.execute("UPDATE vacancy SET screening = %s WHERE id = %s", (Json(screening), vid))
    cur.close()


def _stamp(conn, vid: str, reason: str) -> bool:
    """Guarded write: only an unseen, unscored role with no reason yet."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE vacancy SET scoring_excluded_reason = %s WHERE id = %s AND status = 'unseen' "
        "AND (llm_score IS NULL OR llm_score < 0) AND scoring_excluded_reason IS NULL RETURNING id",
        (reason, vid),
    )
    hit = bool(cur.fetchall())
    cur.close()
    return hit


def _reason(record: dict) -> str:
    p = record.get("p")
    tail = f" {p:.2f}" if isinstance(p, (int, float)) else ""
    return f"{junk_task.REASON_PREFIX} {record['question']}{tail}"


def _prefixed_count(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM vacancy WHERE scoring_excluded_reason LIKE %s", (_PATTERN,))
    n = cur.fetchone()[0]
    cur.close()
    return int(n)


def sample_skips(ids: list[str], pct: int, seed: str) -> list[str]:
    """The seeded draw of ``judge_roles.sample_for_audit``: pct% of tonight's
    live skips go to the Review tab (R17, KTD10)."""
    n = round(len(ids) * pct / 100)
    return random.Random(seed).sample(ids, min(n, len(ids)))


def stop_verdict(conn, live_since: str) -> str | None:
    """R17 stop: a keep/unsure verdict on a sampled skip, given after go-live,
    means the filter dropped a wanted role. Returns why, or None.
    ponytail: the sampled test runs in Python (one SELECT, portable across
    SQLite and Postgres); the verdict rows are few."""
    cur = conn.cursor()
    cur.execute(
        "SELECT jr.vacancy_id, jr.verdict, jr.updated_at, v.screening FROM judge_review jr "
        "JOIN vacancy v ON v.id = jr.vacancy_id "
        "WHERE jr.verdict IN ('keep', 'unsure') AND jr.updated_at > %s ORDER BY jr.updated_at",
        (live_since or "1970-01-01",),
    )
    rows = cur.fetchall()
    cur.close()
    for vid, verdict, at, screening in rows:
        if isinstance(screening, str):
            screening = json.loads(screening)
        if ((screening or {}).get("junk") or {}).get("sampled"):
            return (f"wanted role in the Review sample: {vid} ({verdict} on {at}); "
                    "fix the rule or cut, then move [junk_filter] live_since")
    return None


def _scratch(root: Path | None, vid: str, blob: dict) -> None:
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{vid}.json").write_text(json.dumps(blob, ensure_ascii=False, indent=2, default=str))


def run_stage(cfg: dict, engine=None, limit: int | None = None) -> dict:
    mode = cfg["mode"]
    if mode == "off":
        return {"skipped": 'junk filter is off ([junk_filter] mode = "off")'}
    if junk_task.load_profile() is None:
        return {"skipped": "no profile: config/junk_profile.json is missing, every role goes to scoring"}
    if engine is None:
        if cfg["engine"] == "jev" and not os.environ.get("TYPESAFE_API_KEY"):
            return {"skipped": "no key: TYPESAFE_API_KEY is unset, every role goes to scoring"}
        if cfg["engine"] == "baseline" and not cfg["baseline_model"]:
            return {"skipped": "no baseline model ([junk_filter] baseline_model is empty)"}
        try:
            engine = load_engine(cfg)
        except Exception as exc:  # fail open: nothing is skipped
            return {"skipped": f"engine unavailable ({type(exc).__name__}: {exc})"}

    from db_conn import get_conn

    conn = get_conn()
    stopped = stop_verdict(conn, cfg["live_since"]) if mode == "live" else None
    if stopped:
        mode = "shadow"  # R17: no live skip until Nikita fixes the rule or cut
        print(f"junk_filter: running as shadow: {stopped}", file=sys.stderr, flush=True)
    version = junk_task.task_version(engine_label(cfg))
    budget = min(cfg["max_per_run"], limit) if limit else cfg["max_per_run"]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scratch = Path(cfg["scratch_dir"]) / run_id if cfg.get("scratch_dir") else None
    counts: Counter = Counter()
    by_question: Counter = Counter()
    errors: list[dict] = []
    tried: set[str] = set()
    skipped_ids: list[str] = []
    before = _prefixed_count(conn)

    # KTD4 refill loop: in live mode a skip frees a cap slot and the scorer
    # would pull in a role nobody checked, so re-select until the capped pool
    # holds no unchecked role (or the budget is spent).
    while counts["checked"] < budget:
        pool = [p["id"] for p in prepare_discovery.select_payloads() if p.get("scoring")]
        meta = _meta(conn, [v for v in pool if v not in tried])
        todo, cached_skips = [], []
        for vid in pool:
            if vid not in meta:
                continue
            junk = meta[vid][1].get("junk") or {}
            if junk.get("restored"):
                continue
            if junk.get("version") == version:
                if mode == "live" and junk.get("would_skip"):
                    cached_skips.append((vid, junk))  # decided in shadow; stamp now, no call
                continue
            todo.append(vid)
        todo = todo[: budget - counts["checked"]]
        if not todo and not cached_skips:
            break

        # R9: an override added since the shadow decision still sends the role to scoring.
        cached_overrides = junk_task.overrides(conn, {meta[v][0] for v, _ in cached_skips if meta[v][0]})
        for vid, junk in cached_skips:
            tried.add(vid)
            if meta[vid][0] in cached_overrides:
                counts["overridden"] += 1
                continue
            if _stamp(conn, vid, _reason(junk)):
                _save_junk(conn, vid, {**junk, "mode": mode, "skipped": True})
                skipped_ids.append(vid)
                counts["skipped"] += 1
                by_question[junk["question"]] += 1
        conn.commit()

        roles = {r["id"]: r for r in select_roles_by_ids(todo)}
        overrides = junk_task.overrides(conn, {meta[v][0] for v in todo if meta[v][0]})
        for vid in todo:
            tried.add(vid)
            role = roles.get(vid)
            if role is None:
                continue
            role["override"] = overrides.get(meta[vid][0])
            try:
                res = junk_task.check(role, engine)
            except Exception as exc:  # fail open: the role goes to scoring
                diag = _diagnostics(vid, exc)
                errors.append(diag)
                counts["errors"] += 1
                print(f"junk_filter: engine failed on {vid}: {json.dumps(diag, default=str)}",
                      file=sys.stderr, flush=True)
                _scratch(scratch, vid, {"request": junk_task.build_state(role), "error": diag})
                continue
            counts["checked"] += 1
            if res["answers"] is not None:
                _scratch(scratch, vid, {"request": junk_task.build_state(role),
                                        "response": {"answers": res["answers"], "usage": res["usage"]}})
            if role["override"]:
                counts["overridden"] += 1
            record = {
                "version": version,
                "engine": engine_label(cfg),
                "mode": mode,
                "would_skip": res["skip"],
                "question": res["question"],
                "p": res["p"],
                "answers": res["answers"],
                "usage": res["usage"],
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            if res["skip"]:
                counts["would_skip"] += 1
                if mode == "live":
                    record["skipped"] = _stamp(conn, vid, _reason(record))
                    counts["skipped"] += record["skipped"]
                    if record["skipped"]:
                        skipped_ids.append(vid)
                if mode == "shadow" or record["skipped"]:
                    by_question[res["question"]] += 1
            _save_junk(conn, vid, record)
            conn.commit()

    sampled = sorted(sample_skips(sorted(skipped_ids), cfg["sample_pct"],
                                  datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    for vid in sampled:
        junk = _current_screening(conn, vid).get("junk") or {}
        _save_junk(conn, vid, {**junk, "sampled": True, "sampled_at": datetime.now(timezone.utc).isoformat()})
    conn.commit()

    saved_roles = counts["skipped"] if mode == "live" else counts["would_skip"]
    out = {
        "mode": mode,
        "engine": engine_label(cfg),
        "version": version,
        "counts": dict(counts),
        "by_question": dict(by_question),
        "seconds_saved": saved_roles * SECONDS_PER_SCORE,
        "junk_reasons_before": before,
        "junk_reasons_after": _prefixed_count(conn),
        "errors": errors,
        "sampled": sampled,
    }
    if stopped:
        out["stopped"] = stopped
    return out


def restore(ids: list[str] | None = None, since: str | None = None) -> dict:
    """Clear junk-filter reasons only (never a rule-filter reason) and mark the
    record ``restored`` so the stage leaves the role alone from now on (KTD3)."""
    from db_conn import get_conn

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM vacancy WHERE scoring_excluded_reason LIKE %s", (_PATTERN,))
    candidates = [str(r[0]) for r in cur.fetchall()]
    cur.close()
    if ids is not None:
        candidates = [v for v in candidates if v in set(ids)]
    restored = []
    for vid in candidates:
        junk = _current_screening(conn, vid).get("junk") or {}
        if since and str(junk.get("checked_at") or "") < since:
            continue
        cur = conn.cursor()
        cur.execute(
            "UPDATE vacancy SET scoring_excluded_reason = NULL WHERE id = %s "
            "AND scoring_excluded_reason LIKE %s RETURNING id",
            (vid, _PATTERN),
        )
        hit = bool(cur.fetchall())
        cur.close()
        if hit:
            _save_junk(conn, vid, {**junk, "restored": True,
                                   "restored_at": datetime.now(timezone.utc).isoformat()})
            restored.append(vid)
    conn.commit()
    return {"restored": restored, "count": len(restored)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="check at most this many roles")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--restore", help="comma-separated vacancy ids to send back to scoring")
    group.add_argument("--restore-all-since", help="restore every skip checked on or after this ISO date")
    args = parser.parse_args()

    if args.restore:
        result = restore(ids=[i.strip() for i in args.restore.split(",") if i.strip()])
    elif args.restore_all_since:
        result = restore(since=args.restore_all_since)
    else:
        result = run_stage(settings.junk_filter(), limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
