#!/usr/bin/env python3
"""One driver for the daily ``/jobs-new`` cycle — deterministic orchestration.

STRATEGY guardrail 5 ("scriptable core, agent on top"): the ORDER of stages,
the batching, the retries, the heartbeat and the publish gate are deterministic
Python and live here. The agent contributes only JUDGMENT — scoring vacancies,
WANT-scoring companies, and capturing verdicts. It can never reorder the stages
or publish a bad run, because it does not own that logic; this driver does.

How it runs
-----------
The driver is a resumable stage machine. It runs a contiguous block of
AUTO stages (fetch, enrich, filter, publish) back-to-back, writing its state to
``vacancies/run_state.json`` after every step. When it reaches a GATE — a point
that needs the agent's judgment — it prepares a machine-readable task on disk,
prints plain-language instructions, and EXITS. The agent does the judgment with
the existing scripts + subagents, then re-runs the driver with ``--resume`` to
continue. Gates are idempotent: a weak executor that forgets to save is simply
re-prompted for exactly what is still missing; the order can never be violated.

Exit codes (the runbook branches on these)::

    0   DONE   — pipeline complete (see the final summary)
    10  GATE   — stopped for the agent; do the printed task, then --resume
    20  ABORT  — unrecoverable (bad profile, DB outage); fix, then start fresh
    30  ERROR  — a stage crashed; inspect the output, then --resume (or --new)

The long, silent stages (fetch, enrich) write ``vacancies/run_status.json``
themselves; launch the driver in the background and poll ``run_card.py`` to show
a live progress card, exactly as before — but now with ONE entrypoint.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# When run as ``python3 scripts/run_daily.py`` the interpreter puts scripts/ on
# sys.path[0]; add it explicitly too so the module also imports cleanly in tests.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PROJECT_ROOT = SCRIPTS_DIR.parent
STATE_PATH = PROJECT_ROOT / "vacancies" / "run_state.json"
FETCH_STATS_PATH = PROJECT_ROOT / "vacancies" / "fetch_stats.json"
VAC_PAYLOAD_PATH = PROJECT_ROOT / "vacancies" / "score_vacancies_payload.json"
CO_PAYLOAD_PATH = PROJECT_ROOT / "vacancies" / "score_companies_payload.json"

EXIT_DONE = 0
EXIT_GATE = 10
EXIT_ABORT = 20
EXIT_ERROR = 30

# Publish gate: block a publish when a single org lost a large share of its live
# roles to gone-from-source archival this run (the signature of a truncated
# HTTP-200 fetch). A floor keeps a 1-of-1 tiny org from tripping the gate.
GONE_ARCHIVE_BLOCK_SHARE = 0.30
GONE_ARCHIVE_MIN_COUNT = 3

# The canonical stage order. This list — not the runbook, not the maintainer's
# memory — is the single source of truth for what happens when.
#
# Two positions are reserved insertion points for follow-up work and currently
# no-op (they only print a one-line note):
#   * learning_review  — a future verdict-driven feedback loop that offers
#                        filter/scoring corrections at the START of a run
#                        (skippable, rolls over — STRATEGY guardrail 8).
#   * company_scoring  — a future step will extend this gate to collect +
#                        auto-enrich new companies, WANT-score them, and show the
#                        best role as evidence at an in-chat approval gate. Today
#                        the gate just WANT-scores candidates; they land in
#                        Pending for review.
STAGE_ORDER = [
    "validate_profile",  # AUTO  — abort early on a missing/placeholder profile
    "preflight",  # AUTO  — DB-outage hard-stop, first-run + resume detect
    "onboarding",  # GATE  — only when the company table is empty
    "learning_review",  # noop  — reserved insertion point (skipped)
    "fetch",  # AUTO  — pull new vacancies (heartbeat inside the script)
    "enrich",  # AUTO  — backfill blind descriptions (Firecrawl)
    "filter",  # AUTO  — quality report; never auto-deletes
    "company_scoring",  # GATE  — WANT-score new candidate companies
    "vacancy_scoring",  # GATE  — per-vacancy subagent scoring (1 vac = 1 agent)
    "verdicts",  # GATE  — show top matches, capture like/pass
    "publish",  # AUTO  — publish, gated on a clean run
]


@dataclass
class Opts:
    """Run options — persisted in the state so ``--resume`` keeps them."""

    job_boards: str | None = None
    full_rescore: bool = False
    no_publish: bool = False
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# State persistence (vacancies/ is gitignored — pure runtime state)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_state(opts: Opts) -> dict:
    return {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "created_at": _now(),
        "updated_at": _now(),
        "finished": False,
        "first_run": None,
        "cursor": 0,
        "gate": None,
        "options": {
            "job_boards": opts.job_boards,
            "full_rescore": opts.full_rescore,
            "no_publish": opts.no_publish,
        },
        "stages": [{"name": n, "status": "pending"} for n in STAGE_ORDER],
    }


def _save_state(state: dict) -> None:
    state["updated_at"] = _now()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _load_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _stage(state: dict, name: str) -> dict:
    for s in state["stages"]:
        if s["name"] == name:
            return s
    raise KeyError(name)


def _opts_from_state(state: dict) -> Opts:
    o = state.get("options", {}) or {}
    return Opts(
        job_boards=o.get("job_boards"),
        full_rescore=bool(o.get("full_rescore")),
        no_publish=bool(o.get("no_publish")),
    )


def _warn_ignored_resume_flags(args: argparse.Namespace) -> None:
    """Resuming replays the options frozen in the checkpoint, not the CLI's.

    ``--boards`` and ``--full-rescore`` are silently dropped on resume unless we
    say so — a user re-running with ``--full-rescore`` to lift the scoring cap
    would otherwise get a normal-cap run with no indication anything changed.
    """
    flags = []
    if args.boards is not None:
        flags.append("--boards")
    if args.full_rescore:
        flags.append("--full-rescore")
    for flag in flags:
        print(
            f"⚠  {flag} is IGNORED on resume: this run's options are locked in "
            "at the checkpoint, not re-read from the CLI. To apply it, finish "
            "or discard the current run (--new) and start over.",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Small DB helpers (backend-agnostic — plain ANSI SQL, no ::casts)
# ---------------------------------------------------------------------------


def _close_db() -> None:
    """Drop the driver's connection before spawning a subprocess.

    The child opens its own connection to the same SQLite file; holding an open
    transaction here could block its writes."""
    try:
        from db_conn import close_conn

        close_conn()
    except Exception:
        pass


def _scalar(sql: str, params: tuple = ()) -> int:
    from db_conn import get_conn

    cur = get_conn().cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    cur.close()
    return int(row[0]) if row and row[0] is not None else 0


def _registry_load_failed() -> bool:
    try:
        from company_registry import registry_load_failed

        return bool(registry_load_failed())
    except Exception:
        return False


def _company_count() -> int:
    return _scalar("SELECT count(*) FROM company")


def _candidates_to_score() -> int:
    return _scalar(
        "SELECT count(*) FROM company "
        "WHERE status = 'candidate' AND alignment_score IS NULL "
        "AND website IS NOT NULL AND website != ''"
    )


def _unscored_unseen() -> int:
    return _scalar("SELECT count(*) FROM vacancy WHERE status = 'unseen' AND llm_score IS NULL")


def _scored_unseen() -> int:
    return _scalar("SELECT count(*) FROM vacancy WHERE status = 'unseen' AND llm_score IS NOT NULL")


def _unscored_company_ids(target_ids: list[str]) -> set[str]:
    """Of ``target_ids``, which companies still lack a WANT score.

    A target that vanished (deleted) is treated as resolved, so a mid-run change
    can never wedge the gate open."""
    from db_conn import get_conn

    cur = get_conn().cursor()
    cur.execute("SELECT id, alignment_score FROM company")
    have = {str(r[0]): r[1] for r in cur.fetchall()}
    cur.close()
    return {cid for cid in target_ids if have.get(cid) is None and cid in have}


def _unscored_vacancy_ids(target_ids: list[str]) -> set[str]:
    """Of ``target_ids``, which vacancies still lack an ``llm_score``."""
    from database_supabase import load_vacancies

    vacs = load_vacancies(include_candidate_companies=True, include_inactive_companies=True)
    return {vid for vid in target_ids if vid in vacs and vacs[vid].get("llm_score") is None}


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _child_env(opts: Opts) -> dict:
    env = dict(os.environ)
    if opts.job_boards is not None:
        env["JOB_BOARDS"] = opts.job_boards
    return env


def _run(cmd: list[str], opts: Opts) -> int:
    """Run a stage subprocess, streaming its output (live card stays visible)."""
    _close_db()
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=_child_env(opts)).returncode


def _run_capture(cmd: list[str], opts: Opts) -> subprocess.CompletedProcess:
    """Run a stage subprocess and capture stdout (JSON contract) + stderr."""
    _close_db()
    return subprocess.run(
        cmd, cwd=str(PROJECT_ROOT), env=_child_env(opts), capture_output=True, text=True
    )


def _py(script: str) -> list[str]:
    return [sys.executable, str(SCRIPTS_DIR / script)]


# ---------------------------------------------------------------------------
# Pure decision helpers (unit-tested directly)
# ---------------------------------------------------------------------------


def resolve_scoring_limit(full_rescore: bool) -> tuple[int | None, str | None]:
    """The per-run scoring cap decision (STRATEGY guardrails 3 & 4).

    Default: return ``None`` so ``score_vacancies.py --local`` applies the
    profile's ``max_per_run`` fuse itself — the cap protects a spike day AND a
    hypothetical full re-score alike. A full re-score is a deliberate opt-in
    (``--full-rescore``) that LIFTS the fuse to a high ceiling and prints a loud
    warning, so the cost is never silent.
    """
    if not full_rescore:
        return None, None
    from scoring_settings import max_per_run

    cap = max_per_run()
    ceiling = max(cap * 10, 1000)
    warn = (
        "⚠  FULL RE-SCORE: the per-run cap "
        f"({cap}) is LIFTED to {ceiling} for this run. This re-scores far more "
        "vacancies than a normal day and will use markedly more of your plan. "
        "This is the explicit opt-in; a normal run keeps the cap."
    )
    return ceiling, warn


def check_publish_gate(state: dict, fetch_stats: dict | None = None) -> tuple[bool, list[str]]:
    """May the driver publish? Publish ONLY on a clean run (STRATEGY guardrail 4).

    Clean = (1) no pipeline STAGE crashed this run, AND (2) no single org lost a
    large share of its live roles to gone-from-source archival (a truncated fetch
    would otherwise push a corrupt snapshot live). Returns ``(allowed, reasons)``;
    ``reasons`` is empty when allowed.
    """
    reasons: list[str] = []

    errored = [s["name"] for s in state.get("stages", []) if s.get("status") == "error"]
    if errored:
        reasons.append("stage error(s): " + ", ".join(errored))

    stats = fetch_stats if fetch_stats is not None else _read_fetch_stats()
    for org, d in (stats.get("orgs", {}) or {}).items():
        gone = int(d.get("gone", 0) or 0)
        live = int(d.get("live", 0) or 0)
        denom = gone + live
        share = (gone / denom) if denom else 0.0
        if gone >= GONE_ARCHIVE_MIN_COUNT and share >= GONE_ARCHIVE_BLOCK_SHARE:
            reasons.append(f"{org}: {gone}/{denom} live roles archived as gone ({share:.0%})")

    return (not reasons), reasons


def _read_fetch_stats() -> dict:
    try:
        return json.loads(FETCH_STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _extract_json(text: str):
    """Parse the JSON value a stage emits on stdout, tolerating banner/trailer.

    filter_vacancies.py wraps its JSON with a leading backend banner and a
    trailing validation line on stdout; the --local scorers keep stdout pure.
    ``raw_decode`` from the first ``{``/``[`` parses exactly one value and
    ignores anything after it, so both shapes work without the caller caring."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = dec.raw_decode(text, i)
                return obj
            except Exception:
                continue
    raise ValueError("no JSON value found in stage output")


# ---------------------------------------------------------------------------
# Gate payload persistence + printing
# ---------------------------------------------------------------------------


def _write_payload(path: Path, payloads: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_payload(path: Path) -> list:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _emit_gate(stage: str, payload: dict) -> None:
    bar = "=" * 70
    print(f"\n{bar}")
    print(f"  ⏸  GATE: {stage} — your judgment is needed")
    print(bar)
    print(payload["instructions"].strip())
    if payload.get("payload_path"):
        print(f"\n  Machine-readable task: {payload['payload_path']}")
    print("\n  When the task above is done, continue the run:")
    print("      python3 scripts/run_daily.py --resume")
    print(bar, flush=True)


# ---------------------------------------------------------------------------
# Instruction text (written for a weak executor — explicit, one path)
# ---------------------------------------------------------------------------

ONBOARDING_TEXT = """
Your company table is empty — this looks like a fresh clone. Onboard starter
companies before the pipeline can fetch anything:

  1. Read config/user_profile.md for the field/roles and geography.
  2. Web-search 10-15 real, mission-aligned employers that match and are hiring.
  3. For each, guess an ATS slug and validate by probing the public ATS APIs with
     curl (Greenhouse / Lever / Ashby / Workable). Keep the ones that return jobs.
  4. Show the shortlist (name, careers URL, detected ATS + slug) and WAIT for a
     yes before inserting anything.
  5. On approval, run scripts/migrate.py, then insert each approved company as
     'active' (see the onboarding recipe in .claude/commands/jobs-new.md).

Then --resume: the driver re-checks the company table and continues.
""".strip()

VERDICT_TEXT = """
Show the user the freshly scored, still-unseen vacancies (highest score first)
and capture a like / pass / skip verdict for each — writing EACH verdict
immediately so an interruption keeps captured decisions:

  python3 -c "import sys;sys.path.insert(0,'scripts');\\
from database_supabase import update_vacancy_status; from db_conn import get_conn;\\
update_vacancy_status('<VACANCY_ID>','liked'); get_conn().commit()"

Statuses: liked | passed | skipped | to_apply. This is the quick daily pass; the
deep structured review of liked roles lives in /jobs-review.

Then --resume to publish.
""".strip()


def _vacancy_gate_text(payloads: list, opts: Opts) -> str:
    from scoring_settings import scoring_model

    model = scoring_model()
    return (
        f"Score {len(payloads)} vacancy(ies) — ONE subagent per vacancy, model "
        f'"{model}" (from your profile\'s [## VOLUME] scoring_model).\n\n'
        f"Each entry in the payload file carries its own system_prompt + user_msg\n"
        f"and the real DB member_ids. CRITICAL: 1 vacancy = 1 subagent (batching\n"
        f"over-scores by +20-50). Run at most 5 subagents at a time (rolling waves).\n\n"
        f"Save results incrementally (each --save commits, so an interrupt keeps\n"
        f"finished work):\n"
        f"  python3 scripts/score_vacancies.py --save < chunk.json\n\n"
        f"Flat per-vacancy fields to save: member_ids, org, title, score, reasoning,\n"
        f"tags, hard_requirements, short_summary. Then --resume — the driver\n"
        f"re-checks and re-prompts only for any vacancy still unscored."
    )


def _company_gate_text(payloads: list) -> str:
    return (
        f"WANT-score {len(payloads)} new candidate company(ies) — ONE subagent per\n"
        f'company, model "sonnet". Each payload entry carries its own system_prompt\n'
        f"+ user_msg. 1 company = 1 subagent; at most 5 at a time (rolling waves).\n\n"
        f"Wrap each result under 'enrichment' and save incrementally:\n"
        f"  python3 scripts/score_companies.py --save < chunk.json\n\n"
        f"Scored companies land in Companies -> Pending for approval (deeper review\n"
        f"in /jobs-review companies --status candidate). Then --resume.\n\n"
        f"[reserved insertion point] Follow-up work will collect + auto-enrich these\n"
        f"companies earlier in the run and show each one's best role as evidence at\n"
        f"an in-chat approve/reject gate; that logic slots in around this stage."
    )


# ---------------------------------------------------------------------------
# Stage handlers. Each returns one of:
#   ("advance", note) | ("skip", note) | ("gate", payload)
#   ("error", msg)    | ("abort", msg)
# ---------------------------------------------------------------------------


def _h_validate_profile(state, entry, opts):
    from prompts import DEFAULT_PROFILE_PATH, EXAMPLE_PROFILE_PATH

    env_path = os.environ.get("USER_PROFILE_PATH")
    path = Path(env_path).expanduser() if env_path else DEFAULT_PROFILE_PATH

    if not path.exists():
        return (
            "abort",
            "config/user_profile.md is missing. Fill it in (field/role, target "
            "locations, seniority, visa) or run /jobs-profile, then re-run.",
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return "abort", "config/user_profile.md is empty. Fill it in, then re-run."
    try:
        example = EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        example = None
    if example is not None and (
        text == example or path.resolve() == EXAMPLE_PROFILE_PATH.resolve()
    ):
        return (
            "abort",
            "config/user_profile.md still equals the bundled EXAMPLE (a fictional "
            "person). Personalise it, then re-run — scores against the example are "
            "meaningless.",
        )
    return "advance", "profile OK"


def _h_preflight(state, entry, opts):
    if _registry_load_failed():
        return (
            "abort",
            "database unreachable, not a fresh clone — the empty company table is "
            "an outage artifact. Fix the DB and retry; do NOT onboard.",
        )
    n = _company_count()
    if n == 0:
        state["first_run"] = True
        return "advance", "empty company table — first-run onboarding required"
    state["first_run"] = False
    pending = _unscored_unseen()
    note = f"{n} companies tracked"
    if pending:
        note += f"; {pending} unscored vacancies from a prior run will be picked up in scoring"
    return "advance", note


def _h_onboarding(state, entry, opts):
    if not state.get("first_run"):
        return "skip", "companies already present — onboarding not needed"
    if _company_count() > 0:
        return "advance", "starter companies added"
    return "gate", {"action": "onboard", "instructions": ONBOARDING_TEXT, "payload_path": None}


def _h_learning_review(state, entry, opts):
    # Reserved insertion point. When implemented, this stage offers verdict-driven
    # filter/scoring corrections at the START of a run (skippable; skipped
    # verdicts roll over — STRATEGY guardrail 8).
    return (
        "skip",
        "learning review skipped — verdict-driven corrections not yet implemented; "
        "insertion point reserved",
    )


def _h_fetch(state, entry, opts):
    # -u (unbuffered) is a python flag, not a script flag — so the live card sees
    # the heartbeat immediately while fetch runs in the background.
    cmd = [sys.executable, "-u", str(SCRIPTS_DIR / "fetch_vacancies.py"), "--no-dashboard"]
    if state.get("first_run"):
        cmd.append("--no-boards")  # keep the first run fast
    rc = _run(cmd, opts)
    if rc != 0:
        return "error", f"fetch exited with code {rc}"
    stats = _read_fetch_stats()
    new = stats.get("total_new", "?")
    errs = len(stats.get("errors", {}) or {})
    note = f"{new} new vacancies"
    if errs:
        note += f"; {errs} source(s) had a fetch error (does not block the other sources)"
    return "advance", note


def _h_enrich(state, entry, opts):
    if not os.environ.get("FIRECRAWL_API_KEY"):
        return (
            "skip",
            "enrich skipped — FIRECRAWL_API_KEY unset (scoring tolerates blind rows; accuracy drops)",
        )
    rc = _run([sys.executable, "-u", str(SCRIPTS_DIR / "enrich_blind_vacancies.py")], opts)
    if rc != 0:
        return "error", f"enrich exited with code {rc}"
    return "advance", "blind vacancies enriched"


def _h_filter(state, entry, opts):
    res = _run_capture(_py("filter_vacancies.py"), opts)
    if res.returncode != 0:
        return "error", f"filter exited with code {res.returncode}: {res.stderr[-400:]}"
    try:
        data = _extract_json(res.stdout)
    except Exception:
        return "error", "filter did not emit valid JSON"
    ready = data.get("ready", 0)
    delete_ids = data.get("delete_ids", {}) or {}
    junk = sum(len(v) for v in delete_ids.values())
    entry["filter"] = {"ready": ready, "junk_flagged": junk}
    note = f"{ready} ready to score"
    if junk:
        note += f"; {junk} junk candidate(s) flagged (NOT deleted — review in /jobs-review)"
    return "advance", note


def _h_company_scoring(state, entry, opts):
    # Idempotent resume: only re-prompt for companies still missing a score.
    if entry.get("emitted"):
        remaining = _unscored_company_ids(entry.get("target_ids", []))
        if not remaining:
            return (
                "advance",
                f"company scoring complete ({len(entry.get('target_ids', []))} scored)",
            )
        payloads = [p for p in _read_payload(CO_PAYLOAD_PATH) if str(p.get("id")) in remaining]
        return "gate", {
            "action": "score_companies",
            "count": len(payloads),
            "payload_path": str(CO_PAYLOAD_PATH),
            "instructions": _company_gate_text(payloads),
        }

    n = _candidates_to_score()
    if n == 0:
        return "advance", "no new candidate companies to score"
    if not os.environ.get("FIRECRAWL_API_KEY"):
        return (
            "skip",
            f"{n} candidate companies found but FIRECRAWL_API_KEY unset — left "
            "unscored (approve manually later via /jobs-review)",
        )
    # Deterministic prep: scrape about-pages, then build scoring payloads.
    rc = _run(_py("fetch_companies.py") + ["--limit", str(n)], opts)
    if rc != 0:
        return "error", f"company scrape exited with code {rc}"
    res = _run_capture(_py("score_companies.py") + ["--local", "--limit", str(n)], opts)
    if res.returncode != 0:
        return "error", f"score_companies --local exited {res.returncode}: {res.stderr[-400:]}"
    try:
        payloads = _extract_json(res.stdout or "[]")
    except Exception:
        return "error", "score_companies --local did not emit valid JSON"
    if not payloads:
        return "advance", "candidate companies had no scrape cache — nothing to score"
    _write_payload(CO_PAYLOAD_PATH, payloads)
    entry["target_ids"] = [str(p["id"]) for p in payloads]
    return "gate", {
        "action": "score_companies",
        "count": len(payloads),
        "payload_path": str(CO_PAYLOAD_PATH),
        "instructions": _company_gate_text(payloads),
    }


def _h_vacancy_scoring(state, entry, opts):
    import run_status

    if entry.get("emitted"):
        if entry.get("oneshot"):
            return (
                "advance",
                "full re-score batch handed off (oneshot; re-run --full-rescore if incomplete)",
            )
        payloads = _read_payload(VAC_PAYLOAD_PATH)
        remaining_ids = _unscored_vacancy_ids(entry.get("target_ids", []))
        remaining = [
            p for p in payloads if any(str(m) in remaining_ids for m in p.get("member_ids", []))
        ]
        if not remaining:
            run_status.finish()
            return (
                "advance",
                f"vacancy scoring complete ({len(entry.get('target_ids', []))} scored)",
            )
        return "gate", {
            "action": "score_vacancies",
            "count": len(remaining),
            "payload_path": str(VAC_PAYLOAD_PATH),
            "instructions": _vacancy_gate_text(remaining, opts),
        }

    limit, warn = resolve_scoring_limit(opts.full_rescore)
    if warn:
        print(warn, flush=True)
    cmd = _py("score_vacancies.py") + ["--local"]
    if opts.full_rescore:
        cmd.append("--force")
    if limit is not None:
        cmd += ["--limit", str(limit)]
    res = _run_capture(cmd, opts)
    if res.returncode != 0:
        return "error", f"score_vacancies --local exited {res.returncode}: {res.stderr[-400:]}"
    # --local prints its honest "Scoring X of Y … cap reached" lines to stderr.
    if res.stderr.strip():
        print(res.stderr.strip(), file=sys.stderr, flush=True)
    try:
        payloads = _extract_json(res.stdout or "[]")
    except Exception:
        return "error", "score_vacancies --local did not emit valid JSON"
    if not payloads:
        return "advance", "no unscored vacancies to score"
    _write_payload(VAC_PAYLOAD_PATH, payloads)
    entry["target_ids"] = [str(m) for p in payloads for m in p.get("member_ids", [])]
    if opts.full_rescore:
        entry["oneshot"] = True  # force-rescore can't use the "llm_score IS NULL" check
    run_status.begin("score", len(payloads))
    return "gate", {
        "action": "score_vacancies",
        "count": len(payloads),
        "payload_path": str(VAC_PAYLOAD_PATH),
        "instructions": _vacancy_gate_text(payloads, opts),
    }


def _h_verdicts(state, entry, opts):
    if entry.get("emitted"):
        return "advance", "verdicts captured (or skipped) — continuing to publish"
    n = _scored_unseen()
    if n == 0:
        return "advance", "no freshly scored matches to review"
    return "gate", {
        "action": "verdicts",
        "count": n,
        "payload_path": None,
        "instructions": f"{n} freshly scored match(es) await a verdict.\n\n" + VERDICT_TEXT,
    }


def _h_publish(state, entry, opts):
    allowed, reasons = check_publish_gate(state)
    if not allowed:
        return (
            "skip",
            "publish SKIPPED — run not clean, previous good snapshot kept: " + "; ".join(reasons),
        )
    if opts.no_publish:
        return "skip", "publish suppressed (--no-publish) — run was clean and WOULD have published"
    rc = _run(_py("fetch_vacancies.py") + ["--report-only"], opts)
    if rc != 0:
        return "error", f"publish (--report-only) exited with code {rc}"
    return "advance", "dashboard refreshed (clean run)"


HANDLERS = {
    "validate_profile": _h_validate_profile,
    "preflight": _h_preflight,
    "onboarding": _h_onboarding,
    "learning_review": _h_learning_review,
    "fetch": _h_fetch,
    "enrich": _h_enrich,
    "filter": _h_filter,
    "company_scoring": _h_company_scoring,
    "vacancy_scoring": _h_vacancy_scoring,
    "verdicts": _h_verdicts,
    "publish": _h_publish,
}


# ---------------------------------------------------------------------------
# The driver loop
# ---------------------------------------------------------------------------


def drive(state: dict, opts: Opts) -> int:
    """Run AUTO stages back-to-back; stop at the first unmet GATE. Idempotent."""
    while True:
        idx = state["cursor"]
        if idx >= len(STAGE_ORDER):
            state["finished"] = True
            state["gate"] = None
            _save_state(state)
            return EXIT_DONE

        name = STAGE_ORDER[idx]
        entry = _stage(state, name)
        entry.setdefault("started_at", _now())
        entry["status"] = "running"
        _save_state(state)

        try:
            kind, info = HANDLERS[name](state, entry, opts)
        except Exception as exc:  # a handler crash is a stage error, not a driver crash
            entry["status"] = "error"
            entry["note"] = f"{type(exc).__name__}: {exc}"
            _save_state(state)
            print(f"\n✗ Stage '{name}' crashed: {entry['note']}", file=sys.stderr, flush=True)
            return EXIT_ERROR

        if kind in ("advance", "skip"):
            entry["status"] = "done" if kind == "advance" else "skipped"
            entry["note"] = info
            entry["finished_at"] = _now()
            state["cursor"] = idx + 1
            state["gate"] = None
            _save_state(state)
            print(f"  ▸ {name}: {info}", flush=True)
            continue

        if kind == "gate":
            entry["status"] = "blocked_gate"
            entry["emitted"] = True
            entry["gate"] = info
            state["gate"] = {"stage": name, "action": info.get("action")}
            _save_state(state)
            _emit_gate(name, info)
            return EXIT_GATE

        if kind == "error":
            entry["status"] = "error"
            entry["note"] = info
            _save_state(state)
            print(f"\n✗ Stage '{name}' failed: {info}", file=sys.stderr, flush=True)
            return EXIT_ERROR

        if kind == "abort":
            entry["status"] = "aborted"
            entry["note"] = info
            _save_state(state)
            print(f"\n✗ {info}", file=sys.stderr, flush=True)
            return EXIT_ABORT

        raise RuntimeError(f"stage {name} returned unknown result kind {kind!r}")


# ---------------------------------------------------------------------------
# Human summary
# ---------------------------------------------------------------------------


def _print_summary(state: dict) -> None:
    stats = _read_fetch_stats()
    new = stats.get("total_new")
    try:
        active = _company_count() and _scalar("SELECT count(*) FROM company WHERE status='active'")
        candidates = _scalar("SELECT count(*) FROM company WHERE status='candidate'")
        cand_scored = _scalar(
            "SELECT count(*) FROM company WHERE status='candidate' AND alignment_score IS NOT NULL"
        )
        scored_unseen = _scored_unseen()
        liked = _scalar("SELECT count(*) FROM vacancy WHERE status='liked'")
    except Exception:
        active = candidates = cand_scored = scored_unseen = liked = "?"

    publish_note = _stage(state, "publish").get("note", "")

    bar = "=" * 70
    print(f"\n{bar}")
    print("  ✓ /jobs-new complete")
    print(bar)
    if new is not None:
        print(f"  • {new} new vacancies saved this run")
    print(
        f"  • {active} active companies, {candidates} candidate ({cand_scored} scored, in Pending)"
    )
    print(f"  • {scored_unseen} scored matches await your verdict; {liked} liked so far")
    print(f"  • publish: {publish_note}")
    print("  • deeper review of liked roles: /jobs-review")
    print(bar, flush=True)


def _print_status() -> None:
    state = _load_state()
    if not state:
        print("No run on disk. Start one: python3 scripts/run_daily.py")
        return
    print(f"run {state.get('run_id')} — {'finished' if state.get('finished') else 'in progress'}")
    for s in state["stages"]:
        mark = {
            "done": "✓",
            "skipped": "–",
            "blocked_gate": "⏸",
            "error": "✗",
            "running": "…",
            "pending": " ",
        }.get(s.get("status", "pending"), "?")
        note = s.get("note", "")
        print(f"  [{mark}] {s['name']:<18} {note}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deterministic driver for the daily /jobs-new cycle.")
    p.add_argument("--resume", action="store_true", help="Continue an interrupted / gated run")
    p.add_argument("--new", action="store_true", help="Start a fresh run (discard any prior state)")
    p.add_argument("--status", action="store_true", help="Print the current run's stage board")
    p.add_argument(
        "--boards",
        type=str,
        default=None,
        help="JOB_BOARDS value for the fetch stage (e.g. '80k_hours,idealist'). Boards are opt-in.",
    )
    p.add_argument(
        "--full-rescore",
        action="store_true",
        help="Explicit opt-in: LIFT the per-run scoring cap and re-score (loud warning; costly).",
    )
    p.add_argument(
        "--no-publish",
        action="store_true",
        help="Run every stage but never publish (safe for a git worktree / dry run).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.status:
        _print_status()
        return EXIT_DONE

    existing = _load_state()
    if args.new:
        opts = Opts(
            job_boards=args.boards, full_rescore=args.full_rescore, no_publish=args.no_publish
        )
        state = _new_state(opts)
    elif args.resume:
        if not existing:
            print("No run to resume. Start one: python3 scripts/run_daily.py")
            return EXIT_ABORT
        state = existing
        opts = _opts_from_state(state)
        _warn_ignored_resume_flags(args)
        if args.no_publish:
            opts.no_publish = True
    elif existing and not existing.get("finished"):
        print(f"Resuming interrupted run {existing.get('run_id')} …", flush=True)
        state = existing
        opts = _opts_from_state(state)
        _warn_ignored_resume_flags(args)
        if args.no_publish:
            opts.no_publish = True
    else:
        opts = Opts(
            job_boards=args.boards, full_rescore=args.full_rescore, no_publish=args.no_publish
        )
        state = _new_state(opts)

    rc = drive(state, opts)

    if rc == EXIT_DONE:
        _print_summary(state)
    elif rc == EXIT_GATE:
        print(
            "\n⏸  Paused for your judgment (see the gate above). "
            "Do the task, then: python3 scripts/run_daily.py --resume"
        )
    elif rc == EXIT_ERROR:
        print(
            "\n✗ A stage failed. Inspect the output above, fix it, then "
            "python3 scripts/run_daily.py --resume (or --new to restart)."
        )
    elif rc == EXIT_ABORT:
        print("\n✗ Run aborted (see the message above). Fix it, then start a fresh run.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
