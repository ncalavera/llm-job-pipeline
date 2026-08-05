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

Two protective signals mirror the save path so a genuinely open second role is
never silently merged away:

  * URL guard — two rows carrying different non-empty apply URLs are distinct
    reqs and are NOT clustered together, even if their titles normalize alike.
  * both-live guard — a cluster where two or more rows are still live (neither
    archived/expiring AND seen in the company's latest fetch) is NOT an
    over-time rename; both roles may be open. It is flagged MANUAL REVIEW and
    left untouched instead of auto-collapsed.

Safe by default: ``--dry-run`` (the default) only PRINTS what would happen, and
names every loser it would archive and why the cluster reads as a rename. It
writes nothing without an explicit ``--apply``. Point it at a throwaway SQLite
DB with ``JOBSEARCH_DB_PATH`` and no ``SUPABASE_DB_URL`` to rehearse.

Canonical (survivor) selection per cluster, in order:
  1. the most-decided status (applied > to_apply > liked > … > unseen > archived)
     — so a variant you already applied to or passed is the one that survives;
  2. the highest llm_score;
  3. the oldest first_seen (the most established row).

The survivor then absorbs the cluster's richest data — the longest description,
the max score, the union of locations, the earliest first_seen / latest
last_seen. On ``--apply`` the losing rows are first written to a JSON archive
under ``VACANCIES_DIR/archive`` (same atomic temp-file → os.replace mechanism
and record shape as database_supabase.archive_vacancies) and only then deleted,
so no row is lost without a durable on-disk copy. Losers are NOT tombstoned: a
future fetch of any variant re-merges onto the survivor through the save index.

Usage::

    python3 scripts/dedup_sweep.py                 # dry-run (default), prints pairs
    python3 scripts/dedup_sweep.py --apply         # archive losers + merge + delete
    python3 scripts/dedup_sweep.py --limit 50      # cap clusters shown/applied
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_backend import Json, RealDictCursor, IS_SQLITE, print_backend_banner  # noqa: E402
from database_supabase import (  # noqa: E402
    VACANCIES_DIR,
    _normalize_title_strong,
    _titles_equal_sans_stopwords,
    make_normalized_id,
    description_fingerprint,
    normalize_apply_url,
    serialize_vacancy_rows,
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

# A row in one of these states is no longer "live" in the source listing.
_GONE_STATUSES = frozenset({"archived", "expiring"})


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


def _urls(row):
    """Non-empty apply/job URLs (normalized) recorded on a row's locations[].

    Normalization strips tracking query params (utm_* etc.), so the same req
    linked from two boards with different decorations compares equal."""
    urls = set()
    for loc in row.get("locations") or []:
        u = normalize_apply_url(loc.get("url"))
        if u:
            urls.add(u)
    return urls


def _url_compatible(a, b):
    """False only when both rows carry non-empty apply URLs that do not overlap
    — that pair is two distinct reqs and must not be clustered together."""
    ua, ub = _urls(a), _urls(b)
    if ua and ub and ua.isdisjoint(ub):
        return False
    return True


def _cluster(rows):
    """Return clusters (lists of >= 2 rows) that are cross-variant duplicates.

    Rows are grouped within a company: any two rows sharing a normalized-title
    key OR a description fingerprint are unioned into the same cluster — unless
    the URL guard says they are distinct reqs.
    """
    by_company: dict = {}
    for r in rows:
        by_company.setdefault(r["company_id"], []).append(r)

    clusters = []
    for company_rows in by_company.values():
        by_id = {r["id"]: r for r in company_rows}
        uf = _Union()
        norm_seen: dict = {}
        desc_seen: dict = {}
        url_seen: dict = {}
        for r in company_rows:
            uf.find(r["id"])  # ensure the row is a node even if it stays a singleton
            nkey = make_normalized_id(r["org"], r["title"] or "")
            prev = norm_seen.get(nkey)
            if prev is None:
                norm_seen[nkey] = r["id"]
            elif _url_compatible(by_id[prev], r):
                uf.union(r["id"], prev)
            fp = description_fingerprint(r.get("full_description"))
            if fp:
                prev_d = desc_seen.get(fp)
                if prev_d is None:
                    desc_seen[fp] = r["id"]
                elif _url_compatible(by_id[prev_d], r):
                    uf.union(r["id"], prev_d)
            # Shared normalized apply URL = one requisition — union even when
            # neither the title key nor the description fingerprint matches (a
            # board's stub body vs the ATS's full JD). Mirrors the save-path
            # same-URL merge: titles must contain each other or differ only by
            # connective words, so orgs stamping one generic careers URL on
            # every role never collapse together.
            for u in _urls(r):
                prev_u = url_seen.get(u)
                if prev_u is None:
                    url_seen[u] = r["id"]
                    continue
                ta = _normalize_title_strong(r.get("title") or "")
                tb = _normalize_title_strong(by_id[prev_u].get("title") or "")
                if ta and tb and (ta in tb or tb in ta or _titles_equal_sans_stopwords(ta, tb)):
                    uf.union(r["id"], prev_u)

        groups: dict = {}
        for r in company_rows:
            groups.setdefault(uf.find(r["id"]), []).append(by_id[r["id"]])
        clusters.extend(g for g in groups.values() if len(g) > 1)
    return clusters


def _live_rows(cluster):
    """Rows still live in the source: active status AND seen in the cluster's
    latest fetch (last_seen == the max last_seen across the cluster)."""
    latest = max((str(r.get("last_seen") or "") for r in cluster), default="")
    if not latest:
        return []
    return [
        r
        for r in cluster
        if r.get("status") not in _GONE_STATUSES and str(r.get("last_seen") or "") == latest
    ]


def _needs_manual_review(cluster):
    """A cluster where >= 2 rows are still live is NOT an over-time rename —
    both roles may be genuinely open right now — so it is left for a human
    instead of auto-collapsed (mirrors the save-path batch-alive guard).

    Exception: when every live row shares a common normalized apply URL, that
    URL identifies ONE requisition (an apply link points at a single req), so
    the pair is a same-req double listing, not two open roles — collapse it."""
    live = _live_rows(cluster)
    if len(live) < 2:
        return False
    url_sets = [_urls(r) for r in live]
    if all(url_sets) and set.intersection(*url_sets):
        return False
    return True


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


def _archive_losers(all_losers):
    """Serialize every loser to a temp JSON file under VACANCIES_DIR/archive.

    Returns (tmp_path, final_path). Mirrors archive_vacancies: the temp file is
    written BEFORE any DELETE (a disk/encoding failure raises here, before the
    DB is touched), and the caller os.replace()s it onto the final name only
    AFTER the merge+delete is committed — so the artifact never describes a
    removal a rollback undid.
    """
    archive_dir = VACANCIES_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    final_path = archive_dir / f"dedup_sweep_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    tmp_path = final_path.with_name(final_path.name + ".tmp")
    payload = {
        "archived_at": datetime.now().isoformat(),
        "reason": "dedup_sweep_merged",
        "count": len(all_losers),
        "vacancies": serialize_vacancy_rows(all_losers),
    }
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path, final_path


def _print_manual(clusters):
    """Report clusters left for a human (both-live, not a rename)."""
    for i, cluster in enumerate(clusters, 1):
        live = _live_rows(cluster)
        print(
            f"[M{i}] {cluster[0]['org']}  — MANUAL REVIEW: {len(live)} rows still "
            f"live in the latest fetch (not an over-time rename, left untouched)"
        )
        for r in cluster:
            tag = "live " if r in live else "stale"
            print(f'    {tag} {r["status"]:<10} "{r["title"]}"  last seen {r.get("last_seen")}')


def _print_collapsible(merges):
    """Report clusters that would collapse, naming each loser and why."""
    for i, (survivor, losers) in enumerate(merges, 1):
        cluster = [survivor] + losers
        n_live = len(_live_rows(cluster))
        print(
            f"[{i}] {survivor['org']}  — collapse ({n_live} of {len(cluster)} rows "
            f"still live → the rest read as prior titles of the same role)"
        )
        print(f'    KEEP     {survivor["status"]:<10} "{survivor["title"]}"  (survivor)')
        for loser in losers:
            print(
                f'    ARCHIVE  {loser["status"]:<10} "{loser["title"]}"'
                f"  last seen {loser.get('last_seen')} → folds in, inherits "
                f"'{survivor['status']}'"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="archive losers + merge + delete (default is a read-only dry-run)",
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

    manual = [c for c in clusters if _needs_manual_review(c)]
    merges = [_pick_survivor(c) for c in clusters if not _needs_manual_review(c)]

    if manual:
        _print_manual(manual)
        print()
    _print_collapsible(merges)

    total_losers = sum(len(losers) for _, losers in merges)
    print(
        f"\n{len(merges)} duplicate cluster(s), {total_losers} row(s) "
        f"{'archived + merged + deleted' if args.apply else 'would be collapsed'}"
        f"; {len(manual)} cluster(s) flagged for manual review."
    )

    if not args.apply:
        print("Dry-run only — re-run with --apply to write.")
        return 0

    if not merges:
        print("Nothing to collapse.")
        return 0

    all_losers = [loser for _, losers in merges for loser in losers]
    # (1) archive losers to a temp file BEFORE any delete.
    tmp_path, final_path = _archive_losers(all_losers)
    # (2) merge + delete, then (3) commit — the durable record of what was removed.
    for survivor, losers in merges:
        _apply_merge(survivor, losers)
    get_conn().commit()
    # (4) publish the archive under its final name only after the commit.
    try:
        os.replace(tmp_path, final_path)
    except OSError:
        print(
            f"  ERROR: archive rename failed AFTER the delete was committed.\n"
            f"  The archived payload is preserved at: {tmp_path}",
            file=sys.stderr,
        )
        raise
    print(f"Committed. Archived {len(all_losers)} loser row(s) → {final_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
