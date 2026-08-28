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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import-cheap (stdlib only, no DB/profile side effects), so it stays above the
# --help guard with the other early imports.
from llm_json import FLAT_SCORE_OBJECT, parse_llm_json  # noqa: E402

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
        "--archive",
        action="store_true",
        help="With --save: auto-archive unseen vacancies scoring below "
        "LLM_SCORE_THRESHOLD after saving (default: archival stays paused)",
    )
    parser.add_argument(
        "--scored-by",
        help="With --save: model tier that scored this batch (e.g. 'haiku' for a "
        "SCREEN pass, 'opus' for an ESCALATE pass) — recorded on vacancy.scored_by "
        "so a kept-cheap score is never indistinguishable from a confirmed strong "
        "score. Omit to leave scored_by unset.",
    )
    parser.add_argument(
        "--unattended",
        action="store_true",
        help="Nightly-run mode: offer oldest unscored roles first, so a capped "
        "run drains the backlog instead of forever chasing the newest fetch",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        metavar="FILE",
        help="With --save: read individual result files instead of stdin, "
        "tolerating a malformed one (BUG-5) — a bad file is named and skipped, "
        "the rest still save.",
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
import quality  # noqa: E402
from prompts import VACANCY_SCORING_PROMPT as SYSTEM_PROMPT  # noqa: E402
from prompts import VACANCY_SCORING_USER_TEMPLATE as USER_TEMPLATE  # noqa: E402
from config import GEO_ONSITE_PENALTY, GEO_ONSITE_OK_SET  # noqa: E402
from geo import region_for_country, is_remote_mode  # noqa: E402

sys.stdout = _real_stdout


# ---------------------------------------------------------------------------
# Inline utilities
# ---------------------------------------------------------------------------


def _parse_json(text: str) -> dict:
    """Parse JSON from an LLM response, handling fences and preamble.

    Thin adapter over the shared ``llm_json.parse_llm_json`` with the vacancy
    brace fallback (a flat ``{"score": N}`` object).
    """
    return parse_llm_json(text, FLAT_SCORE_OBJECT)


def _sanitize_text(text: str) -> str:
    """Normalize text for safe API submission."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ")
    text = "".join(c for c in text if c == "\n" or c == "\t" or (ord(c) >= 32 and ord(c) != 127))
    return text


def _build_user_msg(vacancy: dict) -> str:
    """Build user message for scoring — identical for all backends.

    Vacancy scoring is independent of company scoring (KTD4): the company
    alignment_score is intentionally not part of the prompt input.
    """
    description = (
        vacancy.get("full_description") or vacancy.get("snippet") or "No description available"
    )
    description = _sanitize_text(description)
    if len(description) > 8000:
        description = description[:8000] + "\n\n[Description truncated]"

    locs = vacancy.get("locations", [])
    loc_parts = []
    for loc in locs:
        # Try structured v2 fields first, fall back to v1 'location' text
        parts = [
            p
            for p in [loc.get("city"), loc.get("country"), loc.get("region"), loc.get("work_mode")]
            if p
        ]
        if not parts:
            # v1 fallback: 'location' key contains free-text like "Washington, DC metro area"
            v1_loc = loc.get("location")
            if v1_loc:
                parts = [v1_loc]
        if parts:
            loc_parts.append(", ".join(parts))
    location_str = "; ".join(loc_parts)

    tier = vacancy.get("tier") or vacancy.get("company_tier") or "?"

    return USER_TEMPLATE.format(
        org=vacancy["org"],
        tier=tier,
        title=vacancy["title"],
        location=location_str or "",
        description=description,
    )


# ---------------------------------------------------------------------------
# Data loading + dedup
# ---------------------------------------------------------------------------


def _load_and_dedup(
    *,
    force=False,
    include_passed=False,
    include_candidates=True,
    limit=None,
    offset=0,
    unattended=False,
):
    """Load vacancies, dedup by (org, title), filter.

    Returns: (roles, fitness_map, stats)
        roles = [(key, rep, members), ...]

    include_candidates=True (default) also pulls strong unscored vacancies from
    *candidate* companies (alignment >= floor or unscored), capped per run, so a
    forgotten company's strong role still gets scored. Disable with
    --no-candidates.

    unattended=True (--unattended) orders roles oldest-unscored-first, so the
    nightly capped run drains the backlog instead of always scoring the newest
    fetch and re-deferring the same old rows every night.

    Note: geography filtering (US/CIS/rest-of-world) lives in the pre-score
    filter (filter_vacancies.py), not here — the filter pass records its
    exclusions on vacancy.scoring_excluded_reason and the loader skips them.
    The blind/boilerplate gate below is deliberately KEPT alongside that
    record for one proven night before it is removed.
    """
    from database_supabase import (
        load_vacancies,
        get_company_fitness_map,
        load_candidate_vacancies_for_scoring,
    )

    # Skip already-decided vacancies by default — /score should not waste
    # subagent budget on auto-passed expired or user-skipped rows.
    status_exclude = None if include_passed else ["passed", "skipped"]
    vacancies = (
        load_vacancies(status_exclude=status_exclude)
        if force
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
                file=sys.stderr,
                flush=True,
            )

    role_groups: dict[tuple[str, str], list[dict]] = {}
    for v in vacancies.values():
        key = (v["org"], v["title"])
        role_groups.setdefault(key, []).append(v)

    roles = []
    stats = {
        "blacklisted": 0,
        "company_title_filtered": 0,
        "blind": 0,
        "total": len(vacancies),
        "candidates": candidate_count,
    }
    for key, members in role_groups.items():
        rep = max(
            members,
            key=lambda m: len(m.get("full_description") or m.get("snippet") or ""),
        )
        # Compute desc up-front so blacklist can check description-level kills.
        desc = rep.get("full_description") or rep.get("snippet") or ""
        if filters.title_words_blacklisted(rep["title"]) or filters.description_words_blacklisted(
            desc
        ):
            stats["blacklisted"] += 1
            continue
        # Per-company INCLUDE-filter — for a listed company, keep the role out
        # of scoring unless it matches the company's include-list. The body goes
        # in too: a description-scoped pattern keeps a role on its body, and
        # fetch time already honoured that, so judging on the title alone here
        # would drop a role the fetch deliberately kept.
        ctf_reason = filters.company_title_filter_reason(rep["org"], rep["title"], desc)
        if ctf_reason:
            stats["company_title_filtered"] += 1
            print(
                f"  [COMPANY TITLE FILTER] {rep['org']:25s} {rep['title'][:50]} ({ctf_reason})",
                file=sys.stderr,
                flush=True,
            )
            continue
        # Blind / junk gate — skip a role with no real job content behind it.
        # Either the desc is empty, OR it is pure page boilerplate (nav chrome,
        # inline page scripts, a cookie wall, an ATS 'job gone' page). Either
        # way the LLM would score on the title alone, so keep it out of scoring
        # until a real enrich succeeds — never scored on chrome. This backstops
        # the save-time quality gate (quality.clean_description): it also drops a
        # junk row already persisted before that gate could catch it (a 5.7K-char
        # UNICEF listing page full of jQuery once scored 55 on the title alone).
        if not desc.strip() or quality.is_boilerplate_junk(desc):
            stats["blind"] += 1
            reason = "no description" if not desc.strip() else "boilerplate/no real content"
            print(
                f"  [BLIND SKIP] {rep['org']:25s} {rep['title'][:50]} ({reason})",
                file=sys.stderr,
                flush=True,
            )
            continue
        if not force and rep.get("llm_score") is not None and rep.get("llm_score") != -1:
            continue
        roles.append((key, rep, members))

    if unattended:
        # Oldest-unscored-first: load order is created_at DESC, so a capped run
        # would otherwise starve the backlog forever.
        roles.sort(key=lambda r: r[1].get("created_at") or "")

    roles = roles[offset:]
    # Count schedulable roles (post-filter, post-offset) BEFORE the cap so the
    # caller can print an honest "scored X of Y" line.
    stats["roles_available"] = len(roles)
    if limit:
        roles = roles[:limit]

    return roles, fitness_map, stats


# ---------------------------------------------------------------------------
# Shared: build score_data
# ---------------------------------------------------------------------------


#: The scorer contract (scripts/prompts/vacancy-scoring.md) is an INTEGER 0-100.
_SCORE_MIN, _SCORE_MAX = 0, 100


def _coerce_score(raw):
    """Coerce an agent-supplied score to a whole int in [0, 100], or None.

    The scoring prompt asks for an integer 0-100, but a bare-LLM slip can emit
    999, 3.7, "high" or a bool. Those must never reach the DB — and from there
    public/data.js — verbatim (this is the save-time guard, the last line before
    the write). Returns None for anything that is not a whole number in range;
    the caller skips that entry loudly, exactly like a missing member_ids or a
    UUID-not-found row, so the vacancy stays unscored and is re-offered next run
    rather than surfacing a garbage score.
    """
    if isinstance(raw, bool):  # bool is an int subclass — never a valid score
        return None
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            return None
        val = int(raw)
    elif isinstance(raw, str):
        if not re.fullmatch(r"\s*-?\d+\s*", raw):
            return None
        val = int(raw)
    else:
        return None
    if val < _SCORE_MIN or val > _SCORE_MAX:
        return None
    return val


def _apply_onsite_penalty(score: int, country: str, work_mode: str) -> int:
    """Subtract the profile's on-site penalty for a non-remote role outside the
    no-penalty regions (makes remote roles preferable). Pure score nudge — hard
    geography bans are enforced separately (pre-score filter + save-time net).

    No-op when the profile sets no penalty, when the role is remote, when the
    country is unknown, or when its region is in onsite_ok_regions.
    """
    if not GEO_ONSITE_PENALTY or is_remote_mode(work_mode):
        return score
    region = region_for_country(country or "")
    if not region or region in GEO_ONSITE_OK_SET:
        return score
    return max(0, score - GEO_ONSITE_PENALTY)


def _make_score_data(result: dict, rep: dict) -> dict:
    """Build score_data dict from an LLM result.

    Hard geography bans are enforced in the pre-score filter (delete_geo) and a
    save-time net (update_llm_score). Here we apply only the SOFT on-site penalty
    that nudges non-remote roles outside the preferred regions down the list.
    """
    score = result["score"]
    country = (result.get("country") or "").strip()
    work_mode = (result.get("work_mode") or "").strip()
    adjusted = _apply_onsite_penalty(score, country, work_mode)
    reasoning = result["reasoning"]
    if adjusted != score:
        reasoning = f"{reasoning} [geo: -{score - adjusted} on-site outside preferred regions]"
    data = {
        "llm_score": adjusted,
        "llm_reasoning": reasoning,
        "llm_summary": result["short_summary"],
        "llm_hard_requirements": result.get("hard_requirements", []),
        # Carried for the save-time geo ban net (not DB columns themselves).
        "country": country,
        "work_mode": work_mode,
    }
    # US work-eligibility (orthogonal to the fit score): outside_us_ok | us_only
    # | unclear. Only persisted when the subagent supplied a recognised value.
    elig = result.get("us_eligibility")
    if elig in ("outside_us_ok", "us_only", "unclear"):
        data["us_eligibility"] = elig
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
    from scoring_settings import max_per_run, scoring_model

    _real_stdout = sys.stdout
    sys.stdout = sys.stderr

    # An explicit --limit wins; otherwise the per-run spike-day cap applies so a
    # burst day (hundreds of new roles at once) can't silently burn the plan.
    cap = args.limit if args.limit is not None else max_per_run()

    roles, _fitness_map, stats = _load_and_dedup(
        force=args.force,
        include_passed=args.include_passed,
        include_candidates=not args.no_candidates,
        limit=cap,
        offset=args.offset,
        unattended=getattr(args, "unattended", False),
    )

    available = stats.get("roles_available", len(roles))

    if not roles:
        sys.stdout = _real_stdout
        json.dump([], _real_stdout, ensure_ascii=False)
        return

    # Honest one-liner: which model will score, and X of Y this run.
    print(
        f"Scoring model: {scoring_model()} "
        f"(set '[## VOLUME] scoring_model' in your profile to change).",
        file=sys.stderr,
    )
    print(
        f"Scoring {len(roles)} of {available} new vacancies for subagent scoring", file=sys.stderr
    )
    if len(roles) < available and args.limit is None:
        deferred = available - len(roles)
        print(
            f"  Per-run cap reached ({cap}): {deferred} more will be offered next run. "
            f"Raise '[## VOLUME] max_per_run' or pass --limit to score more now.",
            file=sys.stderr,
        )

    output = []
    for _key, rep, members in roles:
        user_msg = _build_user_msg(rep)

        output.append(
            {
                "payload_kind": "vacancy",
                "id": rep["id"],
                "member_ids": [m["id"] for m in members],
                "org": rep["org"],
                "title": rep["title"],
                "system_prompt": SYSTEM_PROMPT,
                "user_msg": user_msg,
            }
        )

    sys.stdout = _real_stdout
    json.dump(output, _real_stdout, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Mode: --save
# ---------------------------------------------------------------------------


def cmd_save(args):
    """Read scored results from stdin JSON (or --files), save to DB.

    With --files, each result file is parsed independently via
    llm_json.read_result_files — a malformed one (truncated by a kill,
    unescaped quotes) is named and skipped, never failing the rest of the
    batch (BUG-5). Without --files, stdin must still be one valid JSON blob
    (a single bad entry there corrupts the surrounding array syntax and can't
    be split apart after the fact) — that failure is reported clearly instead
    of crashing with a raw traceback.
    """
    from database_supabase import update_llm_score, get_conn
    from llm_json import read_result_files

    bad_files: list[str] = []
    files = getattr(args, "files", None)
    if files:
        data, bad_files = read_result_files(files)
        if bad_files:
            print(
                f"WARNING: {len(bad_files)} malformed result file(s) skipped — re-score these:",
                file=sys.stderr,
            )
            for b in bad_files:
                print(f"  - {b}", file=sys.stderr)
    else:
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(
                f"ERROR: stdin is not valid JSON ({exc}) — nothing saved. Save "
                "each subagent's raw output to its own file and pass "
                "--files f1.json f2.json ... so one malformed result doesn't "
                "block the rest.",
                file=sys.stderr,
            )
            return

    if not data:
        if not bad_files:
            print("No results to save.")
        return

    # Score provenance for this whole batch (one --save call = one pass, one
    # model — see _vacancy_gate_text in run_daily.py). Omitted -> unset.
    scored_by = getattr(args, "scored_by", None)

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
        if score_data:
            coerced = _coerce_score(score_data.get("llm_score"))
            if coerced is None:
                print(
                    f"ERROR: invalid score {score_data.get('llm_score')!r} in "
                    f"score_data (must be an integer 0-100) — score not saved "
                    f"for {entry.get('org', '?')} — {entry.get('title', '?')}",
                    file=sys.stderr,
                )
                errors += 1
                continue
            score_data["llm_score"] = coerced
        if not score_data:
            if "score" in entry:
                score = _coerce_score(entry["score"])
                if score is None:
                    print(
                        f"ERROR: invalid score {entry['score']!r} (must be an "
                        f"integer 0-100) — score not saved for "
                        f"{entry.get('org', '?')} — {entry.get('title', '?')}",
                        file=sys.stderr,
                    )
                    errors += 1
                    continue
                result = {
                    "score": score,
                    "reasoning": entry.get("reasoning", ""),
                    "short_summary": entry.get("short_summary", ""),
                    "hard_requirements": entry.get("hard_requirements", []),
                    "country": entry.get("country"),
                    "work_mode": entry.get("work_mode"),
                    "us_eligibility": entry.get("us_eligibility"),
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

        if scored_by:
            score_data.setdefault("scored_by", scored_by)

        # Warn on short summaries
        summary = score_data.get("llm_summary", "")
        if summary and len(summary) < 200:
            print(
                f"WARNING: Short summary ({len(summary)} chars) for "
                f"{entry.get('org', '?')} — {entry.get('title', '?')}",
                file=sys.stderr,
            )

        # A malformed entry with no member_ids must be skipped, never allowed to
        # raise a KeyError out of the loop — that would abort before the single
        # batch commit below and roll back EVERY good score already staged.
        member_ids = entry.get("member_ids")
        if not member_ids:
            print(
                f"ERROR: entry missing member_ids — score not saved for "
                f"{entry.get('org', '?')} — {entry.get('title', '?')}",
                file=sys.stderr,
            )
            errors += 1
            continue

        for member_id in member_ids:
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
    summary = f"Saved {len(data) - errors} scores ({total_records} records). Errors: {errors}"
    if bad_files:
        summary += f". Skipped {len(bad_files)} malformed file(s) — see warnings above"
    print(summary)

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

    if getattr(args, "archive", False):
        # archive_vacancies OWNS its transaction: it commits the DELETE itself
        # and only then writes the on-disk JSON, so the two can't diverge (the
        # one deliberate exception to the caller-commits DAL rule). No commit
        # needed here — and no reliance on generate_dashboard()'s snapshot commit
        # as a side effect (which only fires in full mode).
        archived = archive_vacancies(force=True)
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
