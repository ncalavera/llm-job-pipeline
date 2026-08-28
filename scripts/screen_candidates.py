#!/usr/bin/env python3
"""Cheap relevance screen for newly discovered candidate companies.

Board-discovered candidates otherwise flow straight into PAID enrichment
(Firecrawl scrape + Exa search, twice each) and then WANT-scoring. Most are
commercial / staffing / board noise that a human would reject on sight. This
screen reads the user's OWN profile targeting and drops the CLEAR mismatches
with the cheapest model tier BEFORE any Firecrawl / Exa call runs, so paid
enrichment only ever touches plausible fits.

Two cuts, cheapest first:

1. Already-tracked dedup (no LLM). A fresh candidate whose name is a variant of a
   company we already track (any non-candidate status, or already WANT-scored)
   is dropped — never enriched twice. Reuses ``company_registry.
   company_name_variants_match`` (the same tolerance gate the save layer uses to
   MERGE board name variants); after that merge-on-save, most duplicates never
   even reach here, so this is a cheap second line, not the primary defense.
   Dedup drops carry the ``screen-dup:`` status_reason prefix so they are
   filterable apart from the LLM screen's ``screen:`` drops.

2. Relevance screen (cheap LLM). Each remaining candidate gets a keep/drop from
   the cheapest model tier on its NAME plus whatever short snippet a board
   already gave us — no web scrape. The screen is deliberately biased toward
   KEEP: it only drops UNAMBIGUOUS mismatches (staffing agencies, purely
   commercial businesses outside the candidate's field, explicit anti-list
   matches). Borderline / unfamiliar names are kept, because a wrong drop
   silently hides a real employer while a wrong keep only costs one cheap
   research pass that a human still reviews.

The screen is neutral by default (STRATEGY guardrail 1): every judgment comes
from ``config/user_profile.md`` via the same template-substitution path scoring
uses, never from a baked-in company-type blocklist. Kept companies still land in
Pending for human review — the screen only cuts what reaches paid enrichment.

Every keep / drop is logged with a reason so a wrong drop is visible. Drops are
recorded on the candidate row (``status='inactive'``, ``status_reason``
prefixed ``screen:`` / ``screen-dup:``) and snapshotted to
``companies/archive/`` for review, mirroring ``filter_companies.py``. A summary
JSON (screened N, llm_ran yes/no, drops M) is written to
``vacancies/screen_companies_summary.json`` so "ran and kept all" is auditable
apart from "errored and kept all by fail-safe".

No-API-key path (mirrors score_companies --local conventions): when no direct
Anthropic client is available, ``--apply`` still applies the deterministic
dedup cut, then writes the remaining candidates' screen payloads (one entry per
company, each carrying its own system_prompt + user_msg + real DB id) to
``vacancies/screen_companies_payload.json`` instead of calling a model.
run_daily gates on that file so Claude Code subagents (cheap tier) screen the
payloads and feed decisions back:

    python3 scripts/screen_candidates.py --save < decisions.json
    python3 scripts/screen_candidates.py --save --files r1.json r2.json ...

Direct API stays the fast path when a key exists.

Usage:
    python3 scripts/screen_candidates.py            # analyze + print, no writes
    python3 scripts/screen_candidates.py --apply    # drop clear mismatches
    python3 scripts/screen_candidates.py --save     # apply subagent decisions
    python3 scripts/screen_candidates.py --limit N   # cap candidates this run
    python3 scripts/screen_candidates.py --model haiku   # override the tier
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_conn import get_conn

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Screen payloads for the no-API-key (subagent) path — same dir as the other
#: gate payload files run_daily writes (vacancies/ is gitignored runtime state).
SCREEN_PAYLOAD_PATH = PROJECT_ROOT / "vacancies" / "screen_companies_payload.json"

#: Machine-readable run marker: screened N, llm_ran yes/no, drops M. Written on
#: every run so a "zero drops" outcome is auditable after the fact ("ran, kept
#: all" vs "errored, kept all by fail-safe" vs "deferred to subagents").
SCREEN_SUMMARY_PATH = PROJECT_ROOT / "vacancies" / "screen_companies_summary.json"

# status_reason prefixes — the two cut types are filterable apart.
_DROP_PREFIX_LLM = "screen"
_DROP_PREFIX_DUP = "screen-dup"

# Tier name -> API model id. Only haiku is ever used by default; the strong
# tiers exist because the config knob is clamped to <= the scoring model.
_TIER_TO_MODEL = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}


# ---------------------------------------------------------------------------
# Prompt building (pure — unit-tested directly)
# ---------------------------------------------------------------------------


def screen_system_prompt() -> str:
    """Render the neutral screen instruction against the ACTIVE user profile.

    Reuses the same template-substitution path scoring uses, so the screen is
    profile-driven and owner-agnostic (STRATEGY guardrail 1) with no baked-in
    company-type list. Rendered lazily (not at import) so a missing profile
    surfaces where the screen actually runs, not on import of this module."""
    import prompts

    prompts.clear_profile_cache()
    sections = prompts._load_user_profile()
    return prompts._render(prompts._load_template("company-screen.md"), sections)


def build_user_message(name: str, snippet: str = "") -> str:
    """The per-candidate user turn: just the name and any free board snippet."""
    msg = f'Company name: "{name}"'
    snippet = (snippet or "").strip()
    if snippet:
        msg += f"\nBoard snippet: {snippet}"
    else:
        msg += "\nBoard snippet: (none — decide from the name alone; unknown → keep)"
    return msg


def parse_screen_verdict(text: str) -> "dict":
    """Parse a keep/drop decision from the model's text. Fail-safe to KEEP.

    A screen must never DROP on a parse failure — that would silently hide a real
    employer on a malformed response. Anything we cannot read as an explicit
    ``keep: false`` is treated as a keep. ``fail_safe`` marks decisions that were
    defaulted rather than made, so the run summary can report them apart."""
    from llm_json import parse_llm_json

    parsed = parse_llm_json(text)
    if not isinstance(parsed, dict) or "error" in parsed:
        return {
            "keep": True,
            "reason": "unparseable screen response — kept (fail-safe)",
            "fail_safe": True,
        }
    keep = parsed.get("keep", True)
    if not isinstance(keep, bool):
        keep = str(keep).strip().lower() not in ("false", "no", "0", "drop")
    reason = str(parsed.get("reason", "")).strip() or (
        "plausible fit" if keep else "clear mismatch"
    )
    return {"keep": keep, "reason": reason, "fail_safe": False}


# ---------------------------------------------------------------------------
# Already-tracked dedup (pure — unit-tested directly)
# ---------------------------------------------------------------------------


def dedupe_tracked(
    candidates: "list[dict]", tracked_names: "list[str]"
) -> "tuple[list[dict], list[tuple[dict, str, str]]]":
    """Split fresh candidates into (to_screen, dup_drops).

    A candidate whose name variant-matches an already-tracked company is a
    duplicate and is dropped (never re-enriched). Reuses the save-layer matcher
    so the dedup rule is defined in exactly one place. Drop entries are
    ``(row, reason, kind)`` with kind='dup'."""
    from company_registry import company_name_variants_match

    to_screen: list[dict] = []
    dup_drops: list[tuple[dict, str, str]] = []
    for row in candidates:
        name = row["canonical_name"]
        match = next((t for t in tracked_names if company_name_variants_match(name, t)), None)
        if match is not None:
            dup_drops.append((row, f"already tracked as {match}", "dup"))
        else:
            to_screen.append(row)
    return to_screen, dup_drops


# ---------------------------------------------------------------------------
# The screen (LLM call injected for tests)
# ---------------------------------------------------------------------------


def screen_candidates(candidates: "list[dict]", system_prompt: str, call_llm) -> "list[dict]":
    """Screen each candidate; return decisions ``[{row, keep, reason, fail_safe}]``.

    ``call_llm(system, user) -> str`` is injected so tests mock the model. One
    request per candidate (never batched — STRATEGY guardrail 6). Any per-call
    exception fails safe to KEEP (a screen must not drop on an API error)."""
    decisions: list[dict] = []
    for row in candidates:
        user = build_user_message(row["canonical_name"], row.get("description", ""))
        try:
            text = call_llm(system_prompt, user)
            decision = parse_screen_verdict(text)
        except Exception as exc:  # noqa: BLE001 — an API error must not drop a company
            decision = {
                "keep": True,
                "reason": f"screen error, kept (fail-safe): {type(exc).__name__}",
                "fail_safe": True,
            }
        decisions.append(
            {
                "row": row,
                "keep": decision["keep"],
                "reason": decision["reason"],
                "fail_safe": decision.get("fail_safe", False),
            }
        )
    return decisions


def build_screen_payloads(candidates: "list[dict]", system_prompt: str) -> "list[dict]":
    """Subagent-screening payloads for the no-API-key path — one entry per
    candidate, same shape conventions as score_companies --local (each entry
    carries its own system_prompt + user_msg + the real DB id)."""
    return [
        {
            "payload_kind": "company_screen",
            "id": str(row["id"]),
            "canonical_name": row["canonical_name"],
            "system_prompt": system_prompt,
            "user_msg": build_user_message(row["canonical_name"], row.get("description", "")),
        }
        for row in candidates
    ]


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def _earned_vacancy_clause() -> str:
    """SQL EXISTS predicate: this candidate has a vacancy that EARNS paid
    enrichment — one scored at/above the floor, or one the user liked. Placeholder
    ``%s`` binds the score floor (db_conn translates it per backend)."""
    return (
        "EXISTS (SELECT 1 FROM vacancy v WHERE v.company_id = company.id "
        "AND (v.llm_score >= %s OR v.status = 'liked'))"
    )


def load_fresh_candidates(
    conn, limit: int = 0, min_vacancy_score: "int | None" = None
) -> "list[dict]":
    """Fresh board-discovered candidates that have EARNED paid enrichment:
    status='candidate', not yet scored, AND with at least one vacancy scoring at/
    above ``min_vacancy_score`` (or a liked vacancy). A stranger company with no
    earning vacancy is a free name-only row that simply waits — it never reaches
    this screen or the paid chain behind it (R3). ``min_vacancy_score`` defaults to
    the ``[volume] company_paid_min_vacancy_score`` knob.

    Includes ghost candidates (no website yet) on purpose — dropping a clear
    mismatch HERE also saves the paid website-search that would otherwise run on
    it. Ordered by name so a --limit run screens a stable prefix.

    Defensive against schema drift: if ``company.description`` is missing from a
    live DB (it lives in the frozen baseline but drifted out once), the screen
    degrades to NAME-ONLY selection with a printed warning instead of crashing —
    a missing snippet only makes the screen slightly blinder, never wrong (it
    fails safe to KEEP)."""
    if min_vacancy_score is None:
        from scoring_settings import company_paid_min_vacancy_score

        min_vacancy_score = company_paid_min_vacancy_score()

    tail = (
        "FROM company "
        "WHERE status = 'candidate' AND alignment_score IS NULL "
        f"AND {_earned_vacancy_clause()} "
        "ORDER BY canonical_name"
    )
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, canonical_name, description, category " + tail, (min_vacancy_score,)
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 — degrade to name-only, never crash the valve
        try:
            conn.rollback()  # a failed SELECT aborts the Postgres txn; clear it
        except Exception:
            pass
        print(
            "  ⚠  screen: company.description unavailable — screening on name only "
            f"(schema drift: {type(exc).__name__})",
            flush=True,
        )
        cur = conn.cursor()
        cur.execute("SELECT id, canonical_name, category " + tail, (min_vacancy_score,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r.setdefault("description", "")
    cur.close()
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


def count_unearned_candidates(conn, min_vacancy_score: "int | None" = None) -> int:
    """Fresh candidates that have NOT yet earned paid enrichment — no vacancy at/
    above the score floor and none liked. These are the free name-only rows that
    simply wait; counting them lets the run say how many are held back (R3), never
    silently dropping them."""
    if min_vacancy_score is None:
        from scoring_settings import company_paid_min_vacancy_score

        min_vacancy_score = company_paid_min_vacancy_score()
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM company "
        "WHERE status = 'candidate' AND alignment_score IS NULL "
        f"AND NOT {_earned_vacancy_clause()}",
        (min_vacancy_score,),
    )
    n = cur.fetchone()[0]
    cur.close()
    return int(n or 0)


def load_tracked_names(conn) -> "list[str]":
    """Names of companies already past the fresh-candidate stage — anything that
    is NOT a still-unscored candidate. A fresh candidate matching one of these is
    a duplicate the screen must not enrich again."""
    cur = conn.cursor()
    cur.execute(
        "SELECT canonical_name FROM company "
        "WHERE status != 'candidate' OR alignment_score IS NOT NULL"
    )
    names = [r[0] for r in cur.fetchall()]
    cur.close()
    return names


def apply_drops(conn, drops: "list[tuple[dict, str, str]]") -> None:
    """Set dropped candidates inactive with a kind-prefixed status_reason
    (``screen-dup:`` for dedup drops, ``screen:`` for LLM drops), after
    snapshotting them to companies/archive/ (mirrors filter_companies.py). Only
    candidate rows are touched; a kept company is never modified."""
    if not drops:
        return

    snapshot_dir = PROJECT_ROOT / "companies" / "archive"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    snapshot_path = snapshot_dir / f"company_screen_{ts}.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "archived_at": datetime.now().isoformat(),
                "source": "screen_candidates.py",
                "count": len(drops),
                "companies": [
                    {
                        "id": str(row["id"]),
                        "canonical_name": row["canonical_name"],
                        "reason": reason,
                        "kind": kind,
                    }
                    for row, reason, kind in drops
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cur = conn.cursor()
    for row, reason, kind in drops:
        prefix = _DROP_PREFIX_DUP if kind == "dup" else _DROP_PREFIX_LLM
        cur.execute(
            "UPDATE company SET status = 'inactive', status_reason = %s "
            "WHERE id = %s AND status = 'candidate'",
            (f"{prefix}: {reason}", row["id"]),
        )
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_screen(conn, call_llm, limit: int = 0) -> "dict":
    """Dedup + screen the fresh candidates; return a decision summary.

    Never writes — the caller applies drops when ``--apply`` is set.

    ``call_llm`` may be ``None`` (no direct-API credentials): then the LLM cut
    does not run here — the remaining candidates are returned under ``pending``
    so the caller can write subagent payloads for them (the --local-style path).
    They stay candidates (kept — the safe direction) until decisions come back
    via ``--save``."""
    candidates = load_fresh_candidates(conn, limit)
    try:
        waiting_unearned = count_unearned_candidates(conn)
    except Exception:  # best-effort count for the log line — never fail the screen
        waiting_unearned = 0
    base = {
        "total": len(candidates),
        "keep": [],
        "drop": [],
        "pending": [],
        "screened": 0,
        "fail_safe_keeps": 0,
        "llm_ran": call_llm is not None,
        "waiting_unearned": waiting_unearned,
    }
    if not candidates:
        return base

    tracked = load_tracked_names(conn)
    to_screen, dup_drops = dedupe_tracked(candidates, tracked)

    keep: list[tuple[dict, str]] = []
    drop: list[tuple[dict, str, str]] = list(dup_drops)
    pending: list[dict] = []
    fail_safe = 0

    if call_llm is None:
        pending = to_screen
    else:
        system_prompt = screen_system_prompt()
        for d in screen_candidates(to_screen, system_prompt, call_llm):
            if d["keep"]:
                keep.append((d["row"], d["reason"]))
                fail_safe += 1 if d.get("fail_safe") else 0
            else:
                drop.append((d["row"], d["reason"], "llm"))

    base.update(
        keep=keep,
        drop=drop,
        pending=pending,
        screened=len(to_screen) if call_llm is not None else 0,
        fail_safe_keeps=fail_safe,
    )
    return base


def write_summary(summary: dict, applied: bool, path: Path = SCREEN_SUMMARY_PATH) -> dict:
    """Persist the one-line run marker (screened N, llm_ran yes/no, drops M) so a
    zero-drop run is auditable: ``llm_ran`` false means the LLM cut did not run
    here (no credentials — decisions deferred to subagents), and
    ``fail_safe_keeps`` counts keeps that were defaults on an error, not real
    decisions."""
    dup_drops = sum(1 for _, _, k in summary["drop"] if k == "dup")
    llm_drops = len(summary["drop"]) - dup_drops
    record = {
        "at": datetime.now().isoformat(),
        "applied": applied,
        "total": summary["total"],
        "screened": summary["screened"],
        "llm_ran": summary["llm_ran"],
        "kept": len(summary["keep"]),
        "drops": len(summary["drop"]),
        "dup_drops": dup_drops,
        "llm_drops": llm_drops,
        "fail_safe_keeps": summary["fail_safe_keeps"],
        "pending_count": len(summary["pending"]),
        "waiting_unearned": summary.get("waiting_unearned", 0),
        "payload_path": str(SCREEN_PAYLOAD_PATH) if summary["pending"] else "",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


# ---------------------------------------------------------------------------
# Anthropic call (real credentials path)
# ---------------------------------------------------------------------------


def build_call_llm(model_tier: str):
    """Return a ``call_llm(system, user) -> str`` bound to the cheap model, or
    ``None`` to screen with subagents instead.

    Subagents are the NORMAL route, not a fallback: Nikita runs every LLM call
    on his Claude subscription through headless sessions, and deliberately
    keeps ANTHROPIC_API_KEY out of the environment so the night's spend cannot
    slip onto per-token billing (2026-08-28). Returning None routes the run to
    the subagent (--local-style) path — candidates stay kept until decisions
    come back via --save.

    The explicit key check still matters: the SDK client constructs fine
    without a key and only fails per-request, which would burn one fail-safe
    keep per candidate instead of cleanly handing over to subagents."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  screen: screening with subagents", file=sys.stderr)
        return None
    try:
        import anthropic
    except ImportError:
        print(
            "  screen: anthropic package not installed — deferring to subagent screening",
            file=sys.stderr,
        )
        return None

    model = _TIER_TO_MODEL.get(model_tier, _TIER_TO_MODEL["haiku"])
    client = anthropic.Anthropic()

    def call_llm(system: str, user: str) -> str:
        resp = client.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text if resp.content else ""

    return call_llm


# ---------------------------------------------------------------------------
# Mode: --save (subagent decisions back into the DB)
# ---------------------------------------------------------------------------


def cmd_save(files: "list[str] | None") -> int:
    """Apply subagent screen decisions: stdin JSON array, or --files (one result
    file per subagent, parsed independently via llm_json.read_result_files so a
    malformed one is named and skipped, never failing the rest — same contract
    as score_companies --save).

    Each decision: {"id": "<company id>", "keep": true|false, "reason": "..."}.
    Only keep=false rows are written (kept candidates are simply left alone);
    a missing/unknown id is reported and skipped, and anything unreadable
    defaults to keep — the fail-safe direction."""
    from llm_json import read_result_files

    if files:
        data, bad_files = read_result_files(files)
        for b in bad_files:
            print(f"  WARNING: malformed result file skipped (its companies stay kept): {b}")
    else:
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(
                f"ERROR: stdin is not valid JSON ({exc}) — nothing applied (all kept). "
                "Save each subagent's raw output to its own file and pass --files.",
                file=sys.stderr,
            )
            return 1

    if not isinstance(data, list):
        data = [data]

    conn = get_conn()
    known = {str(r["id"]): r for r in load_fresh_candidates(conn)}

    drops: list[tuple[dict, str, str]] = []
    kept = 0
    skipped = 0
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue
        cid = str(item.get("id", "")).strip()
        row = known.get(cid)
        if row is None:
            print(f"  SKIP unknown/already-decided id: {cid or '(missing)'}")
            skipped += 1
            continue
        keep = item.get("keep", True)
        if not isinstance(keep, bool):
            keep = str(keep).strip().lower() not in ("false", "no", "0", "drop")
        reason = str(item.get("reason", "")).strip() or (
            "plausible fit" if keep else "clear mismatch"
        )
        if keep:
            kept += 1
            print(f"  keep  {row['canonical_name']}: {reason}")
        else:
            drops.append((row, reason, "llm"))
            print(f"  DROP  {row['canonical_name']}: {reason}")

    apply_drops(conn, drops)
    print(f"Applied {len(drops)} drop(s), {kept} keep(s), {skipped} skipped")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _print_report(summary: dict, applied: bool) -> None:
    kept, dropped, pending = summary["keep"], summary["drop"], summary["pending"]
    head = "APPLIED" if applied else "DRY-RUN"
    llm_note = "LLM screen ran" if summary["llm_ran"] else "no API key — LLM screen deferred"
    print(
        f"[{head}] {summary['total']} fresh candidate(s): {len(kept)} kept, "
        f"{len(dropped)} dropped, {len(pending)} pending subagent screen ({llm_note})"
    )
    waiting = summary.get("waiting_unearned", 0)
    if waiting:
        print(
            f"  {waiting} candidate(s) waiting unearned — no vacancy has earned them "
            "paid enrichment yet (kept free until one does)"
        )
    for row, reason, kind in dropped:
        tag = _DROP_PREFIX_DUP if kind == "dup" else _DROP_PREFIX_LLM
        print(f"  DROP [{tag}] {row['canonical_name']}: {reason}")
    for row, reason in kept:
        print(f"  keep  {row['canonical_name']}: {reason}")
    for row in pending:
        print(f"  pend  {row['canonical_name']}: awaiting subagent decision")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cheap relevance screen before company enrichment")
    parser.add_argument("--apply", action="store_true", help="Drop clear mismatches (writes DB)")
    parser.add_argument(
        "--save", action="store_true", help="Apply subagent decisions (stdin/--files)"
    )
    parser.add_argument("--files", nargs="+", help="With --save: per-subagent result files")
    parser.add_argument("--limit", type=int, default=0, help="Cap candidates screened this run")
    parser.add_argument(
        "--model", type=str, default="", help="Model tier override (haiku/sonnet/opus)"
    )
    args = parser.parse_args()

    if args.save:
        return cmd_save(args.files)

    from scoring_settings import company_screen_model

    tier = (args.model or company_screen_model()).strip().lower()
    call_llm = build_call_llm(tier)

    conn = get_conn()
    summary = run_screen(conn, call_llm, limit=args.limit)

    if args.apply:
        apply_drops(conn, summary["drop"])
        if summary["pending"]:
            # No direct-API client: hand the remaining candidates to subagent
            # screening (run_daily gates on this file; decisions return via --save).
            SCREEN_PAYLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
            SCREEN_PAYLOAD_PATH.write_text(
                json.dumps(
                    build_screen_payloads(summary["pending"], screen_system_prompt()),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"  Wrote {len(summary['pending'])} screen payload(s) to {SCREEN_PAYLOAD_PATH}")

    record = write_summary(summary, applied=args.apply)
    _print_report(summary, applied=args.apply)
    print(
        f"screen summary: screened {record['screened']} of {record['total']}, "
        f"llm_ran {'yes' if record['llm_ran'] else 'no'}, drops {record['drops']} "
        f"(dup {record['dup_drops']} + screen {record['llm_drops']}), "
        f"fail-safe keeps {record['fail_safe_keeps']}, pending {record['pending_count']}, "
        f"waiting unearned {record['waiting_unearned']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
