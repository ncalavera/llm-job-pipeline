#!/usr/bin/env python3
"""Vacancy scoring — local subagent backend, deterministic LLM input.

Usage:
    score_vacancies.py --local --limit 5          # subagent scoring (default)
    score_vacancies.py --save                     # stdin JSON → DB (internal)
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TOKENS = 1024
PROMPT_VERSION = "v4.0_2026_pure_fit"
BILLING_ABORT_THRESHOLD = 5

# Default parallelism for the local subagent orchestrator.
MAX_CONCURRENT = 3


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser. Defined before the heavy project imports so
    ``--help`` / ``-h`` can print usage without connecting to the database or
    loading the user profile (those happen only when a real command runs)."""
    parser = argparse.ArgumentParser(description="Vacancy scoring")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true", help="JSON → stdout for subagents (default)")
    mode.add_argument("--save", action="store_true", help="stdin JSON → DB (internal)")

    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--include-passed",
        action="store_true",
        help="Score vacancies even if status='passed' or 'skipped' (default: skip them)",
    )
    parser.add_argument(
        "--no-candidates",
        action="store_true",
        help="Do NOT pull strong vacancies from candidate (unreviewed) companies "
             "(default: include them, capped per run)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-workers", type=int, default=MAX_CONCURRENT)
    parser.add_argument(
        "--archive", action="store_true",
        help="With --save: auto-archive unseen vacancies scoring below "
             "LLM_SCORE_THRESHOLD after saving (default: archival stays paused)",
    )
    return parser


# Print help and exit BEFORE importing anything that touches the DB or profile.
from cli_help import wants_help
if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

# Redirect stdout → stderr during imports so db_conn diagnostics don't pollute
# JSON output in --local mode
_real_stdout = sys.stdout
if "--local" in sys.argv or "--save" not in sys.argv:
    sys.stdout = sys.stderr

import filters  # noqa: E402
from prompts import VACANCY_SCORING_PROMPT as SYSTEM_PROMPT  # noqa: E402
from prompts import VACANCY_SCORING_USER_TEMPLATE as USER_TEMPLATE  # noqa: E402

sys.stdout = _real_stdout


# ---------------------------------------------------------------------------
# Inline utilities
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    """Parse JSON from LLM response, handling fences, preamble."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    brace = re.search(r'\{[^{}]*"score"\s*:\s*\d+[^{}]*\}', text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return {"error": "JSON parse failed", "raw": text[:500]}


def _sanitize_text(text: str) -> str:
    """Normalize text for safe API submission."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ")
    text = "".join(
        c for c in text
        if c == "\n" or c == "\t" or (ord(c) >= 32 and ord(c) != 127)
    )
    return text


def _build_user_msg(vacancy: dict, alignment_score=None) -> str:
    """Build user message for scoring — identical for all backends."""
    description = vacancy.get("full_description") or vacancy.get("snippet") or "No description available"
    description = _sanitize_text(description)
    if len(description) > 8000:
        description = description[:8000] + "\n\n[Description truncated]"

    locs = vacancy.get("locations", [])
    loc_parts = []
    for loc in locs:
        # Try structured v2 fields first, fall back to v1 'location' text
        parts = [p for p in [loc.get("city"), loc.get("country"), loc.get("region"), loc.get("work_mode")] if p]
        if not parts:
            # v1 fallback: 'location' key contains free-text like "Washington, DC metro area"
            v1_loc = loc.get("location")
            if v1_loc:
                parts = [v1_loc]
        if parts:
            loc_parts.append(", ".join(parts))
    location_str = "; ".join(loc_parts)

    alignment_str = f"{alignment_score}/100" if alignment_score is not None else "not enriched"

    tier = vacancy.get("tier") or vacancy.get("company_tier") or "?"

    return USER_TEMPLATE.format(
        org=vacancy["org"],
        tier=tier,
        title=vacancy["title"],
        location=location_str or "",
        description=description,
        alignment_score=alignment_str,
    )


# ---------------------------------------------------------------------------
# Data loading + dedup
# ---------------------------------------------------------------------------

def _load_and_dedup(*, force=False, include_passed=False,
                    include_candidates=True, limit=None, offset=0):
    """Load vacancies, dedup by (org, title), filter.

    Returns: (roles, fitness_map, stats)
        roles = [(key, rep, members), ...]

    include_candidates=True (default) also pulls strong unscored vacancies from
    *candidate* companies (alignment >= floor or unscored), capped per run, so a
    forgotten company's strong role still gets scored. Disable with
    --no-candidates.

    Note: geography filtering (US/CIS/rest-of-world) lives in the pre-score
    filter (filter_vacancies.py), not here.
    """
    from database_supabase import (
        load_vacancies, get_company_fitness_map,
        load_candidate_vacancies_for_scoring,
    )

    # Skip already-decided vacancies by default — /score should not waste
    # subagent budget on auto-passed expired or user-skipped rows.
    status_exclude = None if include_passed else ["passed", "skipped"]
    vacancies = (
        load_vacancies(status_exclude=status_exclude) if force
        else load_vacancies(unscored_only=True, status_exclude=status_exclude)
    )
    fitness_map = get_company_fitness_map()

    # Candidate rescue: merge in promising unscored vacancies from candidate
    # companies (capped). Keyed by UUID, so no double-counting with the active
    # pool above (which only contains active companies anyway).
    candidate_count = 0
    if include_candidates and not force:
        candidates = load_candidate_vacancies_for_scoring(
            status_exclude=status_exclude or ["passed", "skipped"],
        )
        for uid, vac in candidates.items():
            if uid not in vacancies:
                vacancies[uid] = vac
                candidate_count += 1
        if candidate_count:
            print(
                f"  [CANDIDATE RESCUE] +{candidate_count} vacancies from "
                f"candidate (unreviewed) companies",
                file=sys.stderr, flush=True,
            )

    role_groups: dict[tuple[str, str], list[dict]] = {}
    for v in vacancies.values():
        key = (v["org"], v["title"])
        role_groups.setdefault(key, []).append(v)

    roles = []
    stats = {"blacklisted": 0, "blind": 0, "total": len(vacancies),
             "candidates": candidate_count}
    for key, members in role_groups.items():
        rep = max(
            members,
            key=lambda m: len(m.get("full_description") or m.get("snippet") or ""),
        )
        # Compute desc up-front so blacklist can check description-level kills.
        desc = rep.get("full_description") or rep.get("snippet") or ""
        if (filters.title_words_blacklisted(rep["title"])
                or filters.description_words_blacklisted(desc)):
            stats["blacklisted"] += 1
            continue
        # Blind vacancy gate — skip if no description AND no snippet
        if not desc.strip():
            stats["blind"] += 1
            print(f"  [BLIND SKIP] {rep['org']:25s} {rep['title'][:50]} (no description)",
                  file=sys.stderr, flush=True)
            continue
        if not force and rep.get("llm_score") is not None and rep.get("llm_score") != -1:
            continue
        roles.append((key, rep, members))

    roles = roles[offset:]
    if limit:
        roles = roles[:limit]

    return roles, fitness_map, stats


# ---------------------------------------------------------------------------
# Shared: build score_data
# ---------------------------------------------------------------------------

def _make_score_data(result: dict, rep: dict) -> dict:
    """Build score_data dict from an LLM result.

    Geography is enforced in the pre-score filter (delete_geo), not here —
    scoring no longer caps by location.
    """
    score = result["score"]
    data = {
        "llm_score": score,
        "llm_reasoning": result["reasoning"],
        "llm_summary": result["short_summary"],
        "llm_hard_requirements": result.get("hard_requirements", []),
    }
    # LLM-extracted deadline (nullable, only fills gaps)
    dl = result.get("deadline")
    if dl and dl != "null":
        data["llm_deadline"] = dl
    return data


# ---------------------------------------------------------------------------
# Mode: --local
# ---------------------------------------------------------------------------

def cmd_local(args):
    """Output JSON to stdout for subagent scoring."""
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr

    roles, fitness_map, stats = _load_and_dedup(
        force=args.force,
        include_passed=args.include_passed,
        include_candidates=not args.no_candidates,
        limit=args.limit, offset=args.offset,
    )

    if not roles:
        sys.stdout = _real_stdout
        json.dump([], _real_stdout, ensure_ascii=False)
        return

    print(f"Preparing {len(roles)} roles for subagent scoring", file=sys.stderr)

    output = []
    for _key, rep, members in roles:
        company_info = fitness_map.get(rep["org"], {})
        alignment = company_info.get("alignment_score")
        user_msg = _build_user_msg(rep, alignment_score=alignment)

        output.append({
            "payload_kind": "vacancy",
            "id": rep["id"],
            "member_ids": [m["id"] for m in members],
            "org": rep["org"],
            "title": rep["title"],
            "system_prompt": SYSTEM_PROMPT,
            "user_msg": user_msg,
        })

    sys.stdout = _real_stdout
    json.dump(output, _real_stdout, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Mode: --save
# ---------------------------------------------------------------------------

def cmd_save(_args):
    """Read scored results from stdin JSON, save to DB."""
    from database_supabase import update_llm_score, get_conn

    data = json.load(sys.stdin)
    if not data:
        print("No results to save.")
        return

    conn = get_conn()
    total_records = 0
    errors = 0

    for entry in data:
        # payload_kind defaults to "vacancy" when omitted (the docs only ever
        # describe vacancy scoring), but reject any *other* explicit kind.
        kind = entry.get("payload_kind", "vacancy")
        if kind != "vacancy":
            print(
                f"ERROR: wrong payload_kind={kind!r}, "
                f"expected 'vacancy'. Skipping {entry.get('org', '?')} — "
                f"{entry.get('title', '?')}",
                file=sys.stderr,
            )
            errors += 1
            continue

        # Two accepted shapes:
        #   1. Strict: a pre-built nested ``score_data`` with DB column names.
        #   2. Flat (what AGENTS.md + /jobs-new tell agents to produce):
        #      top-level ``score`` / ``reasoning`` / ``short_summary`` /
        #      ``hard_requirements`` / ``tags``. Build score_data from it.
        score_data = entry.get("score_data")
        if not score_data:
            if "score" in entry:
                result = {
                    "score": entry["score"],
                    "reasoning": entry.get("reasoning", ""),
                    "short_summary": entry.get("short_summary", ""),
                    "hard_requirements": entry.get("hard_requirements", []),
                    "deadline": entry.get("deadline"),
                }
                score_data = _make_score_data(result, entry)
            else:
                print(
                    f"ERROR: missing both score_data and a top-level score for "
                    f"{entry.get('org', '?')} — {entry.get('title', '?')}",
                    file=sys.stderr,
                )
                errors += 1
                continue

        # Geography is enforced in the pre-score filter — no score cap here.

        # Warn on short summaries
        summary = score_data.get("llm_summary", "")
        if summary and len(summary) < 200:
            print(
                f"WARNING: Short summary ({len(summary)} chars) for "
                f"{entry.get('org', '?')} — {entry.get('title', '?')}",
                file=sys.stderr,
            )

        for member_id in entry["member_ids"]:
            rowcount = update_llm_score(member_id, score_data)
            if rowcount == 0:
                print(
                    f"WARNING: UUID {member_id} not found in DB — score not saved "
                    f"for {entry.get('org', '?')} — {entry.get('title', '?')}",
                    file=sys.stderr,
                )
                errors += 1
            else:
                total_records += 1

    conn.commit()
    print(f"Saved {len(data) - errors} scores ({total_records} records). Errors: {errors}")

    # Self-healing: normalize locations[] for any vacancy that has dirty
    # city/country. The optional cleanup_locations module is not part of the
    # public repo; when it is absent we simply skip this step silently (no
    # noisy "No module named 'cleanup_locations'" line on every --save).
    try:
        import importlib.util
        if importlib.util.find_spec("cleanup_locations") is not None:
            from cleanup_locations import normalize_location
            scored_ids = [m for entry in data for m in entry.get("member_ids", [])]
            if scored_ids:
                heal_cur = conn.cursor()
                heal_cur.execute(
                    "SELECT id, locations, full_description, snippet "
                    "FROM vacancy WHERE id = ANY(%s::uuid[]) AND locations IS NOT NULL",
                    ([str(i) for i in scored_ids],),
                )
                healed = 0
                for vid, locs, desc, snip in heal_cur.fetchall():
                    full_text = (desc or "") + "\n" + (snip or "")
                    new_locs = []
                    changed = False
                    for loc in locs:
                        cleaned, changes = normalize_location(loc, full_text)
                        if changes:
                            changed = True
                        new_locs.append(cleaned)
                    if changed:
                        from db_backend import Json
                        heal_cur.execute(
                            "UPDATE vacancy SET locations = %s WHERE id = %s",
                            (Json(new_locs), vid),
                        )
                        healed += 1
                heal_cur.close()
                if healed:
                    conn.commit()
                    print(f"Self-healed {healed} vacancies with dirty locations.")
    except Exception as e:
        # A failed statement poisons the shared connection — roll back so the
        # auto-archive + dashboard steps below still work.
        conn.rollback()
        print(f"Self-healing skipped: {e}", file=sys.stderr)

    # Auto-archive low-scoring unseen vacancies (finding #10). The DAL pauses
    # score-threshold archival by default under pure-fit scoring (a high score
    # in an excluded geography would be wrongly deleted), so it only fires when
    # the caller passes --archive. Without the flag we print one line so the
    # documented step is visibly run, not silently missing.
    from database_supabase import archive_vacancies
    if getattr(_args, "archive", False):
        archived = archive_vacancies(force=True)
        # DAL writes are not auto-committed (AGENTS.md) — persist the archival
        # explicitly here rather than relying on generate_dashboard()'s snapshot
        # commit as a side effect (which only fires in full mode, so simple-mode
        # archival would otherwise roll back at exit).
        conn.commit()
        print(f"Auto-archived {len(archived)} low-scoring unseen vacancies.")
    else:
        archive_vacancies()  # prints the "paused" notice; no-op without --archive

    # Regenerate dashboard so ticker and stats reflect new scores
    from report import generate_dashboard
    generate_dashboard()
    print("Dashboard regenerated.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = build_parser().parse_args()

    # Always announce the backend on stderr (safe even in --local mode where
    # stdout carries pure JSON for the subagents).
    from db_backend import print_backend_banner
    print_backend_banner(sys.stderr)

    if args.save:
        cmd_save(args)
    else:
        # Default to --local
        cmd_local(args)


if __name__ == "__main__":
    main()
