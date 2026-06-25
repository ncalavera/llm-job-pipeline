#!/usr/bin/env python3
"""One-time data move: lift job boards out of the `company` table.

Board fetch state used to live on fake `company` rows whose canonical_name was
the board id (a16z, arbeitnow, …), polluting the dashboard's company list.
Migration 0002 added a `board` table; this script:

  1. Seeds the board catalog from config (ALL defined boards).
  2. Backfills board.last_fetched from the matching company rows.
  3. Deletes the now-redundant board-state company rows — but ONLY pure
     board-state rows (no ATS strategy, no vacancies, no score, no new). A
     canonical_name that collides with a real tracked company (e.g. a16z, which
     is also a real Greenhouse-tracked org with vacancies) is left untouched.

Idempotent: safe to run more than once. Run AFTER `python3 scripts/migrate.py`.

Usage:
    python3 scripts/migrate_boards_out_of_company.py            # apply
    python3 scripts/migrate_boards_out_of_company.py --dry-run  # preview only
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from database_supabase import get_conn, sync_boards

# Board ids that existed before the config was trimmed (Consider boards, the old
# split LinkedIn source). Included as deletion candidates; the safety guard
# below still protects any that turn out to be real companies.
LEGACY_BOARD_IDS = {"a16z", "sequoia", "linkedin_company"}

# Boards that ALSO got tracked as a real company (with an ATS strategy and
# vacancies) but are boards, not employers — delete the company row AND its
# vacancies unconditionally. a16z is the Consider VC portfolio board; the firm's
# own roles we'd captured via Greenhouse are all archived and not real targets.
FORCE_DELETE_IDS = {"a16z"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    args = parser.parse_args()

    all_boards = dict(config._ALL_JOB_BOARDS)
    candidate_ids = sorted(set(all_boards) | LEGACY_BOARD_IDS)

    conn = get_conn()
    cur = conn.cursor()

    # 1) Seed catalog
    if args.dry_run:
        print(f"[dry-run] would sync {len(all_boards)} boards into `board`")
    else:
        sync_boards(all_boards)
        print(f"Synced {len(all_boards)} boards into `board`.")

    # 2) Backfill last_fetched from the old company rows
    cur.execute(
        "SELECT canonical_name, last_fetched FROM company "
        "WHERE canonical_name = ANY(%s) AND last_fetched IS NOT NULL",
        (candidate_ids,),
    )
    company_dates = {name: dt for name, dt in cur.fetchall()}
    backfilled = 0
    for board_id, dt in company_dates.items():
        if board_id not in all_boards and board_id not in LEGACY_BOARD_IDS:
            continue
        # Only fill when the board row has no date yet (don't clobber fresher data).
        cur.execute("SELECT last_fetched FROM board WHERE id = %s", (board_id,))
        row = cur.fetchone()
        if row is None:
            continue
        if row[0] is None:
            if args.dry_run:
                print(f"[dry-run] would backfill board {board_id}.last_fetched = {dt}")
            else:
                cur.execute(
                    "UPDATE board SET last_fetched = %s WHERE id = %s",
                    (dt, board_id),
                )
            backfilled += 1
    if not args.dry_run:
        print(f"Backfilled last_fetched for {backfilled} boards.")

    # 3) Delete pure board-state company rows (conservative guard)
    cur.execute(
        """SELECT canonical_name FROM company
           WHERE canonical_name = ANY(%s)
             AND fetch_strategy IS NULL
             AND COALESCE(vacancy_count, 0) = 0
             AND COALESCE(new_count, 0) = 0
             AND alignment_score IS NULL""",
        (candidate_ids,),
    )
    to_delete = sorted(r[0] for r in cur.fetchall())

    cur.execute(
        "SELECT canonical_name FROM company WHERE canonical_name = ANY(%s)",
        (candidate_ids,),
    )
    present = {r[0] for r in cur.fetchall()}
    kept = sorted(present - set(to_delete))

    print(f"\nBoard-state company rows to delete ({len(to_delete)}): {to_delete}")
    if kept:
        print(f"Kept (look like real companies): {kept}")

    if to_delete and not args.dry_run:
        cur.execute(
            "DELETE FROM company WHERE canonical_name = ANY(%s)",
            (to_delete,),
        )
        print(f"Deleted {len(to_delete)} board-state company rows.")
    elif args.dry_run:
        print("[dry-run] no rows deleted")

    # Force-delete board-as-company rows (with their vacancies) — these are
    # boards, not employers.
    force_ids = sorted(FORCE_DELETE_IDS)
    cur.execute(
        "SELECT id, canonical_name FROM company WHERE canonical_name = ANY(%s)",
        (force_ids,),
    )
    force_rows = cur.fetchall()
    for company_id, name in force_rows:
        cur.execute("SELECT COUNT(*) FROM vacancy WHERE company_id = %s", (company_id,))
        vac_n = cur.fetchone()[0]
        print(f"Force-delete board-as-company '{name}': {vac_n} vacancies + 1 company row")
        if not args.dry_run:
            cur.execute("DELETE FROM vacancy WHERE company_id = %s", (company_id,))
            cur.execute("DELETE FROM company WHERE id = %s", (company_id,))

    cur.close()
    # DAL leaves commits to the caller (per AGENTS.md); persist unless dry-run.
    if not args.dry_run:
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
