"""One-off sweep that collapses cross-variant duplicate vacancies already in
the database.

The save path (database_supabase.save_vacancies / save_board_vacancies) stops
NEW duplicates from forming: a role re-listed under a renamed title (a seniority
word added/removed), a re-punctuated title, or a same-company posting in another
language with the same description body all merge onto the live row. This script
cleans up duplicates that were stored BEFORE that logic existed.

It groups each company's rows into clusters — rows sharing a normalized-title
key (make_normalized_id) or a description fingerprint (description_fingerprint)
land in one cluster — then keeps one canonical row per cluster and folds the
rest into it.

Safe by default: ``--dry-run`` (the default) only PRINTS what would happen. It
writes nothing without an explicit ``--apply``. Point it at a throwaway SQLite
DB with ``JOBSEARCH_DB_PATH`` and no ``SUPABASE_DB_URL`` to rehearse.

Canonical (survivor) selection per cluster, in order:
  1. the most-decided status (applied > to_apply > liked > … > unseen > archived)
     — so a variant you already applied to or passed is the one that survives;
  2. the highest llm_score;
  3. the oldest first_seen (the most established row).

The survivor then absorbs the cluster's richest data — the longest description,
the max score, the union of locations, the earliest first_seen / latest
last_seen — and the losing rows are deleted. Losers are NOT tombstoned: a future
fetch of any variant re-merges onto the survivor through the save-path index.

Usage::

    python3 scripts/dedup_sweep.py                 # dry-run (default), prints pairs
    python3 scripts/dedup_sweep.py --apply         # actually merge + delete
    python3 scripts/dedup_sweep.py --limit 50      # cap clusters shown/applied
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_backend import Json, RealDictCursor, IS_SQLITE, print_backend_banner  # noqa: E402
from database_supabase import (  # noqa: E402
    make_normalized_id,
    description_fingerprint,
    get_conn,
)

# Higher = more worth keeping. A user decision (applied/passed/…) always beats
# an undecided 'unseen', so the survivor inherits the decision (the point of the
# whole exercise: a renamed copy must not resurface as unseen).
_STATUS_RANK = {
    "applied": 100,
    "to_apply": 90,
    "liked": 80,
    "to_research": 70,
    "to_network": 60,
    "passed": 50,
    "skipped": 40,
    "expiring": 30,
    "unseen": 20,
    "archived": 10,
}


class _Union:
    """Minimal union-find over row ids."""

    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _load_rows():
    """All vacancies with their company canonical name, as dict rows."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT v.id, v.dedup_hash, v.title, v.full_description, v.status, "
        "v.llm_score, v.locations, v.first_seen, v.last_seen, v.deadline, "
        "v.company_id, c.canonical_name AS org "
        "FROM vacancy v JOIN company c ON v.company_id = c.id"
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _cluster(rows):
    """Return clusters (lists of >= 2 rows) that are cross-variant duplicates.

    Rows are grouped within a company: any two rows sharing a normalized-title
    key OR a description fingerprint are unioned into the same cluster.
    """
    by_company: dict = {}
    for r in rows:
        by_company.setdefault(r["company_id"], []).append(r)

    clusters = []
    for company_rows in by_company.values():
        uf = _Union()
        norm_seen: dict = {}
        desc_seen: dict = {}
        for r in company_rows:
            uf.find(r["id"])  # ensure the row is a node even if it stays a singleton
            nkey = make_normalized_id(r["org"], r["title"] or "")
            if nkey in norm_seen:
                uf.union(r["id"], norm_seen[nkey])
            else:
                norm_seen[nkey] = r["id"]
            fp = description_fingerprint(r.get("full_description"))
            if fp:
                if fp in desc_seen:
                    uf.union(r["id"], desc_seen[fp])
                else:
                    desc_seen[fp] = r["id"]

        groups: dict = {}
        by_id = {r["id"]: r for r in company_rows}
        for r in company_rows:
            groups.setdefault(uf.find(r["id"]), []).append(by_id[r["id"]])
        clusters.extend(g for g in groups.values() if len(g) > 1)
    return clusters


def _pick_survivor(cluster):
    """Return (survivor, losers). Highest status rank, then score, then oldest."""
    ordered = sorted(
        cluster,
        key=lambda r: (
            _STATUS_RANK.get(r["status"], 0),
            r["llm_score"] if r["llm_score"] is not None else -1,
            # first_seen ascending → older is "smaller"; invert so it sorts last
            # in a max-first ordering below.
        ),
        reverse=True,
    )
    # Break ties (same rank+score) deterministically toward the oldest first_seen.
    top_rank = (
        _STATUS_RANK.get(ordered[0]["status"], 0),
        ordered[0]["llm_score"] if ordered[0]["llm_score"] is not None else -1,
    )
    tied = [
        r
        for r in ordered
        if (
            _STATUS_RANK.get(r["status"], 0),
            r["llm_score"] if r["llm_score"] is not None else -1,
        )
        == top_rank
    ]
    survivor = min(tied, key=lambda r: str(r.get("first_seen") or ""))
    losers = [r for r in cluster if r["id"] != survivor["id"]]
    return survivor, losers


def _loc_key(loc):
    return loc.get("city") or loc.get("country") or loc.get("work_mode") or ""


def _merge_fields(survivor, losers):
    """Compute the merged field set the survivor should carry."""
    cluster = [survivor] + losers
    # Longest description across the cluster.
    best_desc = max((r.get("full_description") or "" for r in cluster), key=len)
    # Max non-null score.
    scores = [r["llm_score"] for r in cluster if r["llm_score"] is not None]
    best_score = max(scores) if scores else None
    # Union of locations by loc_key, survivor first.
    merged_locs = []
    seen_keys = set()
    for r in cluster:
        for loc in r.get("locations") or []:
            k = _loc_key(loc)
            if k not in seen_keys:
                seen_keys.add(k)
                merged_locs.append(loc)
    first_seen = min(
        (str(r.get("first_seen") or "") for r in cluster if r.get("first_seen")), default=None
    )
    last_seen = max(
        (str(r.get("last_seen") or "") for r in cluster if r.get("last_seen")), default=None
    )
    deadline = next((r["deadline"] for r in cluster if r.get("deadline")), None)
    return {
        "full_description": best_desc,
        "llm_score": best_score,
        "locations": merged_locs,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "deadline": deadline,
    }


def _apply_merge(survivor, losers):
    """Fold losers into the survivor and delete them. Caller commits."""
    fields = _merge_fields(survivor, losers)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE vacancy SET full_description = %s, llm_score = %s, locations = %s, "
        "first_seen = %s, last_seen = %s, deadline = %s WHERE id = %s",
        (
            fields["full_description"],
            fields["llm_score"],
            Json(fields["locations"]),
            fields["first_seen"],
            fields["last_seen"],
            fields["deadline"],
            survivor["id"],
        ),
    )
    loser_ids = [loser["id"] for loser in losers]
    if IS_SQLITE:
        placeholders = ",".join(["%s"] * len(loser_ids))
        cur.execute(f"DELETE FROM vacancy WHERE id IN ({placeholders})", loser_ids)
    else:
        cur.execute("DELETE FROM vacancy WHERE id = ANY(%s::uuid[])", (loser_ids,))
    cur.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually merge + delete (default is a read-only dry-run)",
    )
    ap.add_argument("--limit", type=int, default=None, help="cap the number of clusters processed")
    args = ap.parse_args()

    print_backend_banner()
    mode = "APPLY" if args.apply else "DRY-RUN (no writes)"
    print(f"dedup_sweep: {mode}\n")

    rows = _load_rows()
    clusters = _cluster(rows)
    if args.limit is not None:
        clusters = clusters[: args.limit]

    if not clusters:
        print("No cross-variant duplicates found. Nothing to do.")
        return 0

    total_losers = 0
    for i, cluster in enumerate(clusters, 1):
        survivor, losers = _pick_survivor(cluster)
        total_losers += len(losers)
        org = survivor["org"]
        print(f"[{i}] {org}")
        print(f'    KEEP    {survivor["status"]:<10} "{survivor["title"]}"')
        for loser in losers:
            print(
                f'    collapse {loser["status"]:<10} "{loser["title"]}"'
                f"  -> inherits '{survivor['status']}'"
            )
        if args.apply:
            _apply_merge(survivor, losers)

    print(
        f"\n{len(clusters)} duplicate cluster(s), {total_losers} row(s) "
        f"{'merged + deleted' if args.apply else 'would be collapsed'}."
    )
    if args.apply:
        get_conn().commit()
        print("Committed.")
    else:
        print("Dry-run only — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
