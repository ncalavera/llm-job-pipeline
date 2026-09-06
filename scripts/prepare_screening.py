#!/usr/bin/env python3
"""Screening preparation: one LLM read per vacancy that
extracts structured facts with quotes and compares them with the profile.
No score. The night run dispatches the payloads to subagents; this script
only selects, validates and saves.

    prepare_screening.py --local [--limit N]                  # payload JSON → stdout
    prepare_screening.py --save --files f1.json ... [--prepared-by opus]   # results → DB

Selection (``--local``): undecided (``unseen``) roles with a real description,
not excluded by the filter pass, at active or candidate companies — company
approval is not a prerequisite. Inactive companies stay out of the pilot until
their veto provenance is reviewed (ticket U3). A role already prepared for the
same posting + prompt + profile is never re-sent; a changed posting or profile
invalidates the stored result. Only roles first seen within
``[screening] window_days`` are considered, oldest first, capped at
``nightly_limit`` (or ``--limit``). With ``--pilot`` the cohort is instead
filled round-robin across score band × company status so a pilot stays
mixed, capped at ``pilot_limit``.

Saving (``--save``): every result is validated — known id, allowed enums,
every quote present in the posting text — before it is written. A result that
fails validation is stored as ``screening_state='failed'`` with the reason,
so it stays visible and retryable; nothing is fabricated to fill a field.
Preparation never writes a vacancy status.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Keep stdout pure JSON in --local mode: every banner goes to stderr.
_real_stdout = sys.stdout
if "--save" not in sys.argv:
    sys.stdout = sys.stderr

import quality  # noqa: E402
import settings  # noqa: E402
from llm_json import read_result_files  # noqa: E402
from prompts import VACANCY_SCREENING_PROMPT as SYSTEM_PROMPT  # noqa: E402

sys.stdout = _real_stdout

USER_TEMPLATE = """Prepare this posting for screening.

**Organization:** {org}
**Job Title:** {title}
**Location:** {location}

**Posting text:**
{description}"""

MAX_DESC = 8000

ENUMS = {
    "seniority": {"junior", "mid", "senior", "head", "director", "executive", "unknown"},
    "employment_type": {
        "permanent",
        "fixed-term",
        "contract",
        "consultancy",
        "internship",
        "unknown",
    },
    "work_mode": {"remote", "hybrid", "onsite", "unknown"},
}
REQ_KINDS = {
    "language",
    "experience",
    "education",
    "skill",
    "domain",
    "location",
    "authorisation",
    "other",
}
STRENGTHS = {"required", "preferred", "unknown"}
FINDINGS = {"match", "possible_conflict", "unknown"}
MAX_REQUIREMENTS = 40
MAX_QUOTE = 600


# ---------------------------------------------------------------------------
# Fingerprints — what "unchanged" means
# ---------------------------------------------------------------------------


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def prompt_fingerprint() -> str:
    """Prompt template + rendered profile, as one hash: a profile edit or a
    prompt edit invalidates every interpretation."""
    return _sha(SYSTEM_PROMPT)


def posting_fingerprint(description: str) -> str:
    return _sha(_norm_ws(description)[:MAX_DESC])


def fingerprint(description: str) -> str:
    return f"{posting_fingerprint(description)}:{prompt_fingerprint()}"


# ---------------------------------------------------------------------------
# Selection (--local)
# ---------------------------------------------------------------------------


def _score_band(score) -> str:
    if score is None or score < 0:
        return "unscored"
    if score < 15:
        return "0-14"
    if score < 35:
        return "15-34"
    return "35+"


def pick_cohort(rows: list[dict], limit: int) -> list[dict]:
    """Round-robin across (score band, company status) strata, oldest first
    inside a stratum, until ``limit`` — a mixed pilot cohort by construction.
    Pure function: ``rows`` are already-eligible vacancies."""
    strata: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (_score_band(r.get("llm_score")), r.get("company_status") or "?")
        strata.setdefault(key, []).append(r)
    for bucket in strata.values():
        bucket.sort(key=lambda r: str(r.get("first_seen") or ""))
    out: list[dict] = []
    keys = sorted(strata)
    while len(out) < limit and any(strata[k] for k in keys):
        for k in keys:
            if strata[k] and len(out) < limit:
                out.append(strata[k].pop(0))
    return out


def _location_str(locs) -> str:
    parts = []
    for loc in locs or []:
        if not isinstance(loc, dict):
            continue
        bits = [p for p in (loc.get("city"), loc.get("country"), loc.get("work_mode")) if p]
        if not bits and loc.get("location"):
            bits = [loc["location"]]
        if bits:
            parts.append(", ".join(bits))
    return "; ".join(parts)


def _description(row: dict) -> str:
    return (row.get("full_description") or "").strip()


def eligible(row: dict) -> bool:
    """A role the night may prepare: real description, not already prepared
    for this exact posting + prompt + profile."""
    desc = _description(row)
    if len(desc) < 200 or quality.is_boilerplate_junk(desc):
        return False
    if row.get("screening_state") == "ready" and row.get("screening_fingerprint") == fingerprint(
        desc
    ):
        return False
    return True


def load_pool(window_days: int) -> list[dict]:
    """Undecided, filter-kept roles at active or candidate companies, first
    seen within ``window_days``, oldest first, with the columns selection and
    payload building need. Light on purpose: the description is the only large
    column. ``first_seen`` is an ISO date on both backends, so a string cutoff
    compares correctly without dialect branching."""
    from database_supabase import get_conn
    from db_backend import RealDictCursor

    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    cur = get_conn().cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT v.id, v.title, v.full_description, v.locations, v.llm_score,
               v.first_seen, v.screening_state, v.screening_fingerprint,
               c.canonical_name AS org, c.status AS company_status
        FROM vacancy v JOIN company c ON v.company_id = c.id
        WHERE v.status = 'unseen'
          AND v.scoring_excluded_reason IS NULL
          AND c.status IN ('active', 'candidate')
          AND v.full_description IS NOT NULL
          AND v.first_seen >= %s
        ORDER BY v.first_seen, v.id
        """,
        (cutoff,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    for r in rows:
        r["id"] = str(r["id"])
        locs = r.get("locations")
        if isinstance(locs, str):
            try:
                r["locations"] = json.loads(locs)
            except Exception:
                r["locations"] = []
    return rows


def build_payload(row: dict) -> dict:
    desc = _description(row)
    if len(desc) > MAX_DESC:
        desc = desc[:MAX_DESC] + "\n\n[Description truncated]"
    return {
        "payload_kind": "screening",
        "id": row["id"],
        "org": row.get("org") or "?",
        "title": row.get("title") or "?",
        "fingerprint": fingerprint(_description(row)),
        "system_prompt": SYSTEM_PROMPT,
        "user_msg": USER_TEMPLATE.format(
            org=row.get("org") or "?",
            title=row.get("title") or "?",
            location=_location_str(row.get("locations")),
            description=desc,
        ),
    }


def cmd_local(args) -> None:
    cfg = settings.screening()
    pool = [r for r in load_pool(cfg["window_days"]) if eligible(r)]
    if args.pilot:
        limit = args.limit or cfg["pilot_limit"]
        cohort = pick_cohort(pool, limit)
    else:
        limit = args.limit or cfg["nightly_limit"]
        cohort = pool[:limit]
    print(
        f"Screening prep: {len(pool)} role(s) waiting, preparing {len(cohort)} (cap {limit})",
        file=sys.stderr,
        flush=True,
    )
    json.dump([build_payload(r) for r in cohort], sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# Validation + save (--save)
# ---------------------------------------------------------------------------


def _quote_in(quote: str, text: str) -> bool:
    q = _norm_ws(quote)
    return bool(q) and len(q) <= MAX_QUOTE and q.lower() in text.lower()


def validate_result(result: dict, posting_text: str) -> tuple[dict | None, str | None]:
    """Return ``(clean, None)`` or ``(None, reason)``. ``clean`` keeps only the
    contract's fields; a quote that is not in the posting fails the whole
    result — provenance is the point of this pass."""
    if not isinstance(result, dict):
        return None, "result is not a JSON object"
    if result.get("failed"):
        return None, f"subagent failed: {str(result['failed'])[:200]}"
    facts = result.get("posting_facts")
    if not isinstance(facts, dict):
        return None, "posting_facts missing"
    text = _norm_ws(posting_text)
    clean_facts: dict = {}
    for key in (
        "duties",
        "function",
        "compensation",
        "location",
        "work_authorisation",
        "deadline",
    ):
        val = facts.get(key)
        clean_facts[key] = str(val)[:1000] if isinstance(val, str) and val.strip() else None
    for key, allowed in ENUMS.items():
        val = facts.get(key)
        if val not in allowed:
            return None, f"{key}={val!r} not in {sorted(allowed)}"
        clean_facts[key] = val
    reqs = facts.get("requirements")
    if not isinstance(reqs, list):
        return None, "requirements must be a list"
    if len(reqs) > MAX_REQUIREMENTS:
        return None, f"{len(reqs)} requirements (max {MAX_REQUIREMENTS})"
    clean_reqs = []
    for i, req in enumerate(reqs):
        if not isinstance(req, dict):
            return None, f"requirement {i} is not an object"
        if req.get("kind") not in REQ_KINDS:
            return None, f"requirement {i}: kind={req.get('kind')!r}"
        if req.get("strength") not in STRENGTHS:
            return None, f"requirement {i}: strength={req.get('strength')!r}"
        quote = req.get("quote")
        if not isinstance(quote, str) or not _quote_in(quote, text):
            return None, f"requirement {i}: quote not found in the posting"
        clean_reqs.append(
            {
                "kind": req["kind"],
                "value": str(req.get("value") or "")[:300],
                "strength": req["strength"],
                "quote": _norm_ws(quote),
            }
        )
    clean_facts["requirements"] = clean_reqs
    comps = result.get("profile_comparison")
    if not isinstance(comps, list):
        return None, "profile_comparison must be a list"
    clean_comps = []
    for i, c in enumerate(comps):
        if not isinstance(c, dict):
            return None, f"comparison {i} is not an object"
        idx = c.get("requirement")
        if not isinstance(idx, int) or not 0 <= idx < len(clean_reqs):
            return None, f"comparison {i}: requirement index {idx!r} out of range"
        if c.get("finding") not in FINDINGS:
            return None, f"comparison {i}: finding={c.get('finding')!r}"
        clean_comps.append(
            {
                "requirement": idx,
                "profile_factor": str(c.get("profile_factor") or "")[:300],
                "finding": c["finding"],
                "note": str(c.get("note") or "")[:500],
            }
        )
    unknowns = result.get("unknowns")
    if not isinstance(unknowns, list):
        unknowns = []
    return {
        "posting_facts": clean_facts,
        "profile_comparison": clean_comps,
        "unknowns": [str(u)[:300] for u in unknowns[:20]],
    }, None


def _load_postings(ids: list[str]) -> dict[str, str]:
    """``{vacancy id: posting text}`` — the text every quote is checked against."""
    from database_supabase import get_conn

    if not ids:
        return {}
    cur = get_conn().cursor()
    cur.execute("SELECT id, full_description FROM vacancy WHERE id = ANY(%s::uuid[])", (ids,))
    out = {str(r[0]): r[1] or "" for r in cur.fetchall()}
    cur.close()
    return out


def save_result(vac_id: str, payload: dict, state: str, fp: str, prepared_by: str | None):
    from database_supabase import get_conn
    from db_backend import Json

    body = dict(payload)
    body["model"] = prepared_by
    body["prompt_version"] = prompt_fingerprint()
    cur = get_conn().cursor()
    cur.execute(
        "UPDATE vacancy SET screening = %s, screening_state = %s, "
        "screening_prepared_at = %s, screening_fingerprint = %s WHERE id = %s",
        (Json(body), state, datetime.now(timezone.utc).isoformat(), fp, vac_id),
    )
    cur.close()


def cmd_save(args) -> None:
    from database_supabase import get_conn

    data, bad_files = read_result_files(args.files)
    for b in bad_files:
        print(f"WARNING: malformed result file skipped — retry it: {b}", file=sys.stderr)
    if not data:
        print("No results to save.")
        return

    ids = [str(d.get("id")) for d in data if isinstance(d, dict) and d.get("id")]
    postings = _load_postings(ids)
    ready = failed = errors = 0
    for entry in data:
        vac_id = str(entry.get("id") or "") if isinstance(entry, dict) else ""
        if vac_id not in postings:
            print(f"ERROR: unknown vacancy id {vac_id!r} — result not saved", file=sys.stderr)
            errors += 1
            continue
        desc = postings[vac_id]
        fp = fingerprint(desc)
        clean, reason = validate_result(entry, desc)
        if clean is None:
            save_result(vac_id, {"failed": reason}, "failed", fp, args.prepared_by)
            print(f"FAILED {vac_id}: {reason}", file=sys.stderr)
            failed += 1
            continue
        save_result(vac_id, clean, "ready", fp, args.prepared_by)
        ready += 1
    get_conn().commit()
    summary = f"Saved {ready} prepared role(s), {failed} failed, {errors} error(s)"
    if bad_files:
        summary += f", {len(bad_files)} malformed file(s) skipped"
    print(summary)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true", help="payload JSON → stdout (default)")
    mode.add_argument("--save", action="store_true", help="results → DB")
    p.add_argument("--limit", type=int, help="override [screening] nightly_limit / pilot_limit")
    p.add_argument(
        "--pilot",
        action="store_true",
        help="round-robin across score band × company status, capped at pilot_limit",
    )
    p.add_argument("--prepared-by", help="with --save: model tier that prepared this batch")
    p.add_argument("--files", nargs="+", help="with --save: one result file per role")
    args = p.parse_args()
    if args.save:
        if not args.files:
            p.error("--save needs --files")
        cmd_save(args)
    else:
        cmd_local(args)


if __name__ == "__main__":
    main()
