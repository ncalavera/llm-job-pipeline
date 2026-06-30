#!/usr/bin/env python3
"""One-off backfill: re-score every ACTIVE company with the current prompt.

After the company-scoring prompt is rewritten (pure desirability, KTD3), the
existing active companies still carry scores from the old, role-conflated
rubric. This re-runs scoring for them so alignment_score reflects the new five
dimensions, then the dashboard snapshot is regenerated so applyable counts and
sorts follow.

Manual / one-off — NOT wired into the autonomous Mon/Thu routine. Reuses the
normal subagent scoring backend (prepare → subagents → save):

    # 0. inspect the before-state (and spot-check targets, e.g. GiveWell)
    python3 scripts/backfill_company_scores.py --scores

    # 1. emit scoring payloads for ALL active companies (regardless of having a
    #    score already — the --company filter bypasses the "unscored only" gate)
    python3 scripts/backfill_company_scores.py > /tmp/backfill.jobs.json

    # 2. an Opus subagent scores each payload → /tmp/backfill.done.json
    # 3. persist the new scores
    python3 scripts/score_companies.py --save < /tmp/backfill.done.json

    # 4. recompute applyable counts + sorts
    python3 scripts/fetch_vacancies.py --report-only   # or regenerate snapshot

    # 5. confirm the before/after delta (GiveWell should rise well above 38)
    python3 scripts/backfill_company_scores.py --scores

Companies missing from the Firecrawl scrape cache are skipped (they need a fresh
fetch_companies.py enrichment first); the count is reported so nothing is hidden.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _active_company_names() -> list[str]:
    from db_conn import get_conn
    cur = get_conn().cursor()
    cur.execute(
        "SELECT canonical_name FROM company "
        "WHERE status = 'active' AND website IS NOT NULL AND website != '' "
        "ORDER BY canonical_name"
    )
    names = [r[0] for r in cur.fetchall()]
    cur.close()
    return names


def _active_scores() -> list[tuple]:
    from db_conn import get_conn
    cur = get_conn().cursor()
    cur.execute(
        "SELECT canonical_name, alignment_score FROM company "
        "WHERE status = 'active' ORDER BY alignment_score DESC NULLS LAST, "
        "canonical_name"
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def cmd_scores(_args):
    """Print active companies' current alignment_score (before/after snapshot)."""
    rows = _active_scores()
    scored = [r for r in rows if r[1] is not None]
    print(f"Active companies: {len(rows)} ({len(scored)} scored)", file=sys.stderr)
    for name, score in rows:
        print(f"  {('-' if score is None else score):>4}  {name}")


def cmd_prepare(args):
    """Emit scoring payloads (stdout JSON) for all active companies."""
    import score_companies as sc

    names = _active_company_names()
    if not names:
        print("No active companies with a website.", file=sys.stderr)
        print("[]")
        return

    # Snapshot the before-state for the eventual spot-check.
    print(f"Backfill: {len(names)} active companies to re-score.", file=sys.stderr)

    scrape_cache = sc._load_scrape_cache()
    system_prompt = sc._get_system_prompt()
    strategy_context = sc._load_strategy_context()
    companies = sc._load_companies(company_filter=",".join(names), limit=args.limit)

    output, skipped = [], 0
    for c in companies:
        user_msg = sc._build_user_msg(c, scrape_cache, strategy_context)
        if user_msg is None:
            skipped += 1
            continue
        output.append({
            "payload_kind": "company",
            "id": str(c["id"]),
            "canonical_name": c["canonical_name"],
            "url": (c.get("website") or "").strip(),
            "system_prompt": system_prompt,
            "user_msg": user_msg,
        })
    if skipped:
        print(f"  Skipped {skipped} not in scrape cache (run fetch_companies.py "
              f"to enrich them first).", file=sys.stderr)
    print(f"Prepared {len(output)} companies for scoring.", file=sys.stderr)
    json.dump(output, sys.stdout, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", action="store_true",
                        help="print active companies' current alignment_score")
    parser.add_argument("--limit", type=int,
                        help="cap how many active companies to prepare")
    args = parser.parse_args()
    if args.scores:
        cmd_scores(args)
    else:
        cmd_prepare(args)


if __name__ == "__main__":
    main()
