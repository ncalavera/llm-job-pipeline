#!/usr/bin/env python3
"""Fetch-health report — is the pipeline actually parsing anything?

Read-only. Answers the two questions you cannot answer by staring at the
dashboard: which ACTIVE companies never produce vacancies (and why), and which
job BOARDS silently return nothing. Classifies every active company and every
board into one actionable bucket and prints a summary + the worklist.

    python3 scripts/fetch_health.py            # human report
    python3 scripts/fetch_health.py --json      # machine-readable
    python3 scripts/fetch_health.py --broken    # only the things to fix

Exit code is non-zero when anything is BROKEN, so it doubles as a cron/CI gate.

Buckets (companies):
  NEVER   — active but no fetch_strategy → never even attempted. Fix: assign ATS.
  BROKEN  — fetch errored (js_required / http_5xx / …) or failing for a while.
  STALE   — has a strategy, attempted, but no success in --stale-days days.
  EMPTY   — fetched cleanly, genuinely 0 open roles right now (healthy, low pri).
  OK      — producing vacancies.

Buckets (boards): OK / EMPTY (0 rows returned) / BROKEN (errored) / STALE /
UNKNOWN (no telemetry recorded yet — run the pipeline after migration 0015).
"""

import argparse
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from cli_help import wants_help


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fetch-health report for companies + boards")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    p.add_argument("--broken", action="store_true", help="Only show BROKEN / NEVER items")
    p.add_argument("--stale-days", type=int, default=10, help="Success older than this = STALE")
    return p


if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

from db_conn import get_conn
from database_supabase import is_fetch_error


def _table_columns(cur, table: str) -> set[str]:
    """Columns that actually exist — lets the report run before migration 0015."""
    try:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        cols = {r[0] for r in cur.fetchall()}
        if cols:
            return cols
    except Exception:
        pass
    # SQLite fallback
    try:
        cur.execute(f"PRAGMA table_info({table})")
        return {r[1] for r in cur.fetchall()}
    except Exception:
        return set()


def _age_days(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def classify_company(row: dict, stale_days: int) -> str:
    if not (row.get("fetch_strategy") or "").strip():
        return "NEVER"
    if is_fetch_error(row.get("fetch_status")) or (row.get("consecutive_failures") or 0) >= 3:
        return "BROKEN"
    if (row.get("vacancy_count") or 0) > 0:
        return "OK"
    # No vacancies. Genuinely-empty (no_data / render_ok_zero) is healthy unless
    # it has been failing to produce for a long time.
    success_age = _age_days(row.get("last_success"))
    attempt_age = _age_days(row.get("last_fetched"))
    if success_age is None and attempt_age is not None and attempt_age > stale_days:
        return "STALE"
    if success_age is not None and success_age > stale_days:
        return "STALE"
    return "EMPTY"


def classify_board(row: dict, stale_days: int) -> str:
    status = row.get("fetch_status")
    vc = row.get("vacancy_count")
    if is_fetch_error(status):
        return "BROKEN"
    if vc is None and status is None:
        return "UNKNOWN"  # no telemetry yet (pre-first-run after migration)
    attempt_age = _age_days(row.get("last_fetched"))
    if (vc or 0) == 0:
        return "EMPTY"
    if attempt_age is not None and attempt_age > stale_days:
        return "STALE"
    return "OK"


ORDER = ["BROKEN", "NEVER", "STALE", "EMPTY", "UNKNOWN", "OK"]


def collect(stale_days: int) -> dict:
    conn = get_conn()
    cur = conn.cursor()

    ccols = _table_columns(cur, "company")
    have = lambda c: c if c in ccols else "NULL"  # noqa: E731
    cur.execute(
        f"""SELECT canonical_name, COALESCE(fetch_strategy,''), fetch_status,
                   COALESCE(vacancy_count,0), last_fetched, {have('last_success')},
                   COALESCE({have('consecutive_failures')},0), {have('fetch_error')}
            FROM company WHERE status='active' ORDER BY canonical_name"""
    )
    companies = []
    for r in cur.fetchall():
        row = dict(
            zip(
                ["name", "fetch_strategy", "fetch_status", "vacancy_count",
                 "last_fetched", "last_success", "consecutive_failures", "fetch_error"],
                r,
            )
        )
        row["bucket"] = classify_company(row, stale_days)
        companies.append(row)

    bcols = _table_columns(cur, "board")
    bhave = lambda c: c if c in bcols else "NULL"  # noqa: E731
    cur.execute(
        f"""SELECT id, COALESCE(enabled,{'false' if 'enabled' in bcols else '0'}),
                   strategy, last_fetched, {bhave('vacancy_count')},
                   {bhave('fetch_status')}, {bhave('last_error')}
            FROM board ORDER BY id"""
    )
    boards = []
    for r in cur.fetchall():
        row = dict(
            zip(
                ["id", "enabled", "strategy", "last_fetched", "vacancy_count",
                 "fetch_status", "last_error"],
                r,
            )
        )
        row["bucket"] = classify_board(row, stale_days) if row["enabled"] else "OFF"
        boards.append(row)

    return {"companies": companies, "boards": boards}


def _counts(items) -> dict:
    out = {}
    for it in items:
        out[it["bucket"]] = out.get(it["bucket"], 0) + 1
    return out


def print_report(data: dict, only_broken: bool, stale_days: int) -> None:
    companies, boards = data["companies"], data["boards"]
    ccount = _counts(companies)
    print("=" * 68)
    print(f"  FETCH HEALTH — {len(companies)} active companies, {len(boards)} boards")
    print("=" * 68)
    print("\nCOMPANIES:  " + "   ".join(f"{b}={ccount.get(b,0)}" for b in ORDER if ccount.get(b)))

    show = ["BROKEN", "NEVER"] if only_broken else ORDER
    for bucket in show:
        rows = [c for c in companies if c["bucket"] == bucket]
        if not rows:
            continue
        print(f"\n── {bucket} ({len(rows)}) " + "─" * (52 - len(bucket)))
        for c in rows:
            detail = c["fetch_error"] or c["fetch_status"] or c["fetch_strategy"] or "—"
            streak = c["consecutive_failures"]
            tag = f" x{streak}" if streak else ""
            print(f"   {c['name'][:38]:40} {str(detail)[:24]:24}{tag}")

    bcount = _counts(boards)
    print("\n" + "=" * 68)
    print("BOARDS:  " + "   ".join(f"{b}={bcount.get(b,0)}" for b in list(bcount)))
    for b in boards:
        if only_broken and b["bucket"] not in ("BROKEN", "EMPTY"):
            continue
        vc = b["vacancy_count"]
        vc_s = "?" if vc is None else str(vc)
        print(f"   {b['id'][:22]:24} {b['bucket']:8} rows={vc_s:>5}  {b['strategy'] or ''}")


def main() -> int:
    args = build_parser().parse_args()
    data = collect(args.stale_days)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print_report(data, args.broken, args.stale_days)
    broken = sum(
        1 for c in data["companies"] if c["bucket"] in ("BROKEN",)
    ) + sum(1 for b in data["boards"] if b["bucket"] == "BROKEN")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
