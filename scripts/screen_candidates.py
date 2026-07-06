#!/usr/bin/env python3
"""Cheap relevance screen for newly discovered candidate companies.

Board-discovered candidates otherwise flow straight into PAID enrichment
(Firecrawl scrape + Exa search, twice each) and then Sonnet WANT-scoring. Most
are commercial / staffing / board noise that a human would reject on sight. This
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
recorded on the candidate row (``status='inactive'``, ``status_reason`` prefixed
``screen:``) and snapshotted to ``companies/archive/`` for review, mirroring
``filter_companies.py``.

If no Anthropic credentials are available, the LLM cut is skipped and every
candidate is kept (the safe direction) — only the deterministic dedup still
applies.

Usage:
    python3 scripts/screen_candidates.py            # analyze + print, no writes
    python3 scripts/screen_candidates.py --apply    # drop clear mismatches
    python3 scripts/screen_candidates.py --limit N   # cap candidates this run
    python3 scripts/screen_candidates.py --model haiku   # override the tier
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_conn import get_conn

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Reason-prefix that marks a row dropped by this screen (so drops are greppable
# and distinguishable from filter_companies' structural drops).
_DROP_PREFIX = "screen"

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


def parse_decision(text: str) -> "dict":
    """Parse a keep/drop decision from the model's text. Fail-safe to KEEP.

    A screen must never DROP on a parse failure — that would silently hide a real
    employer on a malformed response. Anything we cannot read as an explicit
    ``keep: false`` is treated as a keep."""
    from llm_json import parse_llm_json

    parsed = parse_llm_json(text)
    if not isinstance(parsed, dict) or "error" in parsed:
        return {"keep": True, "reason": "unparseable screen response — kept (fail-safe)"}
    keep = parsed.get("keep", True)
    if not isinstance(keep, bool):
        keep = str(keep).strip().lower() not in ("false", "no", "0", "drop")
    reason = str(parsed.get("reason", "")).strip() or (
        "plausible fit" if keep else "clear mismatch"
    )
    return {"keep": keep, "reason": reason}


# ---------------------------------------------------------------------------
# Already-tracked dedup (pure — unit-tested directly)
# ---------------------------------------------------------------------------


def dedupe_tracked(
    candidates: "list[dict]", tracked_names: "list[str]"
) -> "tuple[list[dict], list[tuple[dict, str]]]":
    """Split fresh candidates into (to_screen, dup_drops).

    A candidate whose name variant-matches an already-tracked company is a
    duplicate and is dropped (never re-enriched). Reuses the save-layer matcher
    so the dedup rule is defined in exactly one place."""
    from company_registry import company_name_variants_match

    to_screen: list[dict] = []
    dup_drops: list[tuple[dict, str]] = []
    for row in candidates:
        name = row["canonical_name"]
        match = next((t for t in tracked_names if company_name_variants_match(name, t)), None)
        if match is not None:
            dup_drops.append((row, f"already tracked as {match}"))
        else:
            to_screen.append(row)
    return to_screen, dup_drops


# ---------------------------------------------------------------------------
# The screen (LLM call injected for tests)
# ---------------------------------------------------------------------------


def screen_candidates(candidates: "list[dict]", system_prompt: str, call_llm) -> "list[dict]":
    """Screen each candidate; return decisions ``[{row, keep, reason}, ...]``.

    ``call_llm(system, user) -> str`` is injected so tests mock the model. One
    request per candidate (never batched — STRATEGY guardrail 6). Any per-call
    exception fails safe to KEEP (a screen must not drop on an API error)."""
    decisions: list[dict] = []
    for row in candidates:
        user = build_user_message(row["canonical_name"], row.get("description", ""))
        try:
            text = call_llm(system_prompt, user)
            decision = parse_decision(text)
        except Exception as exc:  # noqa: BLE001 — an API error must not drop a company
            decision = {
                "keep": True,
                "reason": f"screen error, kept (fail-safe): {type(exc).__name__}",
            }
        decisions.append({"row": row, "keep": decision["keep"], "reason": decision["reason"]})
    return decisions


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def load_fresh_candidates(conn, limit: int = 0) -> "list[dict]":
    """Fresh board-discovered candidates: status='candidate', not yet scored.

    Includes ghost candidates (no website yet) on purpose — dropping a clear
    mismatch HERE also saves the paid website-search that would otherwise run on
    it. Ordered by name so a --limit run screens a stable prefix."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, canonical_name, description, category "
        "FROM company "
        "WHERE status = 'candidate' AND alignment_score IS NULL "
        "ORDER BY canonical_name"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


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


def apply_drops(conn, drops: "list[tuple[dict, str]]") -> None:
    """Set dropped candidates inactive with a ``screen:`` status_reason, after
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
                "source": "screen_candidates.py --apply",
                "count": len(drops),
                "companies": [
                    {
                        "id": str(row["id"]),
                        "canonical_name": row["canonical_name"],
                        "reason": reason,
                    }
                    for row, reason in drops
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cur = conn.cursor()
    for row, reason in drops:
        cur.execute(
            "UPDATE company SET status = 'inactive', status_reason = %s "
            "WHERE id = %s AND status = 'candidate'",
            (f"{_DROP_PREFIX}: {reason}", row["id"]),
        )
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_screen(conn, call_llm, limit: int = 0) -> "dict":
    """Dedup + screen the fresh candidates; return a decision summary.

    Never writes — the caller applies drops when ``--apply`` is set. ``call_llm``
    may be ``None`` (no credentials): then the LLM cut is skipped and every
    non-duplicate candidate is kept (the safe direction)."""
    candidates = load_fresh_candidates(conn, limit)
    if not candidates:
        return {"total": 0, "keep": [], "drop": [], "screened": 0, "llm_ran": call_llm is not None}

    tracked = load_tracked_names(conn)
    to_screen, dup_drops = dedupe_tracked(candidates, tracked)

    keep: list[tuple[dict, str]] = []
    drop: list[tuple[dict, str]] = list(dup_drops)

    if call_llm is None:
        keep.extend((row, "kept — no LLM credentials, screen skipped") for row in to_screen)
    else:
        system_prompt = screen_system_prompt()
        for d in screen_candidates(to_screen, system_prompt, call_llm):
            (keep if d["keep"] else drop).append((d["row"], d["reason"]))

    return {
        "total": len(candidates),
        "keep": keep,
        "drop": drop,
        "screened": len(to_screen),
        "llm_ran": call_llm is not None,
    }


# ---------------------------------------------------------------------------
# Anthropic call (real credentials path)
# ---------------------------------------------------------------------------


def build_call_llm(model_tier: str):
    """Return a ``call_llm(system, user) -> str`` bound to the cheap model, or
    ``None`` if the Anthropic client can't be constructed (no package / no
    credentials). Returning None keeps every candidate — the safe direction."""
    try:
        import anthropic
    except ImportError:
        print("  screen: anthropic package not installed — keeping all candidates", file=sys.stderr)
        return None

    model = _TIER_TO_MODEL.get(model_tier, _TIER_TO_MODEL["haiku"])
    try:
        client = anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001 — no credentials is a keep-all, not a crash
        print(f"  screen: no Anthropic client ({exc}) — keeping all candidates", file=sys.stderr)
        return None

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
# Main
# ---------------------------------------------------------------------------


def _print_summary(summary: dict, applied: bool) -> None:
    kept, dropped = summary["keep"], summary["drop"]
    head = "APPLIED" if applied else "DRY-RUN"
    print(
        f"[{head}] {summary['total']} fresh candidate(s): "
        f"{len(kept)} kept, {len(dropped)} dropped "
        f"({'LLM screen ran' if summary['llm_ran'] else 'LLM screen SKIPPED — no credentials'})"
    )
    for row, reason in dropped:
        print(f"  DROP  {row['canonical_name']}: {reason}")
    for row, reason in kept:
        print(f"  keep  {row['canonical_name']}: {reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cheap relevance screen before company enrichment")
    parser.add_argument("--apply", action="store_true", help="Drop clear mismatches (writes DB)")
    parser.add_argument("--limit", type=int, default=0, help="Cap candidates screened this run")
    parser.add_argument(
        "--model", type=str, default="", help="Model tier override (haiku/sonnet/opus)"
    )
    args = parser.parse_args()

    from scoring_settings import company_screen_model

    tier = (args.model or company_screen_model()).strip().lower()
    call_llm = build_call_llm(tier)

    conn = get_conn()
    summary = run_screen(conn, call_llm, limit=args.limit)

    if args.apply:
        apply_drops(conn, summary["drop"])
    _print_summary(summary, applied=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
