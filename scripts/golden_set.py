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
     precision/recall at the score threshold, and the disagreement list. It also
     persists a small summary (agreement_pct, set size/version, threshold,
     measured-at) next to the golden set — the ONE seam scripts/learning.py's
     zero-LLM review reads to show the last measured agreement.

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


def _summary_path(base_dir: Path | None = None) -> Path:
    """Where the last-measured-agreement summary lives — a small, gitignored
    snapshot next to the golden set (respects GOLDEN_SET_DIR, same as the
    set itself). This is the file scripts/learning.py's agreement seam reads
    (via scoring_agreement_pct / scoring_agreement_summary below), so the
    /jobs-new learning review can show the LAST measured number without
    re-running an LLM eval."""
    return (base_dir or _default_dir()) / "golden_set_summary.json"


# --- Company mode paths (a parallel, separately-versioned set for the company
# scoring prompt). Same gitignored dir as the vacancy set; distinct filenames so
# the two sets never collide. ------------------------------------------------


def _company_store_path(base_dir: Path | None = None) -> Path:
    return (base_dir or _default_dir()) / "golden_set_companies.jsonl"


def _company_template_path(base_dir: Path | None = None) -> Path:
    return (base_dir or _default_dir()) / "company_label_template.jsonl"


def _company_summary_path(base_dir: Path | None = None) -> Path:
    return (base_dir or _default_dir()) / "golden_set_companies_summary.json"


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
# Company labelling — the company-scoring prompt's own golden set.
#
# Vacancy labels come from like/pass verdicts. Company labels come from the
# user's own keep/reject verdicts on companies: a hand-APPROVED (kept active)
# company is "fit", a hand-REJECTED (deactivated) one is "nofit". The signal is
# in status + status_reason, so this classifier reads both and deliberately
# skips machine-made status changes (auto-approve/reject on the model's own
# score, /filter triage drops, dedup merges, audit sweeps) — those are not the
# user's judgement and would just measure the prompt against itself. Company
# content (evidence) is frozen INTO the record, so the set is self-contained.
# ---------------------------------------------------------------------------

#: status_reason prefixes that mark a MACHINE status change, not a hand verdict.
#: Anything starting with one of these is excluded from company labels (the
#: "screen" prefix is here per the same rule the vacancy path uses — a screen
#: drop is a cheap pre-filter, not a considered verdict).
_MACHINE_REASON_PREFIXES = (
    "auto-approved",
    "auto-rejected",
    "triage drop",
    "pre-filter",
    "board noise",
    "board_disabled",
    "mass-rejected",
    "mass-approved",
    "audit-",
    "cadence reset",
    "merged into",
    "merged_into",
    "archived",
    "sheet:",
    "deactivated",
    "manual_check",
    "monitor-only",
    "screen",
)

#: Hand-APPROVE reasons: the user clicked approve in a dashboard. Exact matches
#: (not prefixes) so they can't be confused with machine reasons. Two spellings:
#: the canonical "approved via dashboard" plus the historical string
#: scripts/dashboard_local.py wrote before it was canonicalized — old rows keep
#: the old wording forever, so both must count as a fit verdict.
_HAND_APPROVE_REASONS = frozenset(
    {
        "approved via dashboard",
        "approved via local dashboard",
    }
)

#: Minimum frozen evidence length for a company to be scorable (below this the
#: scorer has nothing to judge, same spirit as _has_description for vacancies).
_MIN_COMPANY_CONTENT = 200


def _is_hand_reject_reason(status_reason: str | None) -> bool:
    """True when an *inactive* company's status_reason reads as the user's own
    reject, not a machine status change. Empty reason is treated as unknown →
    not a hand label."""
    reason = (status_reason or "").strip().lower()
    if not reason:
        return False
    if any(reason.startswith(p) for p in _MACHINE_REASON_PREFIXES):
        return False
    return True


def company_label(status: str | None, status_reason: str | None) -> str | None:
    """Map a company's (status, status_reason) to a binary golden label, or None
    when the row carries no usable user verdict (still a candidate, or the status
    was set by the machine rather than a hand click)."""
    reason = (status_reason or "").strip().lower()
    if status == "active" and reason in _HAND_APPROVE_REASONS:
        return LABEL_FIT
    if status == "inactive" and _is_hand_reject_reason(status_reason):
        return LABEL_NOFIT
    return None


def _freeze_company(company: dict, content: str) -> dict:
    """The minimal, frozen scoring input stored with a company label. Mirrors the
    csv-context fields score_companies._build_csv_context reads, plus the frozen
    evidence ``content`` so the measurement is reproducible without re-querying."""
    return {
        "canonical_name": company.get("canonical_name", ""),
        "website": company.get("website", "") or "",
        "category": company.get("category"),
        "tier": company.get("tier"),
        "product": company.get("product"),
        "experience_match": company.get("experience_match"),
        "personal_interest": company.get("personal_interest"),
        "user_comments": company.get("user_comments"),
        "content": content,
    }


def make_company_record(
    company: dict, label: str, reason: str, source: str, content: str, flag: str | None = None
) -> dict:
    """Build a company golden record (without version/labeled_at — set at append).
    ``flag``, when set, marks a label the user should sanity-check by hand."""
    rec = {
        "id": str(company.get("id") or company.get("canonical_name", "")),
        "source": source,
        "label": label,
        "reason": reason,
        "company": _freeze_company(company, content),
    }
    if flag:
        rec["flag"] = flag
    return rec


def select_company_seed_records(
    companies: list[dict], *, limit: int, balance: bool = True
) -> list[dict]:
    """Turn labelled+scorable company dicts into golden records.

    Each input dict must already carry ``label`` (fit/nofit), ``content`` (frozen
    evidence, >= _MIN_COMPANY_CONTENT chars) and, optionally, ``alignment_score``
    (the CURRENT stored score, used only to FLAG a likely-ambiguous label — never
    to score). balance=True aims for an even split so precision/recall are
    meaningful; richest-evidence companies are preferred within each class.
    """
    fit_pool = [c for c in companies if c.get("label") == LABEL_FIT]
    nofit_pool = [c for c in companies if c.get("label") == LABEL_NOFIT]
    fit_pool.sort(key=lambda c: len(c.get("content", "")), reverse=True)
    nofit_pool.sort(key=lambda c: len(c.get("content", "")), reverse=True)

    if balance:
        half = max(1, limit // 2)
        chosen = fit_pool[:half] + nofit_pool[:half]
        # top up from whichever class has spare rows if one was short
        if len(chosen) < limit:
            spare = fit_pool[half:] + nofit_pool[half:]
            chosen += spare[: limit - len(chosen)]
    else:
        chosen = (fit_pool + nofit_pool)[:limit]

    # Same env-aware boundary `measure --kind company` uses (AUTO_REVIEW_APPROVE),
    # resolved once per call so the flag heuristic can't drift from the eval's
    # own threshold when the env var is overridden.
    flag_boundary = _company_threshold_default()
    records = []
    for c in chosen:
        label = c["label"]
        reason = c.get("reason") or f"user verdict: {c.get('status_reason', label)}"
        flag = None
        # A nofit whose CURRENT stored score is already at/above the fit boundary
        # is exactly the case most likely driven by a factor the WANT prompt does
        # not model (geography, visa sponsorship) rather than mission fit — flag
        # it so the user can flip or drop it before trusting the number.
        score = c.get("alignment_score")
        if label == LABEL_NOFIT and score is not None and float(score) >= flag_boundary:
            flag = (
                "review: hand-rejected but stored WANT is high — the reject may be "
                "geo/visa (a separate layer), not the mission fit this prompt scores"
            )
        records.append(
            make_company_record(
                c,
                label,
                reason,
                f"seed:{'active' if label == LABEL_FIT else 'inactive'}",
                c.get("content", ""),
                flag=flag,
            )
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


def _display(rec: dict) -> tuple[str, str]:
    """The (headline, subtitle) pair a disagreement row shows, regardless of kind.

    Vacancy records carry a frozen ``vacancy`` (org / title); company records
    carry a frozen ``company`` (canonical_name / category). Kept here so the
    kind-agnostic metrics + report code below reads one accessor, not two shapes.
    """
    v = rec.get("vacancy")
    if v is not None:
        return v.get("org", ""), v.get("title", "")
    c = rec.get("company") or {}
    return c.get("canonical_name", ""), c.get("category", "") or ""


def compute_metrics(records: list[dict], scores: dict[str, int], threshold: int) -> dict:
    """Join golden labels with model scores and compute the quality report.

    ``scores`` maps record id → model score. "fit" is the positive class. A model
    score >= threshold is the model predicting "fit". Works for both vacancy and
    company records — the only kind-specific bit is the display pair (``_display``).
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
            disp_org, disp_title = _display(rec)
            disagreements.append(
                {
                    "id": rec["id"],
                    "org": disp_org,
                    "title": disp_title,
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


# ---------------------------------------------------------------------------
# Learning-cycle seam — persist the headline of the LAST measurement so
# scripts/learning.py's agreement adapter can show it without re-running an
# LLM eval (that module never computes its own agreement number; see its
# "Scoring-agreement adapter" section).
# ---------------------------------------------------------------------------


def write_summary(path: Path, m: dict, version: int) -> dict:
    """Persist the headline measurement next to the golden set. Overwrites —
    this is a snapshot of the LATEST run, not a ledger; the append-only
    golden_set.jsonl remains the source of truth for labels."""
    summary = {
        "agreement_pct": round(m["agreement"] * 100, 1),
        "set_size": m["n"],
        "set_version": version,
        "threshold": m["threshold"],
        "measured_at": _now(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def scoring_agreement_summary() -> dict | None:
    """The last-persisted measurement (agreement_pct, set_size, set_version,
    threshold, measured_at) — or None if `measure` has never run. This is the
    richer of the two functions scripts/learning.py's agreement seam probes
    for (module ``golden_set``); it carries the measured-at date the plain
    percentage does not."""
    path = _summary_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def scoring_agreement_pct() -> float | None:
    """The bare agreement percentage (0-100), or None if never measured."""
    summary = scoring_agreement_summary()
    return summary.get("agreement_pct") if summary else None


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


def build_company_payloads(records: list[dict]) -> list[dict]:
    """One scoring payload per company record, using the SAME system prompt +
    user-message template the pipeline uses (score_companies), so the eval scores
    exactly what a real company-scoring run would. The frozen evidence content is
    fed straight into the real user template; label/reason/flag are excluded so
    the scorer stays blind to the ground truth."""
    import score_companies

    system_prompt = score_companies._get_system_prompt()
    payloads = []
    for rec in records:
        c = rec["company"]
        user_msg = score_companies.COMPANY_SCORING_USER_TEMPLATE.format(
            name=c.get("canonical_name", ""),
            url=c.get("website", "") or "",
            csv_context=score_companies._build_csv_context(c),
            content=c.get("content", ""),
        )
        payloads.append(
            {
                "payload_kind": "company",
                "id": rec["id"],
                "canonical_name": c.get("canonical_name", ""),
                "url": c.get("website", "") or "",
                "system_prompt": system_prompt,
                "user_msg": user_msg,
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


def _load_labelable_companies(exclude_ids: set) -> list[dict]:
    """All companies that carry a hand verdict (approved/kept or rejected) AND
    have enough frozen evidence to be scorable, with the assembled evidence
    content attached. Read-only — reuses score_companies' own evidence assembly
    so the frozen content matches what a real run would score. ``exclude_ids``
    skips companies already in the golden set."""
    import score_companies
    from db_conn import get_conn

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, canonical_name, website, category, product, tier,
               user_comments, experience_match, personal_interest,
               status, status_reason, alignment_score
        FROM company
        WHERE status IN ('active', 'inactive')
        ORDER BY canonical_name, id
        """
    )
    columns = [desc[0] for desc in cur.description]
    rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    cur.close()

    labelled = []
    for row in rows:
        label = company_label(row.get("status"), row.get("status_reason"))
        if label is None:
            continue
        if str(row.get("id")) in exclude_ids:
            continue
        row["label"] = label
        labelled.append(row)

    if not labelled:
        return []

    evidence_map = score_companies._load_company_evidence_map([r["id"] for r in labelled])
    out = []
    for row in labelled:
        content = score_companies._assemble_evidence_content(evidence_map.get(str(row["id"]), []))
        if len(content) < _MIN_COMPANY_CONTENT:
            continue
        row["content"] = content
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_seed(args) -> int:
    if getattr(args, "kind", "vacancy") == "company":
        return cmd_seed_company(args)
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
    if getattr(args, "kind", "vacancy") == "company":
        return cmd_template_company(args)
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
    if getattr(args, "kind", "vacancy") == "company":
        return cmd_add_template_company(args)
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
    is_company = getattr(args, "kind", "vacancy") == "company"
    store = _company_store_path() if is_company else _store_path()
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
        payloads = build_company_payloads(records) if is_company else build_payloads(records)
    finally:
        sys.stdout = real_stdout
    version_shown = args.version if args.version else current_version(store)
    unit = "company" if is_company else "vacancy"
    print(
        f"Emitting {len(payloads)} scoring payloads "
        f"(golden set version <= {version_shown}). "
        f"Score EACH independently — one request per {unit} — and pipe "
        '[{"id":..., "score":...}] to `measure`.',
        file=sys.stderr,
    )
    if is_company:
        print(
            "  Company payloads carry the company-scoring prompt; the score to "
            "collect is the response's mission_fit.alignment_score (0-100).",
            file=sys.stderr,
        )
    json.dump(payloads, real_stdout, ensure_ascii=False)
    return 0


def cmd_measure(args) -> int:
    is_company = getattr(args, "kind", "vacancy") == "company"
    store = _company_store_path() if is_company else _store_path()
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
    threshold = args.threshold
    if threshold is None:
        threshold = _company_threshold_default() if is_company else _threshold_default()
    m = compute_metrics(records, scores, threshold)
    version_shown = args.version if args.version is not None else current_version(store)
    summary_path = _company_summary_path() if is_company else _summary_path()
    write_summary(summary_path, m, version_shown)
    print(format_report(m))
    return 0


def cmd_stats(args) -> int:
    is_company = getattr(args, "kind", "vacancy") == "company"
    store = _company_store_path() if is_company else _store_path()
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
    flagged = sum(1 for r in records if r.get("flag"))
    if flagged:
        print(f"  flagged for review: {flagged} (see the 'flag' field — verify or drop)")
    return 0


# ---------------------------------------------------------------------------
# Company subcommands — same UX, backed by the company golden set + prompt.
# ---------------------------------------------------------------------------


def cmd_seed_company(args) -> int:
    path = _company_store_path()
    known = {r["id"] for r in load_records(path)}
    companies = _load_labelable_companies(known)
    if not companies:
        print(
            "No hand-verdict companies found (approved/kept or hand-rejected, with "
            "enough evidence to score). Approve/reject some companies in the dashboard "
            "or /jobs-review first, then re-run `seed --kind company`.",
            file=sys.stderr,
        )
        return 0
    records = select_company_seed_records(companies, limit=args.limit, balance=not args.no_balance)
    n_fit = sum(1 for r in records if r["label"] == LABEL_FIT)
    n_flag = sum(1 for r in records if r.get("flag"))
    print(
        f"Selected {len(records)} company labels ({n_fit} fit / {len(records) - n_fit} nofit) "
        f"from {len(companies)} hand-verdict companies. {n_flag} flagged for review.",
        file=sys.stderr,
    )
    if args.dry_run:
        print("--dry-run: nothing written.", file=sys.stderr)
        return 0
    summary = append_records(path, records)
    print(
        f"Company golden set: +{summary['added']} added, {summary['skipped']} already present. "
        f"Now {summary['total']} labels at version {summary['version']}.\n{path}",
        file=sys.stderr,
    )
    return 0


def cmd_template_company(args) -> int:
    store = _company_store_path()
    tmpl = _company_template_path()
    known = {r["id"] for r in load_records(store)}
    # Blind template: offer companies WITH evidence but drop the verdict-derived
    # label, so the user can hand-label from the preview without seeing it.
    pool = _load_labelable_companies(known)[: args.limit]
    if not pool:
        print("No scorable companies available to template.", file=sys.stderr)
        return 0
    tmpl.parent.mkdir(parents=True, exist_ok=True)
    with tmpl.open("w", encoding="utf-8") as fh:
        for c in pool:
            row = {
                "label": "",  # fill in: fit | nofit
                "reason": "",  # one short line, WHY
                "id": str(c.get("id")),
                "canonical_name": c.get("canonical_name", ""),
                "category": c.get("category", ""),
                "preview": (c.get("content", ""))[:240],
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        f"Wrote {len(pool)} companies to label (blind — no scores shown):\n  {tmpl}\n"
        'Edit each line: set "label" to fit or nofit and add a short "reason". '
        "Leave a line's label empty to skip it. Then run:\n"
        "  python3 scripts/golden_set.py add-template --kind company",
        file=sys.stderr,
    )
    return 0


def cmd_add_template_company(args) -> int:
    store = _company_store_path()
    tmpl = Path(args.path).expanduser() if args.path else _company_template_path()
    if not tmpl.exists():
        print(f"No template at {tmpl}. Run `template --kind company` first.", file=sys.stderr)
        return 1
    # Re-load the scorable companies so we can freeze their evidence content.
    by_id = {str(c.get("id")): c for c in _load_labelable_companies(set())}

    records = []
    bad = 0
    for line in tmpl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        label = (row.get("label") or "").strip().lower()
        if label not in (LABEL_FIT, LABEL_NOFIT):
            continue
        company = by_id.get(str(row.get("id")))
        if not company:
            bad += 1
            continue
        records.append(
            make_company_record(
                company,
                label,
                (row.get("reason") or "").strip(),
                "manual",
                company.get("content", ""),
            )
        )
    if not records:
        print("No labelled rows found in the template (set label to fit/nofit).", file=sys.stderr)
        return 0
    summary = append_records(store, records)
    msg = (
        f"Company golden set: +{summary['added']} added, {summary['skipped']} already present. "
        f"Now {summary['total']} labels at version {summary['version']}."
    )
    if bad:
        msg += f" ({bad} template rows skipped — company no longer scorable.)"
    print(msg, file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Golden-set scoring-quality check (see docs / jobs-eval runbook)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_kind(parser):
        parser.add_argument(
            "--kind",
            choices=("vacancy", "company"),
            default="vacancy",
            help="Which scoring surface to evaluate (default vacancy). 'company' "
            "uses the company golden set + company-scoring prompt.",
        )

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
    _add_kind(sp)
    sp.set_defaults(func=cmd_seed)

    tp = sub.add_parser(
        "template", help="Write a blind labelling template from your own vacancies."
    )
    tp.add_argument("--limit", type=int, default=25, help="Rows to template (default 25).")
    _add_kind(tp)
    tp.set_defaults(func=cmd_template)

    ap = sub.add_parser("add-template", help="Append hand-labelled rows from the template.")
    ap.add_argument("path", nargs="?", help="Template path (default evals/label_template.jsonl).")
    _add_kind(ap)
    ap.set_defaults(func=cmd_add_template)

    ep = sub.add_parser("emit", help="Print scoring payloads (stdout) for the agent to score.")
    ep.add_argument("--version", type=int, help="Freeze to golden-set version <= N.")
    _add_kind(ep)
    ep.set_defaults(func=cmd_emit)

    mp = sub.add_parser("measure", help="Read [{id,score}] from stdin; print the agreement report.")
    mp.add_argument("--version", type=int, help="Freeze to golden-set version <= N.")
    mp.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="Fit boundary: score >= this is 'fit'. Default: APPLYABLE_SCORE "
        "(vacancy) / AUTO_REVIEW_APPROVE (company), both 60 unless overridden.",
    )
    _add_kind(mp)
    mp.set_defaults(func=cmd_measure)

    stp = sub.add_parser("stats", help="Show set size, versions, and label balance.")
    stp.add_argument("--version", type=int, help="Freeze to golden-set version <= N.")
    _add_kind(stp)
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


def _company_threshold_default() -> int:
    """The company pipeline's own keep/reject boundary: AUTO_REVIEW_APPROVE (the
    alignment score at/above which auto_review_candidates flips a candidate to
    active). Same env var and default (60) the DAL reads, so the eval boundary
    matches the pipeline's."""
    import os

    try:
        return int(os.environ.get("AUTO_REVIEW_APPROVE", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
