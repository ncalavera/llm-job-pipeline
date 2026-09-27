"""Screener junk task: the profile card, the Jev questions, the combining rule
and the overrides for the junk filter in front of the scorer (plan
2026-09-27-1000-feat-jev-junk-filter, U3; R4-R9, KTD4, KTD6, KTD7).

catalog-checked: wiki tools catalog searched for "junk", "jev", "filter" - no row; this is the
screener's task definition for the self-made nikita_tools.junk_filter tool (KTD1), not a tool itself.

Jev answers facts; code decides junk. Every question asks one literal fact.
``decide`` does all arithmetic: the deadline, the pay and years buckets, the
posting length, the programme rule, the AI-safety-research exception, the cut.

Skip only, never keep: a missing answer counts as "no", and a role is skipped
only when one branch answer is at or above ``CUT`` (or the deadline passed).
A branch with several conditions takes the minimum of them, so every
condition must reach the cut on its own.

The tool itself (``nikita_tools.junk_filter``) is private and installed only on
forge; it is imported only inside ``check`` when no engine is passed, so this
module and its tests run in public CI.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from statuses import APPLICATION_STATUSES

#: Bump by hand when the decision logic changes; a profile edit changes the hash itself (KTD4).
SCREENER_TASK_VERSION = "1"
#: The judge brief the profile card is copied from (jobsearch-evals
#: prompts/judge/brief_judge_v5-cfdc310c.md).
BRIEF_VERSION = "brief_judge_v5:cfdc310c"

CUT = 0.95
#: scoring_excluded_reason prefix of a live skip; the rule filter keeps these (KTD3).
REASON_PREFIX = "junk_filter:"
POSTING_CAP = 6000  # judge set v1 cap
MIN_POSTING = 400  # the judge's "posting text missing" line (AE5)
FAR_BELOW_SHARE = 0.5  # far below = the whole bucket sits at or under half the target
SENIOR_YEARS = 12  # stated minimum above this = far too senior
EXCEPTION_P = 0.5  # the R7 exception applies from here: a wrong skip costs more

#: The profile card is personal, so it lives in a gitignored file, like
#: config/user_profile.md: {"pay_target_eur_month": int, "card": {...}}.
#: See config/junk_profile.example.json. JUNK_PROFILE_PATH overrides the path.
DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "junk_profile.json"


def load_profile() -> dict | None:
    """The profile file, or None when it is missing (the stage then fails open)."""
    path = Path(os.environ.get("JUNK_PROFILE_PATH") or DEFAULT_PROFILE_PATH)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _profile() -> dict:
    profile = load_profile()
    if profile is None:
        raise RuntimeError("no junk profile: config/junk_profile.json is missing")
    return profile

#: Upper edge in EUR per month for each stated-pay bucket (None = open or unknown).
PAY_BUCKETS = {"under_2000": 2000, "2000_3499": 3500, "3500_5499": 5500, "5500_plus": None, "not_stated": None}
#: Lower edge in years for each required-minimum bucket.
YEARS_BUCKETS = {"0_2": 0, "3_5": 3, "6_9": 6, "10_12": 10, "13_plus": 13, "not_stated": None}
EXCLUDED_FUNCTIONS = ("engineering", "lab_research", "sales", "hr", "assistant", "law")


def _noul(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


QUESTIONS = {
    # Step 0
    "type": {
        "type": "choice",
        "instructions": "What does the posting in role.posting offer?",
        "criteria": {
            "job": "A paid or unpaid job, contract or volunteer position with an employer.",
            "programme": "A fellowship, course, accelerator, incubator, grant or training programme.",
            "not_opportunity": "Not an opportunity: a news item, a company page, a list of roles, or an empty page.",
        },
    },
    # 1. May he apply
    "right_to_work": _noul(
        "Does the posting require the candidate to already hold citizenship, residency or the right to "
        "work in a specific country, with no visa sponsorship offered?",
        "The posting states such a requirement, e.g. 'nationals only', 'must have the right to work in the "
        "UK', 'we do not sponsor visas', 'must be a US resident'.",
        "No such requirement is stated, or sponsorship is offered.",
    ),
    "place": _noul(
        "Can the role only be done from a place that is not in profile.target_places?",
        "Onsite or hybrid only in a place outside profile.target_places with no remote option, or remote "
        "limited to a region outside them (e.g. Americas only, US time zones only).",
        "The role can be done in one of profile.target_places, or remotely from anywhere, or the posting "
        "does not say. Also no when the employer is an international organisation with offices or "
        "programmes in several countries.",
    ),
    # 2. Does he qualify
    "credential": _noul(
        "Does the posting require (not prefer) a degree, licence or security clearance that the candidate "
        "in profile does not have?",
        "A required PhD, medical, clinical, engineering or law degree, a professional licence, or a "
        "security clearance.",
        "No such requirement, or it is only preferred, or the candidate has it.",
    ),
    "language": _noul(
        "Does the posting require (not prefer) a language that is not in profile.languages?",
        "A required language outside profile.languages, e.g. 'fluent French required'.",
        "Every required language is in profile.languages, or other languages are only a plus.",
    ),
    "domain_experience": _noul(
        "Does the posting require (not prefer) experience in a domain or region that the candidate in "
        "profile does not have?",
        "A required domain or region experience he lacks, e.g. '10 years in clinical trials', "
        "'experience working in Africa required'.",
        "No such requirement, or it is only preferred, or it lists duties rather than requirements.",
    ),
    "ai_safety_research": _noul(
        "Is a background in AI safety research one of the required experiences the posting asks for?",
        "The posting requires experience or a background in AI safety or AI alignment research.",
        "AI safety research experience is not required.",
    ),
    # 3. His kind of work
    "function": {
        "type": "choice",
        "instructions": "What is the main function of the role?",
        "criteria": {
            "engineering": "Software, data, ML or hardware engineering as the main duty.",
            "lab_research": "Research scientist or lab or technical research as the main duty.",
            "sales": "Sales with a quota, business development for revenue.",
            "hr": "HR or people function as the main duty: recruiter, talent acquisition, HR partner.",
            "assistant": "Executive or personal assistant: calendar, inbox and travel for a leader.",
            "law": "Practising law or managing legal proceedings.",
            "other": "Anything else: operations, programmes, grants, policy, communications, management.",
        },
    },
    "intern": _noul(
        "Is the position an internship?",
        "The posting calls the position an internship or intern role.",
        "It is not an internship.",
    ),
    "unpaid": _noul(
        "Is the position unpaid or volunteer?",
        "The posting says unpaid, volunteer, or expenses only.",
        "The position is paid, or pay is not mentioned.",
    ),
    "pay": {
        "type": "choice",
        "instructions": "What gross pay per month does the posting state, converted to euros? "
        "Divide a yearly figure by 12. Use the lower end of a range.",
        "criteria": {
            "under_2000": "Under EUR 2,000 per month.",
            "2000_3499": "EUR 2,000 to 3,499 per month.",
            "3500_5499": "EUR 3,500 to 5,499 per month.",
            "5500_plus": "EUR 5,500 per month or more.",
            "not_stated": "The posting states no pay figure.",
        },
    },
    "years": {
        "type": "choice",
        "instructions": "What minimum years of experience does the posting require (not prefer)?",
        "criteria": {
            "0_2": "0 to 2 years.",
            "3_5": "3 to 5 years.",
            "6_9": "6 to 9 years.",
            "10_12": "10 to 12 years.",
            "13_plus": "13 years or more.",
            "not_stated": "No minimum number of years is required.",
        },
    },
    # 4. His kind of employer
    "for_profit_no_mission": _noul(
        "Is the employer a for-profit company that states no social, safety or other impact mission?",
        "A commercial company (property, payments, marketing, insurance, recruiting agency, generic tech "
        "or AI startup) with no impact mission stated in its own words.",
        "A non-profit, foundation, public body, social enterprise, AI-safety lab, or a company that states "
        "an impact mission; or the employer's purpose is unclear.",
    ),
    "excluded_cause": _noul(
        "Does the employer work in animal farming or animal advocacy, and is the role something other than "
        "facilitation or teaching?",
        "Animal farming or animal advocacy is the employer's cause area, and the role is not facilitation "
        "or teaching.",
        "Another cause area, or a facilitation or teaching role.",
    ),
    "english": _noul(
        "Is the posting written mainly in English?",
        "Most of the posting text is in English.",
        "Most of the posting text is in another language.",
    ),
}


def task_version(engine: str) -> str:
    """Hash of questions, cut, engine, the profile and the version strings (KTD4)."""
    blob = json.dumps([QUESTIONS, CUT, engine, SCREENER_TASK_VERSION, BRIEF_VERSION, load_profile()],
                      sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def build_state(role: dict) -> dict:
    """Jev state from a ``judge_roles.role_payload`` dict (R4)."""
    return {
        "profile": _profile()["card"],
        "role": {
            "title": role.get("title"),
            "org": role.get("org"),
            "locations": role.get("locations"),
            "posting": (role.get("posting") or "")[:POSTING_CAP],
        },
    }


def _precheck(role: dict):
    """Decisions that need no model: overrides (R9), expired deadline and short posting (R6)."""
    if role.get("override"):
        return False, f"override:{role['override']}", None
    deadline = role.get("deadline")
    if deadline:
        if not isinstance(deadline, date):
            deadline = date.fromisoformat(str(deadline)[:10])
        if deadline < datetime.now(timezone.utc).date():
            return True, "expired", None
    if len(role.get("posting") or "") < MIN_POSTING:
        return False, "short_posting", None
    return None


def _p(answers: dict, qid: str, options=None) -> float:
    a = answers.get(qid) or {}
    if options is None:
        return float(a.get("noul") or 0.0)
    probs = a.get("probabilities") or {}
    return sum(float(probs.get(o) or 0.0) for o in options)


def decide(answers: dict, role: dict):
    """(skip, question, p). Works for Jev answers and for the baseline's {"p_junk", "cut"}."""
    pre = _precheck(role)
    if pre:
        return pre
    if "p_junk" in answers:
        p = float(answers["p_junk"])
        return (p >= answers.get("cut", CUT), "baseline", p)
    if _p(answers, "english") < 0.5:
        return False, "non_english", None

    p_job = _p(answers, "type", ["job"])  # level checks only for jobs (AE3)
    pay_target = _profile()["pay_target_eur_month"]
    far_below = [b for b, top in PAY_BUCKETS.items() if top and top <= pay_target * FAR_BELOW_SHARE]
    senior = [b for b, low in YEARS_BUCKETS.items() if low is not None and low > SENIOR_YEARS]
    no_exception = 1.0 if _p(answers, "ai_safety_research") < EXCEPTION_P else 0.0  # R7
    branches = {
        "type": _p(answers, "type", ["not_opportunity"]),
        "right_to_work": _p(answers, "right_to_work"),
        "place": _p(answers, "place"),
        "credential": _p(answers, "credential"),
        "language": _p(answers, "language"),
        "domain_experience": min(_p(answers, "domain_experience"), no_exception),
        "function": _p(answers, "function", EXCLUDED_FUNCTIONS),
        "intern": min(p_job, _p(answers, "intern")),
        "unpaid": min(p_job, _p(answers, "unpaid")),
        "pay": min(p_job, _p(answers, "pay", far_below)),
        "years": min(p_job, _p(answers, "years", senior)),
        "for_profit_no_mission": _p(answers, "for_profit_no_mission"),
        "excluded_cause": _p(answers, "excluded_cause"),
    }
    question, p = max(branches.items(), key=lambda kv: kv[1])
    return (p >= CUT, question, p)


def check(role: dict, engine=None) -> dict:
    """One role through the task. No model call when the code checks decide alone.
    Engine errors propagate; the caller fails open (sends the role to scoring)."""
    pre = _precheck(role)
    if pre:
        skip, question, p = pre
        return {"skip": skip, "question": question, "p": p, "answers": None, "usage": None}
    if engine is None:
        from nikita_tools.junk_filter import ask_jev as engine
    result = engine(build_state(role), QUESTIONS)
    answers, usage = result if isinstance(result, tuple) else (result, None)
    skip, question, p = decide(answers, role)
    return {"skip": skip, "question": question, "p": p, "answers": answers, "usage": usage}


def overrides(conn, company_ids) -> dict:
    """{company_id: reason} for companies whose roles always go to scoring (R9, KTD6):
    a role Nikita liked or applied to, an application row, or a north-star role."""
    ids = [str(c) for c in company_ids]
    if not ids:
        return {}
    cur = conn.cursor()
    cur.execute(
        "SELECT v.company_id, 'status:' || v.status FROM vacancy v "
        "WHERE v.company_id = ANY(%s::uuid[]) AND v.status = ANY(%s) "
        "UNION ALL SELECT a.company_id, 'application' FROM application a "
        "WHERE a.company_id = ANY(%s::uuid[]) "
        "UNION ALL SELECT v.company_id, 'north_star' FROM vacancy v "
        "WHERE v.company_id = ANY(%s::uuid[]) "
        "AND CAST(v.screening->>'north_star' AS TEXT) IN ('true', '1')",
        (ids, sorted(APPLICATION_STATUSES | {"liked"}), ids, ids),
    )
    out = {}
    for cid, reason in cur.fetchall():
        out.setdefault(str(cid), reason)
    cur.close()
    return out
