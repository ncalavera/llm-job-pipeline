#!/usr/bin/env python3
"""Weekly low-score audit — catch false negatives the scorer buried.

The scorer is the only thing standing between a good role and oblivion. This
tool re-examines a sample of recently low-scored roles (llm_score < THRESHOLD)
with an independent, skeptical "did we wrongly bury this?" pass and reports the
suspected misses for a human to recalibrate against.

Two-step flow (mirrors score_vacancies.py prepare → subagents → report):

    # 1. emit audit payloads for a sample of recent <40 roles
    python3 scripts/audit_low_scores.py --sample 20 > /tmp/audit.jobs.json
    # 2. an Opus subagent scores each payload (independent pass) → /tmp/audit.done.json
    # 3. render the report
    python3 scripts/audit_low_scores.py --report < /tmp/audit.done.json

Manual / weekly. NOT wired into the autonomous routine. Never mutates the DB.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LOW_SCORE_THRESHOLD = 40
DEFAULT_SAMPLE = 20

# The auditor verdict each subagent must return per role.
AUDIT_USER_TEMPLATE = """Re-examine this role that our scorer buried with a LOW score ({old_score}/100).

Your job is NOT to re-score from scratch. It is to catch a FALSE NEGATIVE: a
role that genuinely fits the candidate but was wrongly buried. Be skeptical —
most low scores are correct. Only flag when you would clearly have scored it
higher.

**Organization:** {org}
**Job Title:** {title}
**Location:** {location}

**Full Description:**
{description}

Return ONLY JSON:
{{"wrongly_buried": true|false, "suggested_score": <0-100>, "reason": "<1-2 sentences: why this was or wasn't a miss>"}}"""


def _audit_system_prompt() -> str:
    """The candidate fit rubric, framed as a false-negative auditor."""
    from prompts import VACANCY_SCORING_PROMPT

    return (
        "You are an independent QA auditor for a career-fit scorer. Using the "
        "same candidate profile and rubric below, decide whether a role that was "
        "scored LOW was in fact a false negative (a genuine fit wrongly buried). "
        "Default to wrongly_buried=false unless the mismatch is clear.\n\n" + VACANCY_SCORING_PROMPT
    )


def _location_str(row) -> str:
    parts = []
    for loc in row.get("locations") or []:
        bits = [loc.get(k) for k in ("city", "country", "region", "work_mode")]
        bits = [b for b in bits if b]
        if not bits and loc.get("location"):
            bits = [loc["location"]]
        if bits:
            parts.append(", ".join(bits))
    return "; ".join(parts)


def select_low_scored(conn, sample_size):
    """Return (sample_rows, total_count) of recently low-scored, undecided roles.

    Undecided only (status='unseen'): a role the user already judged is not a
    scorer false-negative worth re-auditing. Most-recent first.
    """
    from db_backend import RealDictCursor

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT COUNT(*) AS n FROM vacancy "
        "WHERE llm_score IS NOT NULL AND llm_score < %s AND status = 'unseen'",
        (LOW_SCORE_THRESHOLD,),
    )
    total = cur.fetchone()["n"]
    cur.execute(
        "SELECT v.id, v.title, c.canonical_name AS org, v.llm_score, "
        "       v.llm_reasoning, v.full_description, v.snippet, v.locations "
        "FROM vacancy v JOIN company c ON v.company_id = c.id "
        "WHERE v.llm_score IS NOT NULL AND v.llm_score < %s AND v.status = 'unseen' "
        "ORDER BY v.llm_scored_at DESC NULLS LAST, v.created_at DESC "
        "LIMIT %s",
        (LOW_SCORE_THRESHOLD, sample_size),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows, total


def build_audit_payload(row):
    description = row.get("full_description") or row.get("snippet") or "No description available"
    if len(description) > 8000:
        description = description[:8000] + "\n\n[Description truncated]"
    user_msg = AUDIT_USER_TEMPLATE.format(
        old_score=row.get("llm_score"),
        org=row.get("org", ""),
        title=row.get("title", ""),
        location=_location_str(row) or "",
        description=description,
    )
    return {
        "payload_kind": "audit",
        "id": row["id"],
        "org": row.get("org", ""),
        "title": row.get("title", ""),
        "old_score": row.get("llm_score"),
        "system_prompt": _audit_system_prompt(),
        "user_msg": user_msg,
    }


def cmd_prepare(args):
    from db_conn import get_conn

    rows, total = select_low_scored(get_conn(), args.sample)
    # Honest sampling note → stderr so it doesn't pollute the JSON payload.
    print(
        f"Audit: sampling {len(rows)} of {total} undecided roles scored < {LOW_SCORE_THRESHOLD}.",
        file=sys.stderr,
    )
    if not rows:
        print("[]")
        return
    payloads = [build_audit_payload(r) for r in rows]
    json.dump(payloads, sys.stdout, ensure_ascii=False)


def render_report(verdicts, sampled=None, total=None):
    """Build a markdown report of suspected misses. Pure (testable)."""
    flagged = [v for v in verdicts if v.get("wrongly_buried")]
    flagged.sort(key=lambda v: v.get("suggested_score", 0), reverse=True)
    lines = ["# Low-score audit — suspected false negatives", ""]
    if sampled is not None and total is not None:
        lines.append(f"Sampled **{sampled}** of **{total}** roles scored < {LOW_SCORE_THRESHOLD}.")
    else:
        lines.append(f"Reviewed **{len(verdicts)}** roles.")
    lines.append(f"Flagged as wrongly buried: **{len(flagged)}**.")
    lines.append("")
    if not flagged:
        lines.append("No suspected misses — the low scores look correct.")
        return "\n".join(lines)
    for v in flagged:
        org = v.get("org", "")
        title = v.get("title", "")
        old = v.get("old_score", "?")
        sug = v.get("suggested_score", "?")
        reason = v.get("reason", "")
        lines.append(f"- **{org} — {title}** — was {old}, suggested {sug}")
        if reason:
            lines.append(f"  - {reason}")
    return "\n".join(lines)


def cmd_report(args):
    data = json.load(sys.stdin)
    verdicts = data if isinstance(data, list) else data.get("verdicts", [])
    print(render_report(verdicts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        type=int,
        default=DEFAULT_SAMPLE,
        help=f"how many recent <{LOW_SCORE_THRESHOLD} roles to audit (default {DEFAULT_SAMPLE})",
    )
    parser.add_argument(
        "--report", action="store_true", help="read scored audit verdicts from stdin, print report"
    )
    args = parser.parse_args()
    if args.report:
        cmd_report(args)
    else:
        cmd_prepare(args)


if __name__ == "__main__":
    main()
