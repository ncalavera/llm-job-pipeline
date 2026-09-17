#!/usr/bin/env python3
"""One-off pass: fill `vacancy.compensation` from `full_description` for rows
that already have posting text but no stored pay figure.

Uses the same deterministic extractor as the live enrich write path
(database_supabase.backfill_compensation_from_text) — COALESCE-only, never
overwrites a compensation value that is already set.

Usage:
    python3 scripts/backfill_compensation.py --dry-run [--sample N]
        Count how many target rows would be filled; print a random sample of
        proposed fills with the matched source sentence, for eyeballing.

    python3 scripts/backfill_compensation.py --ids id1,id2,id3 [--dry-run]
        Run (or preview) the backfill for exactly these vacancy ids.

    python3 scripts/backfill_compensation.py --apply --backup PATH
        Run the real pass over the target scope (status='unseen' OR
        screening->>'north_star'='true'), after writing a backup of every
        row's id + old compensation to PATH. Requires
        JOBSEARCH_ALLOW_PROD_WRITE=1.
"""

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database_supabase import (
    get_conn,
    _extract_compensation_from_description,
    backfill_compensation_from_text,
    _PAY_CUE_RE,
)

# Any row with posting text and no stored pay figure — used for --dry-run's
# "how many total" count and the eyeball sample (deliberately broader than
# the real-pass scope below).
_ALL_CANDIDATES_SQL = """
    SELECT id, title, full_description
    FROM vacancy
    WHERE full_description IS NOT NULL AND length(full_description) > 50
      AND (compensation IS NULL OR compensation = '')
"""

# The real pass only ever touches rows still worth showing to Nikita: unseen
# (not yet screened) or already flagged north_star (kept despite scoring,
# so a missing salary would otherwise never get fixed).
_TARGET_SCOPE_SQL = (
    _ALL_CANDIDATES_SQL
    + """
      AND (status = 'unseen' OR screening ->> 'north_star' = 'true')
"""
)


def _matched_sentence(text: str, value: str) -> str:
    """Best-effort: the sentence-ish snippet around the first pay cue, for
    human eyeballing — not re-deriving exactly where `value` came from."""
    m = _PAY_CUE_RE.search(text or "")
    if not m:
        return ""
    start = max(0, m.start() - 80)
    end = min(len(text), m.end() + 150)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _fetch_rows(cur, ids=None, scope="target"):
    if ids:
        cur.execute(
            "SELECT id, title, full_description FROM vacancy WHERE id = ANY(%s::uuid[])",
            (list(ids),),
        )
        return cur.fetchall()
    sql = _ALL_CANDIDATES_SQL if scope == "all" else _TARGET_SCOPE_SQL
    cur.execute(sql)
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="comma-separated vacancy ids")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true", help="run the real pass and commit")
    ap.add_argument("--sample", type=int, default=25, help="dry-run eyeball sample size")
    ap.add_argument("--backup", help="path to write id+old-compensation backup JSON before --apply")
    args = ap.parse_args()

    if args.apply and not args.dry_run and not os.environ.get("JOBSEARCH_ALLOW_PROD_WRITE"):
        print("ERROR: --apply requires JOBSEARCH_ALLOW_PROD_WRITE=1", file=sys.stderr)
        sys.exit(1)

    ids = [i.strip() for i in args.ids.split(",")] if args.ids else None
    conn = get_conn()
    cur = conn.cursor()

    scope = "all" if (args.dry_run and not ids) else "target"
    rows = _fetch_rows(cur, ids=ids, scope=scope)
    print(f"Candidate rows: {len(rows)} (scope={'explicit ids' if ids else scope})")

    proposals = []
    for vid, title, desc in rows:
        value = _extract_compensation_from_description(desc or "")
        if value:
            proposals.append((vid, title, value, _matched_sentence(desc, value)))

    print(f"Would fill: {len(proposals)}/{len(rows)}")

    if args.dry_run:
        sample = (
            proposals if len(proposals) <= args.sample else random.sample(proposals, args.sample)
        )
        for vid, title, value, sentence in sample:
            print(f"  {vid}  {title[:45]:45s} -> {value!r}")
            print(f"      ...{sentence}...")
        return

    if ids:
        # Explicit small slice (the 3-row live test): show before -> after.
        before = {vid: comp for vid, comp in _current_compensation(cur, ids)}
        for vid, title, desc in rows:
            wrote = backfill_compensation_from_text(cur, vid, desc or "")
            conn.commit()
            after = _current_compensation(cur, [vid])[0][1]
            print(f"{vid}  {title}")
            print(f"  before: {before.get(vid)!r}")
            print(f"  after:  {after!r}  (wrote={wrote})")
        return

    if not args.apply:
        print("Nothing to do — pass --dry-run, --ids, or --apply.")
        return

    # --apply: real pass over the target scope. Backup first.
    if args.backup:
        comp_by_id = dict(_current_compensation(cur, [vid for vid, _, _ in rows]))
        backup_data = [{"id": vid, "compensation": comp_by_id.get(vid)} for vid, _, _ in rows]
        Path(args.backup).parent.mkdir(parents=True, exist_ok=True)
        Path(args.backup).write_text(json.dumps(backup_data, indent=2))
        print(f"Backup written: {args.backup} ({len(backup_data)} rows)")

    filled = 0
    for vid, title, desc in rows:
        if backfill_compensation_from_text(cur, vid, desc or ""):
            filled += 1
    conn.commit()
    print(f"Applied: {filled}/{len(rows)} rows filled.")


def _current_compensation(cur, ids):
    cur.execute("SELECT id, compensation FROM vacancy WHERE id = ANY(%s::uuid[])", (list(ids),))
    return cur.fetchall()


if __name__ == "__main__":
    main()
