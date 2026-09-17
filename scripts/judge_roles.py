#!/usr/bin/env python3
"""Judge stage: KEEP/UNSURE/KILL board roles against a
private brief, then audit a sample of the kills with a second model.

Self-contained script stage in run_daily.py's STAGE_ORDER, right after
``screening_prep``. It never emits a gate — the unattended night just runs it.
Judge model: codex exec or ``claude -p``, launched the same way as the eval
prototype (~/jobsearch/chat-screen-2026-09-17/eval/run_batch_v4.sh): one batch
of roles per model call, stdin from /dev/null, a scratch dir the child sees
and nothing else. Audit model: one removal per call, flag-only — it can never
restore a role, only write ``screening.audit`` for the weekly Review tab.

Both stages read ``[judge]`` from config/defaults.toml (``settings.judge()``).
``brief_path`` / ``review_brief_path`` ship empty in the public repo, so a
public checkout's stages both skip with a plain note instead of erroring.

    judge_roles.py                              # run the judge stage (called by run_daily.py)
    judge_roles.py --audit                      # run the audit stage
    judge_roles.py --dry-run --ids a,b,c        # read-only: judge 3 real roles, print, write nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import settings  # noqa: E402
from llm_json import parse_llm_json  # noqa: E402
from prepare_screening import _norm_ws, _quote_in  # noqa: E402

VERDICTS = {"KEEP", "UNSURE", "KILL"}

_TODAY_RE = re.compile(r"today is \d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# Brief handling
# ---------------------------------------------------------------------------


def inject_today(brief_text: str, today: str | None = None) -> str:
    """Replace a hardcoded ``today is YYYY-MM-DD`` line with the real date."""
    stamp = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _TODAY_RE.sub(f"today is {stamp}", brief_text)


def brief_version(path: str) -> str:
    """sha1[:8] of the brief file's bytes, plus its filename (contract format)."""
    data = Path(path).read_bytes()
    return f"{hashlib.sha1(data).hexdigest()[:8]}:{Path(path).name}"


# ---------------------------------------------------------------------------
# Column feature-detect (migration 0033 not applied yet — never crash)
# ---------------------------------------------------------------------------


def judge_columns_ready() -> bool:
    try:
        from database_supabase import _vacancy_has_column
    except Exception:
        return False
    return _vacancy_has_column("judge_state") and _vacancy_has_column("description_source")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _decode_locations(row: dict) -> list | None:
    loc = row.get("locations")
    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except (ValueError, TypeError):
            pass
    return loc


def _row_to_role(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "org": row.get("org"),
        "title": row.get("title"),
        "locations": _decode_locations(row),
        "posting": row.get("full_description") or "",
    }


def select_roles(cap: int) -> list[dict]:
    """Board roles waiting for a judge verdict, oldest first, capped at ``cap``.

    Requires judge_columns_ready() — callers check that first."""
    from db_backend import RealDictCursor
    from db_conn import get_conn

    cur = get_conn().cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT v.id, c.canonical_name AS org, v.title, v.locations, v.full_description "
        "FROM vacancy v LEFT JOIN company c ON v.company_id = c.id "
        "WHERE v.source_board IS NOT NULL AND v.status = 'unseen' "
        "AND v.scoring_excluded_reason IS NULL AND v.judge_state = 'pending' "
        "AND length(v.full_description) >= 400 "
        "AND v.description_source IS DISTINCT FROM 'board_summary' "
        "ORDER BY v.created_at ASC LIMIT %s",
        (cap,),
    )
    rows = cur.fetchall()
    cur.close()
    return [_row_to_role(r) for r in rows]


def select_roles_by_ids(ids: list[str]) -> list[dict]:
    """Read-only lookup by id for ``--dry-run`` — needs none of the new columns."""
    from db_backend import RealDictCursor
    from db_conn import get_conn

    if not ids:
        return []
    placeholders = ", ".join(["%s"] * len(ids))
    cur = get_conn().cursor(cursor_factory=RealDictCursor)
    cur.execute(
        f"SELECT v.id, c.canonical_name AS org, v.title, v.locations, v.full_description "
        f"FROM vacancy v LEFT JOIN company c ON v.company_id = c.id "
        f"WHERE v.id IN ({placeholders})",
        tuple(ids),
    )
    rows = cur.fetchall()
    cur.close()
    by_id = {str(r["id"]): _row_to_role(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


# ---------------------------------------------------------------------------
# Batches + payloads
# ---------------------------------------------------------------------------


def build_batches(roles: list[dict], batch_size: int) -> list[list[dict]]:
    return [roles[i : i + batch_size] for i in range(0, len(roles), max(1, batch_size))]


def payload_for(roles: list[dict], system_prompt: str) -> dict:
    return {
        "system_prompt": system_prompt,
        "roles": [
            {
                "id": r["id"],
                "org": r["org"],
                "title": r["title"],
                "locations": r["locations"],
                "posting": r["posting"],
            }
            for r in roles
        ],
    }


# ---------------------------------------------------------------------------
# Model launch — one parametrised command builder (codex exec / claude -p)
# ---------------------------------------------------------------------------


def command(provider: str, model: str, effort: str, directory, prompt_text: str) -> list[str]:
    if provider == "codex":
        return shlex.split(os.environ.get("JUDGE_CODEX_BIN") or "codex") + [
            "exec",
            "--skip-git-repo-check",
            "-s",
            "workspace-write",
            "-C",
            str(directory),
            "-m",
            model,
            "-c",
            f"model_reasoning_effort={effort}",
            prompt_text,
        ]
    if provider == "claude":
        base = shlex.split(os.environ.get("JUDGE_CLAUDE_BIN") or "claude")
        return base + [
            "-p",
            prompt_text,
            "--model",
            model,
            "--permission-mode",
            "dontAsk",
            "--tools",
            "Read,Write",
            "--allowed-tools",
            "Read,Write",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "project,local",
            "--max-turns",
            "12",
            "--output-format",
            "json",
        ]
    raise ValueError(f"unknown judge provider {provider!r}")


def _child_env(provider: str) -> dict:
    if provider == "codex":
        from discovery_runner import child_env

        return child_env()
    from nightly_run import _claude_env

    return _claude_env(False)


def run_model_call(
    prompt_text: str, provider: str, model: str, effort: str, scratch_dir: Path, timeout: int
) -> tuple[bool, str | None]:
    """Launch one model call (writes its own output file); return (ok, error).

    Full diagnostics on failure: exit code, elapsed seconds, stderr/stdout
    tail, and the log path — never raises."""
    scratch_dir.mkdir(parents=True, exist_ok=True)
    log_path = scratch_dir / "call.log"
    cmd = command(provider, model, effort, scratch_dir, prompt_text)
    env = _child_env(provider)
    start = time.monotonic()
    try:
        with open(log_path, "w", encoding="utf-8") as log_fh:
            proc = subprocess.run(
                cmd,
                cwd=scratch_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return False, f"{provider} timed out after {timeout}s (log: {log_path})"
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
        return False, f"{provider} exited {proc.returncode} in {elapsed:.0f}s; log tail: {tail}"
    return True, None


def run_batch(
    roles: list[dict], cfg: dict, scratch_root: Path, batch_id: str
) -> tuple[list | None, str | None]:
    """One model call for a batch of roles. Returns (parsed_list, error)."""
    scratch_dir = scratch_root / f"batch-{batch_id}"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    payload_path = scratch_dir / "payload.json"
    output_path = scratch_dir / "output.json"
    system_prompt = inject_today(Path(cfg["brief_path"]).read_text(encoding="utf-8"))
    payload_path.write_text(
        json.dumps(payload_for(roles, system_prompt), ensure_ascii=False), encoding="utf-8"
    )
    prompt_text = (
        f"Read the payload file {payload_path}. Follow its system_prompt; judge every role "
        f"in its roles array. Write ONE JSON array to {output_path}. Read no other file."
    )
    _, err = run_model_call(
        prompt_text, cfg["provider"], cfg["model"], cfg["effort"], scratch_dir, cfg["timeout"]
    )
    if err:
        return None, err
    if not output_path.exists():
        return None, f"no output file written (scratch: {scratch_dir})"
    raw = output_path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = parse_llm_json(raw)
    if not isinstance(data, list):
        return None, f"model output is not a JSON array (scratch: {scratch_dir})"
    return data, None


# ---------------------------------------------------------------------------
# Completeness check
# ---------------------------------------------------------------------------


def completeness_check(batch_ids: list[str], results) -> tuple[dict, list[str], list[str], int]:
    """Validate a batch's raw results against the contract.

    Returns ``(valid, missing_ids, duplicate_ids, broken_count)``. ``valid``
    maps id -> verdict dict for every entry that passes: JSON object, id in
    the batch, verdict in the enum, confidence 1-5 int on KILL. A repeated id
    is flagged as a duplicate; its last valid occurrence wins (a one-role
    retry legitimately re-sends an id already seen once).
    """
    batch_set = set(batch_ids)
    valid: dict = {}
    seen: set = set()
    duplicates: set = set()
    broken = 0
    if not isinstance(results, list):
        return {}, list(batch_ids), [], 1
    for item in results:
        if not isinstance(item, dict):
            broken += 1
            continue
        vid = item.get("id")
        if not isinstance(vid, str) or vid not in batch_set:
            broken += 1
            continue
        if vid in seen:
            duplicates.add(vid)
        seen.add(vid)
        verdict = item.get("verdict")
        if verdict not in VERDICTS:
            broken += 1
            continue
        if verdict == "KILL":
            conf = item.get("confidence")
            if not isinstance(conf, int) or isinstance(conf, bool) or not (1 <= conf <= 5):
                broken += 1
                continue
        valid[vid] = item
    missing = [i for i in batch_ids if i not in valid]
    return valid, missing, sorted(duplicates), broken


def judge_with_retries(
    roles: list[dict], cfg: dict, scratch_root: Path, batch_id: str, max_retries: int = 2
) -> tuple[dict, list[str], str | None]:
    """Judge one batch; missing/broken ids are re-sent one role per call."""
    ids = [r["id"] for r in roles]
    data, err = run_batch(roles, cfg, scratch_root, batch_id)
    if err:
        return {}, ids, err
    valid, missing, _dup, _broken = completeness_check(ids, data)
    by_id = {r["id"]: r for r in roles}
    for attempt in range(1, max_retries + 1):
        if not missing:
            break
        still_missing = []
        for i, vid in enumerate(missing):
            single, single_err = run_batch(
                [by_id[vid]], cfg, scratch_root, f"{batch_id}-retry{attempt}-{i}"
            )
            if single_err:
                still_missing.append(vid)
                continue
            one_valid, one_missing, _d, _b = completeness_check([vid], single)
            valid.update(one_valid)
            still_missing.extend(one_missing)
        missing = still_missing
    return valid, missing, None


# ---------------------------------------------------------------------------
# Apply rule (contract + amendment)
# ---------------------------------------------------------------------------


def apply_decision(role: dict, verdict_obj: dict, cfg: dict, model: str, brief_ver: str):
    """One validated verdict -> (judge_state, status_update|None, judge_json, counter|None)."""
    verdict = verdict_obj["verdict"]
    kind = verdict_obj.get("kill_kind") if verdict == "KILL" else None
    reason = str(verdict_obj.get("reason") or "")[:500]
    quote = verdict_obj.get("quote")
    quote = quote if isinstance(quote, str) else None
    confidence = verdict_obj.get("confidence") if verdict == "KILL" else None
    judge_json = {
        "verdict": verdict,
        "kill_kind": kind,
        "reason": reason,
        "quote": quote,
        "confidence": confidence,
        "model": model,
        "brief_version": brief_ver,
        "judged_at": datetime.now(timezone.utc).isoformat(),
    }
    if verdict != "KILL":
        return verdict.lower(), None, judge_json, None

    conf_ok = (
        isinstance(confidence, int)
        and not isinstance(confidence, bool)
        and confidence >= cfg["kill_confidence"]
    )
    # v4 brief exception: a US/Canada onsite-only location kill quotes the
    # role's TITLE, not the posting body — so the quote check runs against
    # posting + title, same as chat-screen/apply_kills.py's `hay`.
    hay = _norm_ws(role["posting"] or "") + " " + _norm_ws(role["title"] or "")
    quote_ok = kind == "direction" or (quote is not None and _quote_in(quote, hay))

    if conf_ok and quote_ok:
        status_reason = f"{kind}: {quote or reason}"[:500]
        return "killed", {"status": "passed", "status_reason": status_reason}, judge_json, None

    held = f"confidence {confidence}" if not conf_ok else "quote not found"
    judge_json["held"] = held
    counter = "held_low_confidence" if not conf_ok else "quote_refused"
    return "unsure", None, judge_json, counter


# ---------------------------------------------------------------------------
# DB writes — Python-side jsonb merge (never wipes posting_facts / other keys)
# ---------------------------------------------------------------------------


def _current_screening(conn, vac_id: str) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT screening FROM vacancy WHERE id = %s", (vac_id,))
    row = cur.fetchone()
    cur.close()
    if not row or row[0] is None:
        return {}
    val = row[0]
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (ValueError, TypeError):
            return {}
    return val if isinstance(val, dict) else {}


def save_judge(conn, vac_id: str, judge_json: dict, judge_state: str, status_update) -> None:
    from db_backend import Json

    screening = _current_screening(conn, vac_id)
    screening["judge"] = judge_json
    cur = conn.cursor()
    cur.execute(
        "UPDATE vacancy SET screening = %s, judge_state = %s WHERE id = %s",
        (Json(screening), judge_state, vac_id),
    )
    if status_update:
        cur.execute(
            "UPDATE vacancy SET status = %s, status_reason = %s, status_updated_at = now(), "
            "updated_at = now() WHERE id = %s AND status = 'unseen'",
            (status_update["status"], status_update["status_reason"], vac_id),
        )
    cur.close()


def save_audit(conn, vac_id: str, audit_json: dict) -> None:
    from db_backend import Json

    screening = _current_screening(conn, vac_id)
    screening["audit"] = audit_json
    cur = conn.cursor()
    cur.execute("UPDATE vacancy SET screening = %s WHERE id = %s", (Json(screening), vac_id))
    cur.close()


# ---------------------------------------------------------------------------
# Judge stage
# ---------------------------------------------------------------------------


def run_judge_stage(cfg: dict, dry_run: bool = False, roles: list[dict] | None = None) -> dict:
    if not cfg.get("brief_path"):
        return {"skipped": "no judge brief configured ([judge] brief_path is empty)"}
    if roles is None:
        if not judge_columns_ready():
            return {
                "skipped": "judge_state/description_source columns missing (migration 0033 not applied)"
            }
        roles = select_roles(cfg["max_per_run"])
    counts: dict = defaultdict(int)
    decisions = []
    if not roles:
        return {"counts": dict(counts), "decisions": decisions}

    from db_conn import get_conn

    conn = get_conn()
    model_label = f"{cfg['provider']}:{cfg['model']}"
    brief_ver = brief_version(cfg["brief_path"])
    scratch_root = Path(cfg.get("scratch_dir") or Path.cwd() / "judge_scratch")
    batches = build_batches(roles, cfg["batch_size"])
    for i, batch in enumerate(batches):
        valid, missing, err = judge_with_retries(batch, cfg, scratch_root, str(i))
        if err:
            counts["missing_after_retry"] += len(batch)
            continue
        for role in batch:
            v = valid.get(role["id"])
            if v is None:
                counts["missing_after_retry"] += 1
                continue
            judge_state, status_update, judge_json, counter = apply_decision(
                role, v, cfg, model_label, brief_ver
            )
            counts["judged"] += 1
            counts[judge_state] += 1
            if counter:
                counts[counter] += 1
            decisions.append(
                {
                    "id": role["id"],
                    "judge_state": judge_state,
                    "status_update": status_update,
                    "judge": judge_json,
                }
            )
            if not dry_run:
                save_judge(conn, role["id"], judge_json, judge_state, status_update)
        if not dry_run:
            conn.commit()
    return {"counts": dict(counts), "decisions": decisions}


# ---------------------------------------------------------------------------
# Audit stage
# ---------------------------------------------------------------------------


def sample_for_audit(kills: list[dict], min_score: int, sample_pct: int, seed: str) -> dict:
    """id -> why_sampled, deterministic for a given ``seed`` (run id/date)."""
    chosen: dict[str, str] = {}
    rest = []
    for k in kills:
        if k.get("kill_kind") == "direction":
            chosen[k["id"]] = "direction"
            continue
        score = k.get("llm_score")
        if isinstance(score, (int, float)) and score >= min_score:
            chosen[k["id"]] = f"score>={min_score}"
            continue
        rest.append(k)
    rng = random.Random(seed)
    n = round(len(rest) * sample_pct / 100)
    for k in rng.sample(rest, min(n, len(rest))):
        chosen.setdefault(k["id"], "random")
    return chosen


def _tonight_kills() -> list[dict]:
    """Board roles this run just killed: judge_state='killed', judge.reason/kill_kind
    read back from the stored screening.judge object."""
    from db_backend import RealDictCursor
    from db_conn import get_conn

    cur = get_conn().cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT v.id, c.canonical_name AS org, v.title, v.locations, v.full_description, "
        "v.screening, v.llm_score FROM vacancy v LEFT JOIN company c ON v.company_id = c.id "
        "WHERE v.judge_state = 'killed'"
    )
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        screening = r.get("screening")
        if isinstance(screening, str):
            try:
                screening = json.loads(screening)
            except (ValueError, TypeError):
                screening = {}
        judge = (screening or {}).get("judge") or {}
        role = _row_to_role(r)
        role["kill_kind"] = judge.get("kill_kind")
        role["judge"] = judge
        role["llm_score"] = r.get("llm_score")
        out.append(role)
    return out


def run_audit_stage(
    cfg: dict, seed: str, dry_run: bool = False, roles: list[dict] | None = None
) -> dict:
    if not cfg.get("review_brief_path"):
        return {"skipped": "no audit brief configured ([judge] review_brief_path is empty)"}
    kills = roles if roles is not None else _tonight_kills()
    if not kills:
        return {"counts": {"audited": 0, "flagged": 0}, "decisions": []}
    sample = sample_for_audit(kills, cfg["audit_min_score"], cfg["audit_sample_pct"], seed)
    by_id = {k["id"]: k for k in kills}
    review_brief = Path(cfg["review_brief_path"]).read_text(encoding="utf-8")
    scratch_root = Path(cfg.get("scratch_dir") or Path.cwd() / "audit_scratch")
    audited = flagged = 0
    decisions = []

    from db_conn import get_conn

    conn = get_conn()
    for vid, why in sample.items():
        role = by_id[vid]
        scratch_dir = scratch_root / vid[:12]
        scratch_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "system_prompt": review_brief,
            "judge": role["judge"],
            "user_msg": {
                "id": role["id"],
                "org": role["org"],
                "title": role["title"],
                "locations": role["locations"],
                "posting": role["posting"],
            },
        }
        payload_path = scratch_dir / "payload.json"
        output_path = scratch_dir / "output.json"
        payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        prompt_text = (
            f"Read the payload file {payload_path}. Follow its system_prompt to review the ONE "
            f"removal it describes. Write ONE JSON object to {output_path}: "
            '{"id": "...", "audit": "UPHOLD|OVERTURN", "why": "one line, max 140 chars"}. '
            "Read no other file."
        )
        _, err = run_model_call(
            prompt_text,
            cfg["audit_provider"],
            cfg["audit_model"],
            cfg["effort"],
            scratch_dir,
            cfg["timeout"],
        )
        if err or not output_path.exists():
            continue
        try:
            result = json.loads(output_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            result = parse_llm_json(output_path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(result, dict) or result.get("audit") not in ("UPHOLD", "OVERTURN"):
            continue
        audit_json = {
            "verdict": result["audit"],
            "why": str(result.get("why") or "")[:200],
            "model": f"{cfg['audit_provider']}:{cfg['audit_model']}",
            "audited_at": datetime.now(timezone.utc).isoformat(),
            "why_sampled": why,
        }
        audited += 1
        if audit_json["verdict"] == "OVERTURN":
            flagged += 1
        decisions.append({"id": vid, "audit": audit_json})
        if not dry_run:
            save_audit(conn, vid, audit_json)
            conn.commit()
    rate = round(100 * flagged / audited) if audited else 0
    print(f"judge audit: {audited} audited, {flagged} flagged (flag rate {rate}%)", flush=True)
    return {"counts": {"audited": audited, "flagged": flagged}, "decisions": decisions}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", action="store_true", help="run the audit stage instead")
    parser.add_argument("--dry-run", action="store_true", help="read-only: print, write nothing")
    parser.add_argument("--ids", help="comma-separated vacancy ids (dry-run only)")
    args = parser.parse_args()

    cfg = settings.judge()
    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
        roles = select_roles_by_ids(ids)
    else:
        roles = None

    seed = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.audit:
        result = run_audit_stage(cfg, seed, dry_run=args.dry_run, roles=roles)
    else:
        result = run_judge_stage(cfg, dry_run=args.dry_run, roles=roles)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
