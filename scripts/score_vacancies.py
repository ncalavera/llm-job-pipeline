#!/usr/bin/env python3
"""Unified vacancy scoring — 2 backends, identical LLM input.

Replaces: score_vacancies_llm.py, score_prep.py, score_save.py, score_via_openclaw.py

Usage:
    score_vacancies.py --local --limit 5          # Opus subagent scoring (default)
    score_vacancies.py --openclaw --limit 20      # OpenClaw SSH
"""

import argparse
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Redirect stdout → stderr during imports so db_conn diagnostics don't pollute
# JSON output in --prepare mode
_real_stdout = sys.stdout
if "--local" in sys.argv or not any(f in sys.argv for f in ("--openclaw", "--save")):
    sys.stdout = sys.stderr

from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR, GLOBAL_BLACKLIST_DESC_SUBSTR  # noqa: E402
from prompts import VACANCY_SCORING_PROMPT as SYSTEM_PROMPT  # noqa: E402
from prompts import VACANCY_SCORING_USER_TEMPLATE as USER_TEMPLATE  # noqa: E402

sys.stdout = _real_stdout

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USA_ONLY_SCORE_CAP = 15
MAX_TOKENS = 1024
PROMPT_VERSION = "v3.2_2026_03_10_domain_calibration"
BILLING_ABORT_THRESHOLD = 5

# OpenClaw SSH
EC2_KEY = os.environ.get("OPENCLAW_SSH_KEY", "")
EC2_USER = os.environ.get("OPENCLAW_SSH_USER", "")
EC2_HOST = os.environ.get("OPENCLAW_SSH_HOST", "")
OPENCLAW_AGENT = "scorer"
OPENCLAW_TIMEOUT = 120
MAX_CONCURRENT = 3
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Inline utilities
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    """Parse JSON from LLM response, handling fences, preamble."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    brace = re.search(r'\{[^{}]*"score"\s*:\s*\d+[^{}]*\}', text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return {"error": "JSON parse failed", "raw": text[:500]}


def _is_blacklisted(title: str, description: str = "") -> bool:
    """Check title (GLOBAL_BLACKLIST + SUBSTR) and description (DESC_SUBSTR)."""
    t = title.lower()
    if any(kw in t for kw in GLOBAL_BLACKLIST_SUBSTR):
        return True
    if any(re.search(r'\b' + re.escape(kw) + r'\b', t) for kw in GLOBAL_BLACKLIST):
        return True
    if description:
        d = description.lower()
        if any(kw in d for kw in GLOBAL_BLACKLIST_DESC_SUBSTR):
            return True
    return False


def is_usa_only(vacancy) -> bool:
    """Check if vacancy has only US-based locations (v2 structure)."""
    locs = vacancy.get("locations", [])
    if not locs:
        return False
    has_us = False
    for loc in locs:
        country = (loc.get("country") or "").lower()
        region = (loc.get("region") or "").lower()
        work_mode = (loc.get("work_mode") or "").lower()
        if country in ("united states", "usa", "us"):
            has_us = True
            continue
        if region == "americas" and not country:
            has_us = True
            continue
        if work_mode == "remote" and not country and not region:
            return False
        if country or region:
            return False
        if not country and not region:
            return False
    return has_us


def _sanitize_text(text: str) -> str:
    """Normalize text for safe API submission."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ")
    text = "".join(
        c for c in text
        if c == "\n" or c == "\t" or (ord(c) >= 32 and ord(c) != 127)
    )
    return text


def _build_user_msg(vacancy: dict, alignment_score=None) -> str:
    """Build user message for scoring — identical for all backends."""
    description = vacancy.get("full_description") or vacancy.get("snippet") or "No description available"
    description = _sanitize_text(description)
    if len(description) > 8000:
        description = description[:8000] + "\n\n[Description truncated]"

    locs = vacancy.get("locations", [])
    loc_parts = []
    for loc in locs:
        # Try structured v2 fields first, fall back to v1 'location' text
        parts = [p for p in [loc.get("city"), loc.get("country"), loc.get("region"), loc.get("work_mode")] if p]
        if not parts:
            # v1 fallback: 'location' key contains free-text like "Washington, DC metro area"
            v1_loc = loc.get("location")
            if v1_loc:
                parts = [v1_loc]
        if parts:
            loc_parts.append(", ".join(parts))
    location_str = "; ".join(loc_parts)

    alignment_str = f"{alignment_score}/100" if alignment_score is not None else "not enriched"

    tier = vacancy.get("tier") or vacancy.get("company_tier") or "?"

    return USER_TEMPLATE.format(
        org=vacancy["org"],
        tier=tier,
        title=vacancy["title"],
        location=location_str or "",
        description=description,
        alignment_score=alignment_str,
    )


# ---------------------------------------------------------------------------
# OpenClaw backend
# ---------------------------------------------------------------------------

def _ssh_score(message: str) -> dict:
    """Send scoring request to OpenClaw via SSH."""
    escaped_msg = shlex.quote(message)
    cmd = [
        "ssh", "-i", EC2_KEY,
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        f"{EC2_USER}@{EC2_HOST}",
        f"openclaw agent --agent {OPENCLAW_AGENT} "
        f"--session-id scorer-{uuid.uuid4().hex[:8]} "
        f"--timeout {OPENCLAW_TIMEOUT} --message {escaped_msg}",
    ]
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=OPENCLAW_TIMEOUT + 30,
        )
        latency = time.time() - t0
        if result.returncode != 0:
            return {
                "score": -1, "reasoning": "", "tags": [],
                "hard_requirements": [], "short_summary": "",
                "error": f"SSH exit {result.returncode}: {result.stderr.strip()[:200]}",
                "latency": round(latency, 2),
            }
        parsed = _parse_json(result.stdout.strip())
        return {
            "score": parsed.get("score", -1),
            "reasoning": parsed.get("reasoning", ""),
            "tags": parsed.get("tags", []),
            "hard_requirements": parsed.get("hard_requirements", []),
            "short_summary": parsed.get("short_summary", ""),
            "model": f"openclaw/{OPENCLAW_AGENT}",
            "scored_at": datetime.now().isoformat(),
            "latency": round(latency, 2),
            "error": parsed.get("error"),
        }
    except subprocess.TimeoutExpired:
        return {
            "score": -1, "reasoning": "", "tags": [],
            "hard_requirements": [], "short_summary": "",
            "error": "SSH timeout",
            "latency": round(time.time() - t0, 2),
        }
    except Exception as e:
        return {
            "score": -1, "reasoning": "", "tags": [],
            "hard_requirements": [], "short_summary": "",
            "error": str(e),
            "latency": round(time.time() - t0, 2),
        }


def _ssh_score_with_retry(message: str) -> dict:
    """Send with exponential backoff on failures."""
    result = {}
    for attempt in range(MAX_RETRIES):
        result = _ssh_score(message)
        if not result.get("error") or result["score"] != -1:
            return result
        if attempt < MAX_RETRIES - 1:
            backoff = (2 ** attempt) * (1 + random.random())
            time.sleep(backoff)
    return result


def _build_openclaw_message(user_msg: str) -> str:
    """Wrap system+user for OpenClaw (no separate system prompt support)."""
    return (
        f"<system>\n{SYSTEM_PROMPT}\n</system>\n\n{user_msg}\n\n"
        "IMPORTANT: Return ONLY valid JSON, no markdown fences, no preamble."
    )


# ---------------------------------------------------------------------------
# Data loading + dedup
# ---------------------------------------------------------------------------

def _load_and_dedup(*, force=False, exclude_usa=False, include_passed=False,
                    limit=None, offset=0):
    """Load vacancies, dedup by (org, title), filter.

    Returns: (roles, fitness_map, stats)
        roles = [(key, rep, members), ...]
    """
    from database_supabase import load_vacancies, get_company_fitness_map

    # Skip already-decided vacancies by default — /score should not waste
    # subagent budget on auto-passed expired or user-skipped rows (issue #233).
    status_exclude = None if include_passed else ["passed", "skipped"]
    vacancies = (
        load_vacancies(status_exclude=status_exclude) if force
        else load_vacancies(unscored_only=True, status_exclude=status_exclude)
    )
    fitness_map = get_company_fitness_map()

    role_groups: dict[tuple[str, str], list[dict]] = {}
    for v in vacancies.values():
        key = (v["org"], v["title"])
        role_groups.setdefault(key, []).append(v)

    roles = []
    stats = {"blacklisted": 0, "usa": 0, "blind": 0, "total": len(vacancies)}
    for key, members in role_groups.items():
        rep = max(
            members,
            key=lambda m: len(m.get("full_description") or m.get("snippet") or ""),
        )
        # Compute desc up-front so blacklist can check description-level kills.
        desc = rep.get("full_description") or rep.get("snippet") or ""
        if _is_blacklisted(rep["title"], desc):
            stats["blacklisted"] += 1
            continue
        if exclude_usa and is_usa_only(rep):
            stats["usa"] += 1
            continue
        # Blind vacancy gate — skip if no description AND no snippet
        if not desc.strip():
            stats["blind"] += 1
            print(f"  [BLIND SKIP] {rep['org']:25s} {rep['title'][:50]} (no description)",
                  file=sys.stderr, flush=True)
            continue
        if not force and rep.get("llm_score") is not None and rep.get("llm_score") != -1:
            continue
        roles.append((key, rep, members))

    roles = roles[offset:]
    if limit:
        roles = roles[:limit]

    return roles, fitness_map, stats


# ---------------------------------------------------------------------------
# Shared: apply USA cap + build score_data
# ---------------------------------------------------------------------------

def _make_score_data(result: dict, rep: dict) -> dict:
    """Build score_data dict with USA cap applied."""
    score = result["score"]
    if isinstance(score, (int, float)) and score > USA_ONLY_SCORE_CAP and is_usa_only(rep):
        score = USA_ONLY_SCORE_CAP
    data = {
        "llm_score": score,
        "llm_reasoning": result["reasoning"],
        "llm_summary": result["short_summary"],
        "llm_hard_requirements": result.get("hard_requirements", []),
    }
    # LLM-extracted deadline (nullable, only fills gaps)
    dl = result.get("deadline")
    if dl and dl != "null":
        data["llm_deadline"] = dl
    return data


# ---------------------------------------------------------------------------
# Mode: --local
# ---------------------------------------------------------------------------

def cmd_local(args):
    """Output JSON to stdout for subagent scoring."""
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr

    roles, fitness_map, stats = _load_and_dedup(
        force=args.force, exclude_usa=args.exclude_usa,
        include_passed=args.include_passed,
        limit=args.limit, offset=args.offset,
    )

    if not roles:
        sys.stdout = _real_stdout
        json.dump([], _real_stdout, ensure_ascii=False)
        return

    print(f"Preparing {len(roles)} roles for subagent scoring", file=sys.stderr)

    output = []
    for _key, rep, members in roles:
        company_info = fitness_map.get(rep["org"], {})
        alignment = company_info.get("alignment_score")
        user_msg = _build_user_msg(rep, alignment_score=alignment)

        output.append({
            "payload_kind": "vacancy",
            "id": rep["id"],
            "member_ids": [m["id"] for m in members],
            "org": rep["org"],
            "title": rep["title"],
            "is_usa_only": is_usa_only(rep),
            "system_prompt": SYSTEM_PROMPT,
            "user_msg": user_msg,
        })

    sys.stdout = _real_stdout
    json.dump(output, _real_stdout, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Mode: --save
# ---------------------------------------------------------------------------

def cmd_save(_args):
    """Read scored results from stdin JSON, save to DB."""
    from database_supabase import update_llm_score, get_conn

    data = json.load(sys.stdin)
    if not data:
        print("No results to save.")
        return

    conn = get_conn()
    total_records = 0
    errors = 0

    for entry in data:
        if entry.get("payload_kind") != "vacancy":
            print(
                f"ERROR: wrong payload_kind={entry.get('payload_kind')!r}, "
                f"expected 'vacancy'. Skipping {entry.get('org', '?')} — "
                f"{entry.get('title', '?')}",
                file=sys.stderr,
            )
            errors += 1
            continue

        score_data = entry.get("score_data")
        if not score_data:
            print(
                f"ERROR: missing score_data for {entry.get('org', '?')} — "
                f"{entry.get('title', '?')}",
                file=sys.stderr,
            )
            errors += 1
            continue

        # Apply USA-only cap
        if entry.get("is_usa_only") and isinstance(
            score_data.get("llm_score"), (int, float)
        ):
            if score_data["llm_score"] > USA_ONLY_SCORE_CAP:
                score_data["llm_score"] = USA_ONLY_SCORE_CAP

        # Warn on short summaries
        summary = score_data.get("llm_summary", "")
        if summary and len(summary) < 200:
            print(
                f"WARNING: Short summary ({len(summary)} chars) for "
                f"{entry.get('org', '?')} — {entry.get('title', '?')}",
                file=sys.stderr,
            )

        for member_id in entry["member_ids"]:
            rowcount = update_llm_score(member_id, score_data)
            if rowcount == 0:
                print(
                    f"WARNING: UUID {member_id} not found in DB — score not saved "
                    f"for {entry.get('org', '?')} — {entry.get('title', '?')}",
                    file=sys.stderr,
                )
                errors += 1
            else:
                total_records += 1

    conn.commit()
    print(f"Saved {len(data) - errors} scores ({total_records} records). Errors: {errors}")

    # Self-healing: normalize locations[] for any vacancy that has dirty city/country.
    # Idempotent — returns 0 changes on already-clean rows.
    try:
        from cleanup_locations import normalize_location
        scored_ids = [m for entry in data for m in entry.get("member_ids", [])]
        if scored_ids:
            heal_cur = conn.cursor()
            heal_cur.execute(
                "SELECT id, locations, full_description, snippet "
                "FROM vacancy WHERE id = ANY(%s) AND locations IS NOT NULL",
                (scored_ids,),
            )
            healed = 0
            for vid, locs, desc, snip in heal_cur.fetchall():
                full_text = (desc or "") + "\n" + (snip or "")
                new_locs = []
                changed = False
                for loc in locs:
                    cleaned, changes = normalize_location(loc, full_text)
                    if changes:
                        changed = True
                    new_locs.append(cleaned)
                if changed:
                    from psycopg2.extras import Json
                    heal_cur.execute(
                        "UPDATE vacancy SET locations = %s WHERE id = %s",
                        (Json(new_locs), vid),
                    )
                    healed += 1
            heal_cur.close()
            if healed:
                conn.commit()
                print(f"Self-healed {healed} vacancies with dirty locations.")
    except Exception as e:
        print(f"Self-healing skipped: {e}", file=sys.stderr)

    # Regenerate dashboard so ticker and stats reflect new scores
    from report import generate_dashboard
    generate_dashboard()
    print("Dashboard regenerated.")


# ---------------------------------------------------------------------------
# Mode: --openclaw
# ---------------------------------------------------------------------------

def cmd_openclaw(args):
    """Score vacancies via OpenClaw SSH."""
    if not all([EC2_KEY, EC2_USER, EC2_HOST]):
        print("ERROR: Set OPENCLAW_SSH_KEY, OPENCLAW_SSH_USER, OPENCLAW_SSH_HOST")
        sys.exit(1)

    roles, fitness_map, stats = _load_and_dedup(
        exclude_usa=args.exclude_usa, include_passed=args.include_passed,
        limit=args.limit,
    )

    if not roles:
        print("All roles already scored.")
        return

    from database_supabase import update_llm_score, get_conn, validate_db

    conn = get_conn()
    conn.rollback()
    cur = conn.cursor()
    cur.execute("SET statement_timeout = 30000")
    cur.close()
    conn.commit()

    duped_total = sum(len(m) for _, _, m in roles)
    print(
        f"Scoring {len(roles)} unique roles ({duped_total} records) "
        f"via OpenClaw ({OPENCLAW_AGENT}, {args.max_workers}x parallel)"
    )

    if args.dry_run:
        for i, (_key, rep, members) in enumerate(roles[:20], 1):
            print(f"  {i}. {rep['org']:30s} {rep['title'][:50]:50s} [x{len(members)}]")
        if len(roles) > 20:
            print(f"  ... and {len(roles) - 20} more")
        return

    errors = 0
    scored = 0
    start_time = time.time()

    def _worker(item):
        idx, (_key, rep, members) = item
        time.sleep(random.uniform(1.0, 3.0))
        company_info = fitness_map.get(rep["org"], {})
        alignment = company_info.get("alignment_score")
        user_msg = _build_user_msg(rep, alignment_score=alignment)
        message = _build_openclaw_message(user_msg)
        return idx, rep, members, _ssh_score_with_retry(message)

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        future_map = {
            executor.submit(_worker, (i, item)): i
            for i, item in enumerate(roles, 1)
        }

        for future in as_completed(future_map):
            idx, rep, members, result = future.result()
            score = result["score"]

            if result.get("error") and score == -1:
                errors += 1
                print(
                    f"  [{idx}/{len(roles)}] {rep['org']:25s} "
                    f"{rep['title'][:50]:50s}  ERR: {result['error'][:80]}"
                )
                if errors >= 5:
                    conn.commit()
                    print(f"\n  {errors} errors — cancelling.")
                    for f in future_map:
                        f.cancel()
                    break
                continue

            print(
                f"  [{idx}/{len(roles)}] {rep['org']:25s} "
                f"{rep['title'][:50]:50s}  -> {score:3d}  "
                f"({result['latency']:.1f}s)  [x{len(members)}]"
            )

            score_data = _make_score_data(result, rep)
            try:
                for m in members:
                    update_llm_score(m["id"], score_data)
                conn.commit()
            except Exception as db_err:
                conn.rollback()
                errors += 1
                print(f"    DB write error: {db_err}")
                continue

            scored += 1
            if scored % 10 == 0:
                elapsed = time.time() - start_time
                rate = scored / max(elapsed / 60, 0.1)
                remaining = (len(roles) - scored - errors) / rate if rate > 0 else 0
                print(
                    f"  --- progress: {scored} scored, "
                    f"{rate:.1f}/min, ~{remaining:.0f}m remaining ---"
                )

    conn.commit()
    elapsed = time.time() - start_time
    print(
        f"\nDone! Scored {scored} roles ({duped_total} records) "
        f"in {elapsed / 60:.1f}m. Errors: {errors}"
    )
    validate_db()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Unified vacancy scoring")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--openclaw", action="store_true", help="OpenClaw SSH")
    mode.add_argument("--local", action="store_true", help="JSON → stdout for Opus subagents (default)")
    mode.add_argument("--save", action="store_true", help="stdin JSON → DB (internal)")

    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--exclude-usa", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--include-passed",
        action="store_true",
        help="Score vacancies even if status='passed' or 'skipped' (default: skip them — issue #233)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-workers", type=int, default=MAX_CONCURRENT)

    args = parser.parse_args()

    if args.openclaw:
        cmd_openclaw(args)
    elif args.save:
        cmd_save(args)
    else:
        # Default to --local
        cmd_local(args)


if __name__ == "__main__":
    main()
