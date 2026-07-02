#!/usr/bin/env python3
"""Golden-set scoring-quality check — build a labelled set and measure how well
the CURRENT vacancy-scoring prompt agrees with the user's own verdicts.

Why this exists
---------------
The scoring prompt is the pipeline's main judging surface, but nothing measured
whether its scores match what the user actually wants. This tool closes that
loop the cheap way (method: Hamel Husain's "Critique Shadowing",
https://hamel.dev/blog/posts/llm-judge/): a small, user-labelled set of
vacancies (binary fits / doesn't-fit + a short reason), scored with the live
prompt, reduced to ONE headline agreement number plus a readable list of
disagreements that tells you what to fix.

The set is PERSONAL data (real titles, orgs, the user's reasons) and lives in a
gitignored path (``evals/`` by default, override with ``GOLDEN_SET_DIR``). It is
never committed.

Two ways to build the set, both append-only and version-stamped:
  * ``seed``          — from verdict history already in the database (the fast
                        path: your likes/passes ARE labels).
  * ``template`` +
    ``add-template``  — hand-label your own vacancies blind (without seeing the
                        model score), for cases the verdict history doesn't cover.

Scoring reuses the pipeline's own contract — no LLM call happens in Python, the
orchestrating agent is the scorer (see AGENTS.md):
  1. ``emit``    prints one scoring payload per vacancy to stdout.
  2. the agent scores EACH payload independently (one request per vacancy —
     batching is banned repo-wide, it over-scores) and collects [{id, score}].
  3. ``measure`` reads those scores from stdin and prints the agreement number,
     precision/recall at the score threshold, and the disagreement list.

Storage & versioning
--------------------
One append-only JSONL file (``evals/golden_set.jsonl``). Old lines are NEVER
edited; each labelling batch that adds rows stamps them with the next integer
``version``. ``--version N`` freezes a measurement to the set as it stood at
version N (rows with ``version <= N``). Re-seeding is idempotent: a vacancy
already in the set (keyed by its stable ``dedup_hash``) is skipped, never
relabelled.

Works in both modes: it reads through the DAL, so full mode (Postgres) and
simple mode (SQLite) behave identically. It needs a populated database with some
verdicts — on an empty database ``seed`` explains that and exits cleanly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------------------------------------------------------------------------
# Label model — the binary verdict baskets (see CONCEPTS.md "Liked basket").
# ---------------------------------------------------------------------------

#: Statuses that mean "the user wants this role" → label "fit".
FIT_STATUSES = frozenset({"liked", "to_apply", "to_research", "to_network", "applied"})
#: Statuses that mean "the user declined this role" → label "nofit".
NOFIT_STATUSES = frozenset({"passed", "skipped"})

LABEL_FIT = "fit"
LABEL_NOFIT = "nofit"

#: Default fit decision boundary. A model score at/above this is the model
#: predicting "fit". APPLYABLE_SCORE (60) is the pipeline's own neutral notion of
#: "a role worth acting on", so it is the natural boundary here too. Overridable
#: with --threshold or the same env var the pipeline reads.
DEFAULT_THRESHOLD = 60


def _default_dir() -> Path:
    """Where the (personal, gitignored) golden set lives. ``evals/`` by default;
    override with GOLDEN_SET_DIR (used by tests)."""
    import os

    env = os.environ.get("GOLDEN_SET_DIR")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent / "evals"


def _store_path(base_dir: Path | None = None) -> Path:
    return (base_dir or _default_dir()) / "golden_set.jsonl"


def _template_path(base_dir: Path | None = None) -> Path:
    return (base_dir or _default_dir()) / "label_template.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Pure helpers — labelling & auto-expiry heuristic (unit-tested without a DB)
# ---------------------------------------------------------------------------


def verdict_label(status: str | None) -> str | None:
    """Map a stored vacancy status to a binary golden label, or None if the
    status carries no user verdict (unseen / expiring / archived)."""
    if status in FIT_STATUSES:
        return LABEL_FIT
    if status in NOFIT_STATUSES:
        return LABEL_NOFIT
    return None


def is_probable_auto_expiry(vac: dict, today: str | None = None) -> bool:
    """True for a ``passed`` vacancy whose deadline is in the past — almost
    certainly auto-passed on expiry (pass_expired_vacancies), NOT a fit verdict.
    Excluding these keeps nofit labels honest: a high-scoring role that merely
    lapsed should not read as a model over-score."""
    if vac.get("status") != "passed":
        return False
    deadline = vac.get("deadline")
    if not deadline:
        return False
    today = today or datetime.now(timezone.utc).date().isoformat()
    return str(deadline) < today


def _has_description(vac: dict) -> bool:
    return bool((vac.get("full_description") or vac.get("snippet") or "").strip())


def _sort_key(vac: dict):
    """Most-recent verdict first: status_updated_at, then created_at."""
    return (vac.get("status_updated_at") or "", vac.get("created_at") or "")


def _freeze_vacancy(vac: dict) -> dict:
    """The minimal, frozen scoring input stored with a golden label, so the
    measurement is reproducible even if the source row later changes."""
    return {
        "org": vac.get("org", ""),
        "title": vac.get("title", ""),
        "tier": vac.get("tier") or vac.get("company_tier"),
        "locations": vac.get("locations", []),
        "full_description": vac.get("full_description") or vac.get("snippet") or "",
    }


def make_record(vac: dict, label: str, reason: str, source: str) -> dict:
    """Build a golden record (without version/labelled_at — set at append)."""
    return {
        "id": vac.get("dedup_hash") or vac.get("id"),
        "source": source,
        "label": label,
        "reason": reason,
        "vacancy": _freeze_vacancy(vac),
    }


def select_seed_records(
    vacs: list[dict], *, limit: int, balance: bool = True, today: str | None = None
) -> list[dict]:
    """Pick vacancies with a usable verdict and turn them into golden records.

    Usable = has a fit/nofit verdict, is scored (so the row is a real judgement,
    not a stub), has a description, and is not a probable auto-expiry pass.

    balance=True (default): fit verdicts are scarce, so take ALL of them first,
    then fill the rest of the budget with the most-recent nofit verdicts. This
    guarantees positives in the set, without which precision/recall are
    meaningless. balance=False: strict most-recent-first across both classes.
    """
    fit_pool, nofit_pool = [], []
    for v in vacs:
        label = verdict_label(v.get("status"))
        if label is None or not _has_description(v):
            continue
        if v.get("llm_score") is None:
            continue
        if is_probable_auto_expiry(v, today):
            continue
        (fit_pool if label == LABEL_FIT else nofit_pool).append(v)

    fit_pool.sort(key=_sort_key, reverse=True)
    nofit_pool.sort(key=_sort_key, reverse=True)

    if balance:
        chosen = list(fit_pool[:limit])
        chosen += nofit_pool[: max(0, limit - len(chosen))]
    else:
        chosen = sorted(fit_pool + nofit_pool, key=_sort_key, reverse=True)[:limit]

    records = []
    for v in chosen:
        label = verdict_label(v.get("status"))
        records.append(
            make_record(v, label, f"user verdict: {v.get('status')}", f"seed:{v.get('status')}")
        )
    return records


# ---------------------------------------------------------------------------
# Store — append-only, version-stamped JSONL
# ---------------------------------------------------------------------------


def load_records(path: Path, *, version: int | None = None) -> list[dict]:
    """Read all golden records. ``version=N`` freezes to rows with version <= N."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if version is not None and rec.get("version", 0) > version:
            continue
        rows.append(rec)
    return rows


def current_version(path: Path) -> int:
    rows = load_records(path)
    return max((r.get("version", 0) for r in rows), default=0)


def append_records(path: Path, new_records: list[dict]) -> dict:
    """Append records that are not already present (by id). Append-only: existing
    lines are never rewritten. Newly added rows are stamped with the next
    integer version and the current timestamp. Returns a small summary."""
    existing = load_records(path)
    known_ids = {r.get("id") for r in existing}
    version = max((r.get("version", 0) for r in existing), default=0)

    to_add = []
    skipped = 0
    seen_now: set = set()
    for rec in new_records:
        rid = rec.get("id")
        if not rid or rid in known_ids or rid in seen_now:
            skipped += 1
            continue
        seen_now.add(rid)
        to_add.append(rec)

    if not to_add:
        return {"added": 0, "skipped": skipped, "version": version, "total": len(existing)}

    new_version = version + 1
    stamp = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in to_add:
            rec = {**rec, "version": new_version, "labeled_at": stamp}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return {
        "added": len(to_add),
        "skipped": skipped,
        "version": new_version,
        "total": len(existing) + len(to_add),
    }


# ---------------------------------------------------------------------------
# Metrics — agreement + threshold precision/recall + disagreements
# ---------------------------------------------------------------------------


def compute_metrics(records: list[dict], scores: dict[str, int], threshold: int) -> dict:
    """Join golden labels with model scores and compute the quality report.

    ``scores`` maps record id → model score. "fit" is the positive class. A model
    score >= threshold is the model predicting "fit".
    """
    matched = [(r, scores[r["id"]]) for r in records if r["id"] in scores]
    missing = [r["id"] for r in records if r["id"] not in scores]

    tp = fp = fn = tn = 0
    agree = 0
    disagreements = []
    for rec, score in matched:
        model_label = LABEL_FIT if score >= threshold else LABEL_NOFIT
        user_label = rec["label"]
        if model_label == user_label:
            agree += 1
        else:
            disagreements.append(
                {
                    "id": rec["id"],
                    "org": rec["vacancy"].get("org", ""),
                    "title": rec["vacancy"].get("title", ""),
                    "user_label": user_label,
                    "reason": rec.get("reason", ""),
                    "model_score": score,
                    "model_label": model_label,
                    "kind": "false_negative" if user_label == LABEL_FIT else "false_positive",
                }
            )
        if user_label == LABEL_FIT and model_label == LABEL_FIT:
            tp += 1
        elif user_label == LABEL_NOFIT and model_label == LABEL_FIT:
            fp += 1
        elif user_label == LABEL_FIT and model_label == LABEL_NOFIT:
            fn += 1
        else:
            tn += 1

    n = len(matched)
    agreement = agree / n if n else 0.0
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    # Disagreements closest to the boundary are the most actionable to fix.
    disagreements.sort(key=lambda d: abs(d["model_score"] - threshold))
    return {
        "n": n,
        "threshold": threshold,
        "agreement": agreement,
        "agree": agree,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "disagreements": disagreements,
        "missing_scores": missing,
        "n_fit": sum(1 for r in records if r["label"] == LABEL_FIT),
        "n_nofit": sum(1 for r in records if r["label"] == LABEL_NOFIT),
    }


def format_report(m: dict) -> str:
    """Human-readable report. Prints to the user's terminal only (may contain
    personal titles/orgs) — never written to a tracked file."""
    lines = []
    pct = f"{m['agreement'] * 100:.0f}%"
    lines.append("")
    lines.append(
        f"  AGREEMENT: {pct}  ({m['agree']}/{m['n']} labels match the model at threshold {m['threshold']})"
    )
    lines.append("")

    def fmt(x):
        return "n/a" if x is None else f"{x * 100:.0f}%"

    c = m["confusion"]
    lines.append(f"  Fit = score >= {m['threshold']}. Positive class = 'fit'.")
    lines.append(
        f"  precision {fmt(m['precision'])}   recall {fmt(m['recall'])}   F1 {fmt(m['f1'])}"
    )
    lines.append(
        f"  confusion: TP {c['tp']}  FP {c['fp']}  FN {c['fn']}  TN {c['tn']}"
        f"   (set: {m['n_fit']} fit / {m['n_nofit']} nofit)"
    )
    if m["missing_scores"]:
        lines.append(
            f"  ⚠ {len(m['missing_scores'])} labelled rows had no score submitted (excluded)."
        )
    lines.append("")
    if m["disagreements"]:
        lines.append(
            f"  DISAGREEMENTS ({len(m['disagreements'])}) — what to look at, nearest-boundary first:"
        )
        lines.append(
            "  (FN = you liked it, model scored low · FP = you passed it, model scored high)"
        )
        for d in m["disagreements"]:
            tag = "FN" if d["kind"] == "false_negative" else "FP"
            org = (d["org"] or "?")[:28]
            title = (d["title"] or "?")[:52]
            lines.append(
                f"    [{tag}] score {d['model_score']:>3}  you={d['user_label']:<5}  {org} — {title}"
            )
            if d["reason"]:
                lines.append(f"          your reason: {d['reason']}")
    else:
        lines.append("  No disagreements — model and labels agree on every scored row.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Payload builder — reuse the pipeline's exact scoring input
# ---------------------------------------------------------------------------


def build_payloads(records: list[dict]) -> list[dict]:
    """One scoring payload per record, using the SAME prompt + user-message
    builder the pipeline uses (score_vacancies), so the eval scores exactly what
    a real run would. Label/reason are deliberately EXCLUDED so the scorer stays
    blind to the ground truth."""
    import score_vacancies
    from prompts import VACANCY_SCORING_PROMPT

    payloads = []
    for rec in records:
        vac = dict(rec["vacancy"])
        vac.setdefault("org", "")
        vac.setdefault("title", "")
        payloads.append(
            {
                "payload_kind": "vacancy",
                "id": rec["id"],
                "org": vac.get("org", ""),
                "title": vac.get("title", ""),
                "system_prompt": VACANCY_SCORING_PROMPT,
                "user_msg": score_vacancies._build_user_msg(vac),
            }
        )
    return payloads


# ---------------------------------------------------------------------------
# DB access (read-only) — kept in functions so the pure logic imports cleanly
# ---------------------------------------------------------------------------


def _load_verdict_vacancies() -> list[dict]:
    """All vacancies that carry a user verdict, across every company status
    (a passed role may sit at a now-inactive company). Read-only."""
    from database_supabase import load_vacancies

    vacs = load_vacancies(include_inactive_companies=True)
    return [v for v in vacs.values() if verdict_label(v.get("status")) is not None]


def _load_labelable_vacancies(limit: int, exclude_ids: set) -> list[dict]:
    """Recently-scored vacancies the user can label by hand, newest first,
    excluding anything already in the golden set. Read-only."""
    from database_supabase import load_vacancies

    vacs = load_vacancies(include_inactive_companies=True)
    pool = [
        v
        for v in vacs.values()
        if _has_description(v)
        and v.get("llm_score") is not None
        and (v.get("dedup_hash") or v.get("id")) not in exclude_ids
    ]
    pool.sort(key=_sort_key, reverse=True)
    return pool[:limit]


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_seed(args) -> int:
    path = _store_path()
    vacs = _load_verdict_vacancies()
    if not vacs:
        print(
            "No verdicts found in the database yet. Build history first: like/pass "
            "some vacancies in /jobs-new or /jobs-review, then re-run `seed`. "
            "You can also hand-label with `template` + `add-template`.",
            file=sys.stderr,
        )
        return 0
    records = select_seed_records(vacs, limit=args.limit, balance=not args.no_balance)
    n_fit = sum(1 for r in records if r["label"] == LABEL_FIT)
    print(
        f"Selected {len(records)} verdict labels ({n_fit} fit / {len(records) - n_fit} nofit) "
        f"from {len(vacs)} total verdicts.",
        file=sys.stderr,
    )
    if args.dry_run:
        print("--dry-run: nothing written.", file=sys.stderr)
        return 0
    summary = append_records(path, records)
    print(
        f"Golden set: +{summary['added']} added, {summary['skipped']} already present. "
        f"Now {summary['total']} labels at version {summary['version']}.\n{path}",
        file=sys.stderr,
    )
    return 0


def cmd_template(args) -> int:
    store = _store_path()
    tmpl = _template_path()
    known = {r["id"] for r in load_records(store)}
    pool = _load_labelable_vacancies(args.limit, known)
    if not pool:
        print("No scored, unlabelled vacancies available to template.", file=sys.stderr)
        return 0
    tmpl.parent.mkdir(parents=True, exist_ok=True)
    with tmpl.open("w", encoding="utf-8") as fh:
        for v in pool:
            # llm_score is deliberately omitted so labelling stays blind.
            row = {
                "label": "",  # fill in: fit | nofit
                "reason": "",  # one short line, WHY
                "id": v.get("dedup_hash") or v.get("id"),
                "org": v.get("org", ""),
                "title": v.get("title", ""),
                "preview": (v.get("full_description") or v.get("snippet") or "")[:240],
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        f"Wrote {len(pool)} rows to label (blind — no scores shown):\n  {tmpl}\n"
        'Edit each line: set "label" to fit or nofit and add a short "reason". '
        "Leave a line's label empty to skip it. Then run:\n"
        "  python3 scripts/golden_set.py add-template",
        file=sys.stderr,
    )
    return 0


def cmd_add_template(args) -> int:
    store = _store_path()
    tmpl = Path(args.path).expanduser() if args.path else _template_path()
    if not tmpl.exists():
        print(f"No template at {tmpl}. Run `template` first.", file=sys.stderr)
        return 1
    from database_supabase import load_vacancies

    vacs_by_hash = {}
    for v in load_vacancies(include_inactive_companies=True).values():
        vacs_by_hash[v.get("dedup_hash") or v.get("id")] = v

    records = []
    bad = 0
    for line in tmpl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        label = (row.get("label") or "").strip().lower()
        if label not in (LABEL_FIT, LABEL_NOFIT):
            continue  # unlabelled / skipped line
        vac = vacs_by_hash.get(row.get("id"))
        if not vac:
            bad += 1
            continue
        records.append(make_record(vac, label, (row.get("reason") or "").strip(), "manual"))
    if not records:
        print("No labelled rows found in the template (set label to fit/nofit).", file=sys.stderr)
        return 0
    summary = append_records(store, records)
    msg = (
        f"Golden set: +{summary['added']} added, {summary['skipped']} already present. "
        f"Now {summary['total']} labels at version {summary['version']}."
    )
    if bad:
        msg += f" ({bad} template rows skipped — vacancy no longer in the database.)"
    print(msg, file=sys.stderr)
    return 0


def cmd_emit(args) -> int:
    store = _store_path()
    records = load_records(store, version=args.version)
    if not records:
        print(
            "Golden set is empty. Build it first with `seed` (from your verdicts) "
            "or `template` + `add-template`.",
            file=sys.stderr,
        )
        json.dump([], sys.stdout)
        return 0
    # Redirect stdout → stderr while building payloads: importing the prompt +
    # scoring modules connects the DAL, which prints a backend banner to stdout
    # (score_vacancies.cmd_local uses the same guard). Only the final JSON array
    # is allowed on stdout, so it stays machine-parseable for the scorer.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        payloads = build_payloads(records)
    finally:
        sys.stdout = real_stdout
    version_shown = args.version if args.version else current_version(store)
    print(
        f"Emitting {len(payloads)} scoring payloads "
        f"(golden set version <= {version_shown}). "
        "Score EACH independently — one request per vacancy — and pipe "
        '[{"id":..., "score":...}] to `measure`.',
        file=sys.stderr,
    )
    json.dump(payloads, real_stdout, ensure_ascii=False)
    return 0


def cmd_measure(args) -> int:
    store = _store_path()
    records = load_records(store, version=args.version)
    if not records:
        print("Golden set is empty — nothing to measure.", file=sys.stderr)
        return 1
    raw = json.load(sys.stdin)
    scores: dict[str, int] = {}
    for item in raw:
        if "id" in item and "score" in item:
            scores[item["id"]] = int(item["score"])
    if not scores:
        print(
            'No scores on stdin. Pipe the agent\'s [{"id":..., "score":...}] array in.',
            file=sys.stderr,
        )
        return 1
    m = compute_metrics(records, scores, args.threshold)
    print(format_report(m))
    return 0


def cmd_stats(args) -> int:
    store = _store_path()
    records = load_records(store, version=args.version)
    if not records:
        print("Golden set is empty. Run `seed` or `template` + `add-template`.", file=sys.stderr)
        return 0
    n_fit = sum(1 for r in records if r["label"] == LABEL_FIT)
    versions = sorted({r.get("version", 0) for r in records})
    by_source: dict[str, int] = {}
    for r in records:
        by_source[r.get("source", "?")] = by_source.get(r.get("source", "?"), 0) + 1
    print(f"Golden set: {store}")
    print(f"  labels: {len(records)}  ({n_fit} fit / {len(records) - n_fit} nofit)")
    print(f"  versions: {versions}  (current: {max(versions)})")
    print("  by source: " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Golden-set scoring-quality check (see docs / jobs-eval runbook)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("seed", help="Seed labels from verdict history in the database.")
    sp.add_argument("--limit", type=int, default=50, help="Max labels to add (default 50).")
    sp.add_argument(
        "--no-balance",
        action="store_true",
        help="Take strictly most-recent verdicts instead of guaranteeing fit examples.",
    )
    sp.add_argument(
        "--dry-run", action="store_true", help="Show what would be added; write nothing."
    )
    sp.set_defaults(func=cmd_seed)

    tp = sub.add_parser(
        "template", help="Write a blind labelling template from your own vacancies."
    )
    tp.add_argument("--limit", type=int, default=25, help="Rows to template (default 25).")
    tp.set_defaults(func=cmd_template)

    ap = sub.add_parser("add-template", help="Append hand-labelled rows from the template.")
    ap.add_argument("path", nargs="?", help="Template path (default evals/label_template.jsonl).")
    ap.set_defaults(func=cmd_add_template)

    ep = sub.add_parser("emit", help="Print scoring payloads (stdout) for the agent to score.")
    ep.add_argument("--version", type=int, help="Freeze to golden-set version <= N.")
    ep.set_defaults(func=cmd_emit)

    mp = sub.add_parser("measure", help="Read [{id,score}] from stdin; print the agreement report.")
    mp.add_argument("--version", type=int, help="Freeze to golden-set version <= N.")
    mp.add_argument(
        "--threshold",
        type=int,
        default=_threshold_default(),
        help=f"Fit boundary: score >= this is 'fit' (default {_threshold_default()}).",
    )
    mp.set_defaults(func=cmd_measure)

    stp = sub.add_parser("stats", help="Show set size, versions, and label balance.")
    stp.add_argument("--version", type=int, help="Freeze to golden-set version <= N.")
    stp.set_defaults(func=cmd_stats)

    return p


def _threshold_default() -> int:
    """The pipeline's neutral 'worth acting on' boundary (APPLYABLE_SCORE).

    Read straight from the env var config.py itself uses, so resolving the
    default never imports config (which would open a DB connection just to print
    a report). Same value and override path as the rest of the pipeline.
    """
    import os

    try:
        return int(os.environ.get("APPLYABLE_SCORE", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
