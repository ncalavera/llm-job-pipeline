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
LEARNING_PAYLOAD_PATH = PROJECT_ROOT / "vacancies" / "learning_review.json"

EXIT_DONE = 0
EXIT_GATE = 10
EXIT_ABORT = 20
EXIT_ERROR = 30


class BoardResolveError(RuntimeError):
    """The persisted board set could not be loaded because the DB is unreachable
    (a genuine outage, NOT the schema-predates-persistence case, which degrades
    to the override). Raised from _resolve_boards so main() can surface it through
    the documented EXIT_ABORT path — a clear message + a documented exit code —
    instead of the raw traceback + undocumented exit 1 it produced before, which
    fired BEFORE the stage machine's own DB-outage preflight stop. Subclasses
    RuntimeError and preserves the original message so callers that only assert
    'a failure propagates' still see it."""


# Publish gate: block a publish when a single org lost a large share of its live
# roles to gone-from-source archival this run (the signature of a truncated
# HTTP-200 fetch). A floor keeps a 1-of-1 tiny org from tripping the gate.
GONE_ARCHIVE_BLOCK_SHARE = 0.30
GONE_ARCHIVE_MIN_COUNT = 3

# Overload proxy: when this many already-scored, still-unseen roles await a
# verdict, new roles are arriving faster than they are reviewed.
# The run-start banner then SUGGESTS dialing volume down — it never applies it
# (STRATEGY guardrail 8). Scored-unseen is the honest measurable backlog: it is
# exactly the user's review queue, and unlike raw fetch counts it only grows when
# the user falls behind. 50 ≈ several days' worth of a normal daily surface.
OVERLOAD_BACKLOG = 50

# The canonical stage order. This list — not the runbook, not the maintainer's
# memory — is the single source of truth for what happens when.
#
#   * learning_review  — the verdict-driven feedback loop (STRATEGY guardrail 8):
#                        before a new fetch it offers filter/scoring/board
#                        corrections derived from the verdicts accumulated since
#                        last time. GATE when there is something to review, else
#                        a clean skip. Skippable in a hurry — skipped verdicts
#                        roll over to next run (the mechanics live in learning.py).
# company_scoring now runs the full candidate chain: drop junk → find a
# missing website → collect primary-source evidence → WANT-score. Scored
# candidates land in Pending for review.
STAGE_ORDER = [
    "validate_profile",  # AUTO  — abort early on a missing/placeholder profile
    "preflight",  # AUTO  — DB-outage hard-stop, first-run + resume detect
    "onboarding",  # GATE  — only when the company table is empty
    "learning_review",  # GATE  — verdict-driven corrections (skippable, rolls over)
    "fetch",  # AUTO  — pull new vacancies (heartbeat inside the script)
    "enrich",  # AUTO  — backfill blind descriptions (Firecrawl)
    "filter",  # AUTO  — quality report; never auto-deletes
    "company_scoring",  # GATE  — WANT-score new candidate companies
    "vacancy_scoring",  # GATE  — per-vacancy subagent scoring (1 vac = 1 agent)
    "verdicts",  # GATE  — show top matches, capture like/pass
    "publish",  # AUTO  — publish, gated on a clean run
]


# Plain-language "about to" line for each stage, printed at its START so a
# newcomer can tell what is happening without reading the code (STRATEGY
# guardrail 4). The finish is reported by the existing per-stage note.
STAGE_ABOUT = {
    "validate_profile": "checking your profile is present and personalised",
    "preflight": "checking the database and whether this is a first run",
    "onboarding": "onboarding starter companies (first run only)",
    "learning_review": "offering verdict-driven filter / scoring corrections",
    "fetch": "pulling new vacancies from your companies and boards",
    "enrich": "backfilling blind vacancy descriptions",
    "filter": "quality-filtering the freshly fetched roles",
    "company_scoring": "WANT-scoring new candidate companies",
    "vacancy_scoring": "scoring new roles (cheap screen, then strong finalists)",
    "verdicts": "collecting your like / pass verdicts",
    "publish": "publishing the dashboard (clean runs only)",
}


# One-line preview of what a GATE will ask for, printed before the run pauses so
# the operator knows the size of the judgment task up front.
def _gate_preview(action: str | None, count) -> str:
    n = count if count is not None else "?"
    return {
        "onboard": "onboard starter companies (the pipeline needs a seed list)",
        "learning_review": f"review {n} verdict-driven proposal(s)",
        "score_companies": f"WANT-score {n} candidate company(ies)",
        "score_vacancies": f"score {n} role(s)",
        "verdicts": f"give a like / pass verdict on {n} role(s)",
    }.get(action, f"complete a {action} task")


@dataclass
class Opts:
    """Run options — persisted in the state so ``--resume`` keeps them.

    ``job_boards`` is the ALREADY-resolved effective board set for this run
    (persisted enabled UNION ``--boards`` MINUS ``--skip-boards``); ``tier``
    scopes the fetch to one importance tier for this run only. Both are one-run
    scoping knobs — they never touch the persisted board.enabled set or a
    company's stored status/tier.
    """

    job_boards: str | None = None
    tier: str | None = None
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
            "tier": opts.tier,
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


def _state_file_corrupt() -> bool:
    """True when a checkpoint FILE exists on disk but cannot be parsed.

    ``_load_state`` collapses both "no checkpoint" and "checkpoint present but
    unreadable" to ``None``. That conflation is dangerous: on a corrupt file
    ``--resume`` would falsely report "No run to resume", and a BARE invocation
    would silently start a fresh run and overwrite the (possibly recoverable)
    checkpoint. Distinguishing the two lets ``main`` stop and tell the user
    instead of lying or clobbering."""
    if not STATE_PATH.exists():
        return False
    try:
        json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return False
    except Exception:
        return True


def _stage(state: dict, name: str) -> dict:
    for s in state["stages"]:
        if s["name"] == name:
            return s
    raise KeyError(name)


def _opts_from_state(state: dict) -> Opts:
    o = state.get("options", {}) or {}
    return Opts(
        job_boards=o.get("job_boards"),
        tier=o.get("tier"),
        full_rescore=bool(o.get("full_rescore")),
        no_publish=bool(o.get("no_publish")),
    )


def _ignored_resume_flags(args: argparse.Namespace) -> list[str]:
    """CLI flags whose effect a resume cannot honour: options are frozen at the
    checkpoint, not re-read from the CLI, so ``--boards`` / ``--skip-boards`` /
    ``--tier`` / ``--full-rescore`` have no effect on a resumed run (the fetch
    they scope has already run)."""
    flags = []
    if args.boards is not None:
        flags.append("--boards")
    if args.skip_boards is not None:
        flags.append("--skip-boards")
    if args.tier is not None:
        flags.append("--tier")
    if args.full_rescore:
        flags.append("--full-rescore")
    return flags


def _warn_ignored_resume_flags(args: argparse.Namespace) -> None:
    """Resuming replays the options frozen in the checkpoint, not the CLI's.

    ``--boards`` and ``--full-rescore`` are silently dropped on resume unless we
    say so — a user re-running with ``--full-rescore`` to lift the scoring cap
    would otherwise get a normal-cap run with no indication anything changed.
    """
    for flag in _ignored_resume_flags(args):
        print(
            f"⚠  {flag} is IGNORED on resume: this run's options are locked in "
            "at the checkpoint, not re-read from the CLI. To apply it, finish "
            "or discard the current run (--new) and start over.",
            flush=True,
        )


def _age_str(ts: str | None) -> str:
    """Human age of an ISO timestamp written by ``_now()`` (naive local)."""
    if not ts:
        return "unknown"
    try:
        secs = int((datetime.now() - datetime.fromisoformat(ts)).total_seconds())
    except Exception:
        return "unknown"
    secs = max(secs, 0)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _resume_stage_name(state: dict) -> str:
    """The stage a resume will pick up at (the cursor position)."""
    idx = state.get("cursor", 0)
    if isinstance(idx, int) and 0 <= idx < len(STAGE_ORDER):
        return STAGE_ORDER[idx]
    return "done"


def _print_resume_banner(state: dict) -> None:
    """Loud, unmissable banner when a BARE invocation auto-resumes an unfinished
    run, so a resume is never mistaken for a fresh start.

    Wrapped so a banner failure can never abort the run (STRATEGY goal 1)."""
    try:
        bar = "=" * 70
        run_id = state.get("run_id", "?")
        print(f"\n{bar}", flush=True)
        print("  ⏯  RESUMING AN UNFINISHED RUN — this is NOT a fresh start.", flush=True)
        print(bar, flush=True)
        print(f"  run:      {run_id}", flush=True)
        print(
            f"  age:      started {_age_str(state.get('created_at'))} ago; "
            f"last progress {_age_str(state.get('updated_at'))} ago",
            flush=True,
        )
        print(f"  picks up: at stage '{_resume_stage_name(state)}'", flush=True)
        print(
            "  To DISCARD this run and start fresh instead: python3 scripts/run_daily.py --new",
            flush=True,
        )
        print(bar, flush=True)
    except Exception:
        # A banner is informational; never let it break the run.
        pass


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
    except Exception as exc:
        # An unexpected failure here means we CANNOT confirm the registry loaded.
        # Fail toward the preflight abort (return True) rather than reporting
        # "registry OK" and letting destructive first-run onboarding proceed
        # against what might be a DB outage. Surface the cause.
        print(
            f"  registry-load check failed unexpectedly ({type(exc).__name__}: {exc}); "
            "treating as a load failure to be safe.",
            file=sys.stderr,
            flush=True,
        )
        return True


def _company_count() -> int:
    return _scalar("SELECT count(*) FROM company")


def _candidates_to_score() -> int:
    return _scalar(
        "SELECT count(*) FROM company "
        "WHERE status = 'candidate' AND alignment_score IS NULL "
        "AND website IS NOT NULL AND website != ''"
    )


def _ghost_candidate_count() -> int:
    """Candidates a board discovered with NO website yet — unscorable until a
    site is found. These are the 'candidate without a site' rows the enrichment
    chain backfills so they don't hang unscored forever."""
    return _scalar(
        "SELECT count(*) FROM company "
        "WHERE status = 'candidate' AND alignment_score IS NULL "
        "AND (website IS NULL OR website = '')"
    )


def _candidate_names_to_score(limit: int) -> list[str]:
    """Canonical names of the candidates the scorer will pick this run.

    Mirrors score_companies._load_companies' selection (candidate, no score, has
    website, ordered by name) so evidence is collected for EXACTLY the companies
    that go to scoring — no more (STRATEGY guardrail 3: evidence is not free)."""
    from db_conn import get_conn

    cur = get_conn().cursor()
    cur.execute(
        "SELECT canonical_name FROM company "
        "WHERE status = 'candidate' AND alignment_score IS NULL "
        "AND website IS NOT NULL AND website != '' "
        "ORDER BY canonical_name"
    )
    names = [r[0] for r in cur.fetchall()]
    cur.close()
    return names[:limit] if limit else names


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


def _vacancy_scores(target_ids: list[str]) -> dict[str, int]:
    """Current ``llm_score`` for each target member id — the SCREEN scores once
    the cheap pass has saved them. Archived rows (a geo-ban net can archive one
    at save time) are excluded so they never waste a strong escalation call."""
    from database_supabase import load_vacancies

    vacs = load_vacancies(include_candidate_companies=True, include_inactive_companies=True)
    out: dict[str, int] = {}
    for vid in target_ids:
        v = vacs.get(vid)
        if v and v.get("llm_score") is not None and v.get("status") != "archived":
            out[vid] = int(v["llm_score"])
    return out


def _vacancy_scored_by(target_ids: list[str]) -> dict[str, str]:
    """Current ``scored_by`` model tier for each target member id — the
    same-model guard reads this so a role already scored by the STRONG model is
    never re-sent to it (see ``select_escalation_payloads``). Missing/unset
    values (pre-migration installs, or a row with no score yet) are simply
    absent."""
    from database_supabase import load_vacancies

    vacs = load_vacancies(include_candidate_companies=True, include_inactive_companies=True)
    out: dict[str, str] = {}
    for vid in target_ids:
        v = vacs.get(vid)
        if v and v.get("scored_by"):
            out[vid] = v["scored_by"]
    return out


def _reset_escalation_scores(member_ids: list[str]) -> None:
    """Null the cheap screen score for the finalists so the escalate phase reuses
    the same IS-NULL idempotency the screen phase uses. Commits — the
    DAL writer does not."""
    if not member_ids:
        return
    from database_supabase import reset_llm_scores
    from db_conn import get_conn

    reset_llm_scores(member_ids)
    get_conn().commit()


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _board_id_set(raw: str | None) -> set[str]:
    """The board ids in a comma-separated flag value (blanks dropped)."""
    return {t.strip() for t in (raw or "").split(",") if t.strip()}


def _skip_board_set(raw: str | None) -> tuple[set[str], bool]:
    """Parse ``--skip-boards`` into ``(ids_to_skip, skip_all)``.

    A typo'd skip that silently no-ops defeats the whole flag — the board still
    runs and the user never learns — so every token is validated against the
    known board catalog (the same source config._select_enabled_boards warns
    from) and an unknown one warns LOUDLY. Warn, don't abort: consistent with
    how an unknown ``--boards`` id is handled downstream. ``all`` mirrors
    ``--boards all`` symmetrically: skip every board (boards off this run;
    companies still fetch)."""
    tokens = _board_id_set(raw)
    if not tokens:
        return set(), False
    if any(t.lower() == "all" for t in tokens):
        return tokens, True
    from config import _ALL_JOB_BOARDS

    for tok in sorted(tokens):
        if tok not in _ALL_JOB_BOARDS:
            print(
                f"  WARNING: unknown board in --skip-boards: {tok} "
                f"(known: {', '.join(_ALL_JOB_BOARDS)})",
                file=sys.stderr,
                flush=True,
            )
    return tokens, False


def _resolve_boards(cli_boards: str | None, skip_boards: str | None = None) -> str | None:
    """The effective JOB_BOARDS value for a fresh run.

    The persisted enabled set (survives sessions -- see
    database_supabase.set_board_enabled) UNION the manual add-on override (the
    ``--boards`` flag and any inherited ``JOB_BOARDS`` env), MINUS the per-run
    ``--skip-boards`` subtraction, so an enabled board keeps fetching with no
    reminder, ``--boards`` adds more ON TOP for one run, and ``--skip-boards``
    drops boards from THIS run only (the persisted set is never written). Returns
    a comma-joined id list, ``"all"`` if any override says all (and nothing is
    skipped), or ``None`` when nothing is selected (fetch stays boards-off).

    Resolved ONCE per fresh run and frozen into the run state, so ``--resume``
    replays a consistent board set. A schema that predates board persistence (a
    fresh clone before onboarding runs migrate.py) falls back to the override
    alone -- that must never break the daily run (STRATEGY goal 1). Any OTHER
    failure (DB down, real regression) propagates: degrading it silently to
    boards-off would be indistinguishable from the fresh-clone case."""
    persisted: list[str] = []
    try:
        from database_supabase import BoardPersistenceUnavailable, get_enabled_boards

        try:
            persisted = list(get_enabled_boards())
        except BoardPersistenceUnavailable as exc:
            print(f"  (persisted board set unavailable: {exc}; using override only)", flush=True)
        except Exception as exc:  # noqa: BLE001 — a genuine DB outage, not schema-missing
            # Do NOT silently degrade to boards-off: that is indistinguishable
            # from the fresh-clone case. Raise a typed error so main() aborts
            # through the documented path instead of crashing with a raw traceback.
            raise BoardResolveError(str(exc)) from exc
    finally:
        _close_db()

    skip, skip_all = _skip_board_set(skip_boards)
    if skip_all:
        # --skip-boards all mirrors --boards all: every board is dropped for
        # THIS run (companies still fetch); the persisted set is untouched.
        return None

    tokens = list(persisted)
    saw_all = False
    for raw in (cli_boards, os.environ.get("JOB_BOARDS")):
        if not raw:
            continue
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok.lower() == "all":
                saw_all = True
                continue
            if tok not in tokens:
                tokens.append(tok)

    if saw_all:
        # With nothing to subtract, "all" stays the compact wildcard the fetcher
        # already understands. When --skip-boards is present we must materialise
        # "all" to the explicit known-board list so the skip can bite — otherwise
        # the subtraction would be silently lost against the wildcard.
        if not skip:
            return "all"
        from config import _ALL_JOB_BOARDS

        tokens = [b for b in _ALL_JOB_BOARDS if b not in skip]
        return ",".join(tokens) if tokens else None

    if skip:
        tokens = [t for t in tokens if t not in skip]
    return ",".join(tokens) if tokens else None


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
# Company enrichment chain: before WANT-scoring, a new candidate
# company is made scorable — structural junk is dropped, a missing website is
# found, and primary-source evidence is collected. This is the deterministic
# prep the scoring GATE depends on; it runs inside company_scoring, never as a
# side path (STRATEGY guardrail 5).
# ---------------------------------------------------------------------------


def _prefilter_junk_companies(opts: Opts) -> None:
    """Drop structural junk candidates (gov / university / aggregator) before we
    spend Firecrawl credits enriching them. Best-effort: a failure here must not
    block scoring, so its exit code is advisory only."""
    rc = _run(_py("filter_companies.py") + ["--apply"], opts)
    if rc != 0:
        print("  ⚠  company junk pre-filter exited non-zero — continuing", flush=True)


def _backfill_candidate_websites(opts: Opts) -> None:
    """Find official websites for ghost candidates (board-discovered, no URL) so
    they enter scoring instead of hanging unscored. Capped at the same per-run
    safety net already used for the scoring set (``scoring_settings.max_per_run``
    — STRATEGY guardrail 3: cost) — without it, a board dump of hundreds of
    ghost candidates would fire one paid Firecrawl search() per ghost in a
    single run. Best-effort: a company whose site can't be found stays
    unscored and is reported by the stage note, not silently dropped; anything
    past the cap simply waits for a later run (no silent caps — we say how
    many)."""
    ghosts = _ghost_candidate_count()
    if ghosts == 0:
        return
    from scoring_settings import max_per_run

    cap = max_per_run()
    capped = min(ghosts, cap)
    if ghosts > cap:
        print(
            f"  Ghost candidates without a website: {ghosts} — searching for the "
            f"first {cap} (per-run cap); {ghosts - cap} deferred to a later run",
            flush=True,
        )
    else:
        print(f"  Ghost candidates without a website: {ghosts} — searching for URLs", flush=True)
    rc = _run(_py("find_company_urls.py") + ["--limit", str(capped)], opts)
    if rc != 0:
        print("  ⚠  website backfill exited non-zero — continuing", flush=True)


def _collect_company_evidence(names: list[str], opts: Opts) -> None:
    """Collect primary-source evidence (company_evidence rows) for the companies
    about to be scored. Runs BEFORE score_companies so the scorer reads real
    primary text, not the legacy scrape cache. The caller has already capped
    ``names`` to at most ``scoring_settings.max_per_run`` candidates (the same
    per-run set that fetch and scoring receive), so evidence — a paid path
    (STRATEGY guardrail 3) — is collected for exactly the companies scored this
    run and no more."""
    if not names:
        return
    print(f"  Collecting primary-source evidence for {len(names)} company(ies)", flush=True)
    rc = _run(_py("collect_company_evidence.py") + ["--company", ",".join(names)], opts)
    if rc != 0:
        # LOUD, not silent: a failed collection means scoring will degrade to the
        # scrape cache. score_companies also warns per-company, but say it here too.
        print(
            "  ⚠  evidence collection exited non-zero — WANT scores may fall back to "
            "the scrape cache (degraded). Check the output above.",
            flush=True,
        )


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


def select_escalation_payloads(
    payloads: list,
    scores_by_member: dict[str, int],
    threshold: int,
    scored_by_member: dict[str, str] | None = None,
    escalate_model: str | None = None,
) -> list:
    """The finalists for the strong pass: screen payloads whose cheap
    score reached ``threshold``. A role escalates if any of its member vacancies
    scored at/above the floor (members of one deduped role share a score). Roles
    with no recorded screen score (e.g. archived at save time) never escalate.

    Same-model guard: when ``scored_by_member`` + ``escalate_model`` are
    given, a role is EXCLUDED if any of its members' current score already came
    from ``escalate_model`` — that role was already scored by the strong model
    (e.g. a stale run-state, or a profile changed mid-run), so re-sending it
    would pay for an identical result. Pure — unit-tested directly; the
    escalation set is always a SUBSET of the screened set, so the strong pass
    can never cost more than the cheap one.
    """
    scored_by_member = scored_by_member or {}
    out = []
    for p in payloads:
        member_ids = [str(m) for m in p.get("member_ids", [])]
        vals = [scores_by_member[m] for m in member_ids if m in scores_by_member]
        if not (vals and max(vals) >= threshold):
            continue
        if escalate_model and any(scored_by_member.get(m) == escalate_model for m in member_ids):
            continue
        out.append(p)
    return out


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
    print(
        f"  This gate will ask you to: {_gate_preview(payload.get('action'), payload.get('count'))}"
    )
    print("")
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

  python3 scripts/vac.py mark <VACANCY_ID> liked

(<VACANCY_ID> may be a UUID prefix, 4+ chars; the command validates the status
and commits. Do NOT use a `python3 -c` one-liner for this — ad-hoc one-liners
are not an allowlisted entrypoint, so the prod-write guard blocks their writes.)

Statuses: liked | passed | skipped | to_apply. A plain `passed` means "not for
me" and calibrates scoring. If instead a role is GARBAGE — it should never have
reached scoring at all (a filter hole that burned tokens) — pass it AND flag it,
so next run's learning review can propose a filter for it:

  python3 scripts/learning.py record-garbage --vacancy <VACANCY_ID> \\
      --title "<title>" --source "<board/ats>" --score <llm_score>

This is the quick daily pass; the deep structured review of liked roles lives in
/jobs-review. Then --resume to publish.
""".strip()


def _learning_gate_text(review: dict) -> str:
    fw = review["proposals"]["filter_words"]
    fm = review["proposals"]["factor_moves"]
    rev = review["revision"]
    agr = review["agreement"]
    if agr.get("measured"):
        agr_line = f"{agr['value']:.0f}%"
        if agr.get("measured_at"):
            agr_line += f" (measured {agr['measured_at']})"
        if agr.get("previous") is not None:
            agr_line += f", was {agr['previous']}"
    else:
        agr_line = "not yet measured — run /jobs-eval"
    return (
        f"LEARNING REVIEW — verdicts since your last review teach the filters,\n"
        f"scoring and boards. Nothing changes without your explicit yes; every\n"
        f"applied change is logged. In a hurry? SKIP — just --resume and these\n"
        f"roll over to next time.\n\n"
        f"  • {review['verdicts_since_last_review']} verdict(s) since last review;"
        f" {review['garbage_count']} flagged garbage (filter holes)\n"
        f"  • scoring agreement with your verdicts: {agr_line}\n"
        f"  • {len(fw)} filter-word proposal(s), {len(fm)} factor-move proposal(s),"
        f" {len(rev)} killed title(s) to revisit\n\n"
        f"Full payload (proposals each carry their backtest): {LEARNING_PAYLOAD_PATH}\n\n"
        f"Do this:\n"
        f"  1. Read the payload. For EACH proposal, show the user the word/move and\n"
        f"     its backtest (clean = it would have killed 0 liked/high-scored roles;\n"
        f"     a dirty candidate lists the exact roles it would have wrongly killed).\n"
        f"  2. Apply ONLY the ones the user approves:\n"
        f"       python3 scripts/learning.py apply --type add_filter_word --word W\n"
        f'       python3 scripts/learning.py apply --type move_factor --factor "..." --keyword K\n'
        f"       python3 scripts/learning.py apply --type disable_board --board B\n"
        f'  3. Filter-kill revision ("anything alive here?"): for any killed title the\n'
        f"     user says is actually good, weaken its culprit rule:\n"
        f"       python3 scripts/learning.py apply --type weaken_filter_word --word CULPRIT\n"
        f"  4. When done (you engaged — even if you applied nothing), close the cycle so\n"
        f"     these verdicts do not reappear next run:\n"
        f"       python3 scripts/learning.py complete --agreement <n|skip> --applied <k>"
        f" [--revision-shown]\n"
        f"     To SKIP instead, do NOT run 'complete' — just --resume; verdicts roll over.\n"
    )


def _vacancy_gate_text(
    payloads: list, model: str, pass_kind: str, threshold: int | None = None
) -> str:
    """Instructions for a vacancy-scoring gate, tailored to which two-pass pass
    it is. ``pass_kind`` is 'screen' (cheap, first pass of a real two-pass run),
    'single' (screen_model == scoring_model — ONE pass, no escalate gate),
    'escalate' (strong, finalists only) or 'rescore' (the deliberate
    full re-score)."""
    if pass_kind == "single":
        head = (
            f'SCORE pass — score {len(payloads)} new vacancy(ies) with "{model}". Your '
            f'profile\'s [## VOLUME] screen_model and scoring_model are BOTH "{model}", so '
            f"this run does ONE pass — there is no escalate gate, and no role is sent back "
            f'to "{model}" a second time.'
        )
    elif pass_kind == "screen":
        head = (
            f"SCREEN pass — score {len(payloads)} new vacancy(ies) with the CHEAP model "
            f'"{model}" (from your profile\'s [## VOLUME] screen_model). This is the fast, '
            f"inexpensive first look at EVERY new role; the strong model re-scores only the "
            f"finalists next."
        )
    elif pass_kind == "escalate":
        floor = f" (screen score >= {threshold})" if threshold is not None else ""
        head = (
            f"ESCALATION pass — re-score {len(payloads)} finalist(s) with the STRONG model "
            f'"{model}" (from your profile\'s [## VOLUME] scoring_model). Only roles that cleared '
            f'the escalation threshold{floor} and were NOT already scored by "{model}" are '
            f"here; everything else keeps its cheap screen score, sorted out of view."
        )
    else:  # rescore — the deliberate full re-score one-shot (cap lifted)
        head = (
            f"Full re-score — score {len(payloads)} vacancy(ies) with the STRONG model "
            f'"{model}" (from your profile\'s [## VOLUME] scoring_model). The per-run cap is '
            f"lifted for this deliberate pass."
        )
    return (
        head + "\n\n"
        "Each entry in the payload file carries its own system_prompt + user_msg\n"
        "and the real DB member_ids. CRITICAL: 1 vacancy = 1 subagent (batching\n"
        "over-scores by +20-50). Run at most 5 subagents at a time (rolling waves).\n\n"
        "Save results incrementally (each --save commits, so an interrupt keeps\n"
        f'finished work) — pass --scored-by "{model}" so the score\'s provenance is\n'
        "recorded (a kept-cheap score must never look identical to a confirmed one):\n"
        f'  python3 scripts/score_vacancies.py --save --scored-by "{model}" < chunk.json\n\n'
        "If you saved each subagent's raw result to its own file, pass them as\n"
        "--files instead of assembling one JSON array — a malformed file (a kill\n"
        "mid-write, an unescaped quote) is then named and skipped, the rest still\n"
        "save (BUG-5), instead of one bad file failing the whole batch:\n"
        f'  python3 scripts/score_vacancies.py --save --scored-by "{model}" '
        "--files r1.json r2.json ...\n\n"
        "Flat per-vacancy fields to save: member_ids, org, title, score, reasoning,\n"
        "tags, hard_requirements, short_summary. Then --resume — the driver\n"
        "re-checks and re-prompts only for any vacancy still unscored."
    )


def _company_gate_text(payloads: list) -> str:
    return (
        f"WANT-score {len(payloads)} new candidate company(ies) — ONE subagent per\n"
        f'company, model "sonnet". Each payload entry carries its own system_prompt\n'
        f"+ user_msg. 1 company = 1 subagent; at most 5 at a time (rolling waves).\n\n"
        f"Wrap each result under 'enrichment' and save incrementally:\n"
        f"  python3 scripts/score_companies.py --save < chunk.json\n\n"
        f"If you saved each subagent's raw result to its own file, pass them as\n"
        f"--files instead — a malformed file is then named and skipped, the rest\n"
        f"still save (BUG-5), instead of one bad file failing the whole batch:\n"
        f"  python3 scripts/score_companies.py --save --files r1.json r2.json ...\n\n"
        f"Scored companies land in Companies -> Pending for approval (deeper review\n"
        f"in /jobs-review companies --status candidate). Then --resume.\n\n"
        f"These payloads were built from primary-source company_evidence collected\n"
        f"earlier this stage (websites were auto-found for board candidates that had\n"
        f"none). Any company still missing evidence carries a loud warning above."
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
    # Verdict-driven feedback loop at the START of a run (STRATEGY guardrail 8).
    # Deterministic mechanics live in learning.py; here we only decide whether to
    # stop for the agent's judgment. On resume (emitted) we always advance — the
    # agent either engaged (and ran `learning.py complete`, moving the rollover
    # cursor) or skipped (cursor unchanged → verdicts roll over next run).
    if entry.get("emitted"):
        return "advance", "learning review done (or skipped — skipped verdicts roll over)"

    # First run has no verdict history to learn from; onboarding just happened.
    if state.get("first_run"):
        return "skip", "first run — no verdict history to learn from yet"

    try:
        import learning
    except Exception as exc:  # never let the learning loop break the daily run
        return "skip", f"learning review skipped — module unavailable ({type(exc).__name__})"

    try:
        if not learning.table_ready():
            return (
                "skip",
                "learning review skipped — learning_log table missing; run scripts/migrate.py "
                "to enable verdict-driven corrections",
            )
        if learning.cursor_ts() is None:
            # Cold start: the ledger has never recorded a completed review — a
            # fresh deploy of the learning cycle over an EXISTING verdict
            # back-catalog (this is not the empty-DB first_run case above,
            # which is handled separately). Dumping the whole history as a
            # review the first time it runs would be a wall of noise, so seed
            # the rollover cursor silently and start counting from adoption.
            learning.mark_reviewed(agreement=None, applied_count=0, revision_shown=False)
            return (
                "skip",
                "learning cycle just adopted — cursor seeded silently; counting verdicts "
                "from now on",
            )
        review = learning.build_review()
    except Exception as exc:
        # A learning glitch must never block the daily loop (reliability first).
        return "skip", f"learning review skipped — could not build review ({type(exc).__name__})"

    if not review.get("has_content"):
        return "advance", "no accumulated verdicts or proposals — nothing to review this run"

    _write_payload(LEARNING_PAYLOAD_PATH, review)
    return "gate", {
        "action": "learning_review",
        "count": review["verdicts_since_last_review"],
        "payload_path": str(LEARNING_PAYLOAD_PATH),
        "instructions": _learning_gate_text(review),
    }


def _h_fetch(state, entry, opts):
    # -u (unbuffered) is a python flag, not a script flag — so the live card sees
    # the heartbeat immediately while fetch runs in the background.
    # --no-auto-enrich: the driver's company_scoring stage owns candidate
    # enrichment + WANT-scoring (find site → collect evidence → score). Letting
    # fetch also enrich inline would be a degraded parallel path (STRATEGY 5).
    cmd = [
        sys.executable,
        "-u",
        str(SCRIPTS_DIR / "fetch_vacancies.py"),
        "--no-dashboard",
        "--no-auto-enrich",
    ]
    # Per-run importance-tier scope: fetch_vacancies.py already knows how to keep
    # only companies of one calculated tier. This scopes the fetch for THIS run
    # only; no company's stored status/tier is touched.
    if opts.tier:
        cmd += ["--tier", opts.tier]
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

    # Without Firecrawl we can neither find missing sites nor scrape/collect
    # evidence, so there is nothing this stage can do but leave candidates for a
    # manual pass. Count only the already-scorable (website-bearing) rows for the
    # message; ghost candidates simply wait until a Firecrawl key is present.
    if not os.environ.get("FIRECRAWL_API_KEY"):
        n = _candidates_to_score()
        if n == 0:
            return "advance", "no new candidate companies to score"
        return (
            "skip",
            f"{n} candidate companies found but FIRECRAWL_API_KEY unset — left "
            "unscored (approve manually later via /jobs-review)",
        )

    # Enrichment chain — make new candidates scorable before scoring:
    #   1. drop structural junk so we don't pay to enrich it,
    #   2. find websites for ghost candidates (board-discovered, no URL),
    #   3. count what is now scorable,
    #   4. scrape about-pages + collect primary-source evidence for that set,
    #   5. build the WANT-scoring payloads for the gate.
    _prefilter_junk_companies(opts)
    _backfill_candidate_websites(opts)

    n = _candidates_to_score()
    if n == 0:
        return "advance", "no new candidate companies to score"

    # Spike-day cost cap (STRATEGY guardrail 3). The whole PAID chain — Firecrawl
    # scrape (fetch_companies), primary-source evidence collection, and
    # WANT-scoring — must run on at most max_per_run() candidates. Passing the
    # uncapped count as an explicit --limit to score_companies would SUPPRESS its
    # own internal cap (that cap only applies when --limit is None), so the cap is
    # applied here and threaded into all three steps, exactly as
    # _backfill_candidate_websites caps the website search. The candidates,
    # scoring set, and evidence set share one canonical_name ordering, so the same
    # first `capped` rows flow through every step; the rest stay candidates and
    # are picked up next run — reported here, never silently dropped.
    from scoring_settings import max_per_run

    cap = max_per_run()
    capped = min(n, cap)
    if n > cap:
        print(
            f"  {n} candidate companies to score — scoring the first {cap} this run "
            f"(per-run cost cap); {n - cap} deferred to a later run",
            flush=True,
        )

    names = _candidate_names_to_score(capped)
    rc = _run(_py("fetch_companies.py") + ["--limit", str(capped)], opts)
    if rc != 0:
        return "error", f"company scrape exited with code {rc}"
    _collect_company_evidence(names, opts)

    res = _run_capture(_py("score_companies.py") + ["--local", "--limit", str(capped)], opts)
    if res.returncode != 0:
        return "error", f"score_companies --local exited {res.returncode}: {res.stderr[-400:]}"
    # score_companies --local prints its "N with company_evidence rows" line and a
    # LOUD warning for any company with no evidence to stderr — surface both.
    if res.stderr.strip():
        print(res.stderr.strip(), file=sys.stderr, flush=True)
    try:
        payloads = _extract_json(res.stdout or "[]")
    except Exception:
        return "error", "score_companies --local did not emit valid JSON"
    if not payloads:
        return "advance", "candidate companies had no evidence or scrape cache — nothing to score"
    _write_payload(CO_PAYLOAD_PATH, payloads)
    entry["target_ids"] = [str(p["id"]) for p in payloads]
    ghosts_left = _ghost_candidate_count()
    notes = []
    if n > cap:
        notes.append(f"{n - cap} candidate(s) deferred to a later run (per-run cap {cap})")
    if ghosts_left:
        notes.append(f"{ghosts_left} candidate(s) still lack a findable website")
    note_suffix = ("; " + "; ".join(notes)) if notes else ""
    return "gate", {
        "action": "score_companies",
        "count": len(payloads),
        "payload_path": str(CO_PAYLOAD_PATH),
        "instructions": _company_gate_text(payloads)
        + (f"\n\n[note]{note_suffix}" if note_suffix else ""),
    }


def _corrupt_score_payload_msg(n_unscored: int, phase: str) -> str:
    """Error text for a resume that can't map its still-unscored vacancies back to
    gate tasks — the payload file is missing/truncated/unreadable. Failing loudly
    here is the whole point: an empty read must NOT be mistaken for 'scoring
    complete' and silently skip these rows into a publish."""
    return (
        f"score payload {VAC_PAYLOAD_PATH} is missing or unreadable, but {n_unscored} "
        f"vacancy(ies) are still unscored in the DB ({phase} pass). Refusing to report "
        "scoring complete (that would skip them and publish). Re-run scoring: "
        "python3 scripts/run_daily.py --new (discards the run), or restore the payload "
        "file and --resume."
    )


def _h_vacancy_scoring(state, entry, opts):
    """Two-pass vacancy scoring: a CHEAP model screens every new role,
    the STRONG model re-scores only the finalists that clear the calibrated
    escalation floor; everything else keeps its cheap score, sorted out of view.

    When the profile's ``screen_model`` and ``scoring_model`` are the
    SAME tier, the escalate gate would just re-send a role to the model that
    already scored it — same result, paid twice. In that case the run does
    ONE pass and says so explicitly (no escalate gate at all). Even in a real
    two-pass run, a role whose current score already came from the escalate
    model (``vacancy.scored_by``) is never re-sent to it — a defensive guard
    for a stale run-state or a profile changed mid-run (``select_escalation_
    payloads``).

    The split is deterministic Python; the agent only supplies scores at the two
    gates. ``--resume`` works mid-screen AND mid-escalate because both phases use
    the same ``llm_score IS NULL`` idempotency: at the screen->escalate handshake
    the finalists' cheap score is nulled so the strong pass re-scores them.

    A ``--full-rescore`` is a deliberate one-shot single STRONG pass with the cap
    lifted — it can't use the two-pass IS-NULL handshake (``--force`` leaves every
    row already scored), so it stays single-pass.
    """
    import run_status
    from scoring_settings import (
        escalation_threshold,
        escalation_threshold_warning,
        scoring_model,
        screen_model,
    )

    strong_model = scoring_model()
    cheap_model = screen_model()
    single_pass = cheap_model == strong_model

    # ---- First entry: emit the SCREEN pass (or a full-rescore one-shot) ----
    if not entry.get("emitted"):
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
        run_status.begin("score", len(payloads))

        if opts.full_rescore:
            entry["oneshot"] = True  # force-rescore can't use the "llm_score IS NULL" check
            return "gate", {
                "action": "score_vacancies",
                "count": len(payloads),
                "payload_path": str(VAC_PAYLOAD_PATH),
                "instructions": _vacancy_gate_text(payloads, strong_model, "rescore"),
            }

        entry["phase"] = "screen"
        if single_pass:
            entry["single_pass"] = True
        return "gate", {
            "action": "score_vacancies",
            "count": len(payloads),
            "payload_path": str(VAC_PAYLOAD_PATH),
            "instructions": _vacancy_gate_text(
                payloads, cheap_model, "single" if single_pass else "screen"
            ),
        }

    if entry.get("oneshot"):
        return (
            "advance",
            "full re-score handed off (one-shot single strong pass; re-run --full-rescore "
            "if incomplete)",
        )

    payloads = _read_payload(VAC_PAYLOAD_PATH)
    phase = entry.get("phase", "screen")
    single_pass = entry.get("single_pass", False)

    # ---- SCREEN (or SINGLE) phase: the first (and maybe only) pass ----
    if phase == "screen":
        remaining_ids = _unscored_vacancy_ids(entry.get("target_ids", []))
        remaining = [
            p for p in payloads if any(str(m) in remaining_ids for m in p.get("member_ids", []))
        ]
        if remaining_ids and not remaining:
            return "error", _corrupt_score_payload_msg(len(remaining_ids), "screen")
        if remaining:
            return "gate", {
                "action": "score_vacancies",
                "count": len(remaining),
                "payload_path": str(VAC_PAYLOAD_PATH),
                "instructions": _vacancy_gate_text(
                    remaining, cheap_model, "single" if single_pass else "screen"
                ),
            }

        screened = len(payloads)

        # screen_model == scoring_model -> one pass, no escalate gate.
        if single_pass:
            # Durable run history reads screen_counts from this stage entry
            # (run observability) — a single-pass run must record its
            # scored count too, not just the two-pass path. kept_cheap mirrors
            # the two-pass invariant (screened == escalated + kept_cheap):
            # every role keeps its one (and only) score.
            entry["screen_counts"] = {
                "screened": screened,
                "escalated": 0,
                "kept_cheap": screened,
                "threshold": None,
                "already_strong": 0,
            }
            run_status.finish()
            return (
                "advance",
                f'single-pass scoring complete — {screened} scored with "{strong_model}" '
                "(screen_model == scoring_model; no escalate gate needed)",
            )

        # Screen complete — pick the finalists for the strong pass.
        threshold = escalation_threshold()
        threshold_warn = escalation_threshold_warning(threshold)
        if threshold_warn:
            print(threshold_warn, flush=True)
        scores = _vacancy_scores(entry.get("target_ids", []))
        scored_by = _vacancy_scored_by(entry.get("target_ids", []))
        cleared_floor = select_escalation_payloads(payloads, scores, threshold)
        escalate = select_escalation_payloads(payloads, scores, threshold, scored_by, strong_model)
        already_strong = len(cleared_floor) - len(escalate)
        n_esc = len(escalate)
        entry["screen_counts"] = {
            "screened": screened,
            "escalated": n_esc,
            "kept_cheap": screened - n_esc,
            "threshold": threshold,
            "already_strong": already_strong,
        }
        if not escalate:
            run_status.finish()
            if already_strong:
                why = f'{already_strong} already scored by "{strong_model}" (not re-sent)'
            else:
                why = f"none scored >= {threshold}"
            return (
                "advance",
                f'two-pass scoring complete — screened {screened} with "{cheap_model}", '
                f'0 escalated to "{strong_model}" ({why}); {screened} kept their cheap score',
            )
        esc_member_ids = [str(m) for p in escalate for m in p.get("member_ids", [])]
        _reset_escalation_scores(esc_member_ids)
        _write_payload(VAC_PAYLOAD_PATH, escalate)
        entry["escalate_target_ids"] = esc_member_ids
        entry["phase"] = "escalate"
        run_status.begin("score", len(escalate))
        return "gate", {
            "action": "score_vacancies",
            "count": len(escalate),
            "payload_path": str(VAC_PAYLOAD_PATH),
            "instructions": _vacancy_gate_text(escalate, strong_model, "escalate", threshold),
        }

    # ---- ESCALATE phase: strong model re-scores the finalists ----
    threshold = entry.get("screen_counts", {}).get("threshold")
    remaining_ids = _unscored_vacancy_ids(entry.get("escalate_target_ids", []))
    remaining = [
        p for p in payloads if any(str(m) in remaining_ids for m in p.get("member_ids", []))
    ]
    if remaining_ids and not remaining:
        return "error", _corrupt_score_payload_msg(len(remaining_ids), "escalate")
    if remaining:
        # Every call that reaches this branch is a RESUME of the escalate gate
        # (the transition above returns its own gate without falling through
        # here) — re-begin so the live progress card reflects what's actually
        # still outstanding instead of a stale count left over from before.
        run_status.begin("score", len(remaining))
        return "gate", {
            "action": "score_vacancies",
            "count": len(remaining),
            "payload_path": str(VAC_PAYLOAD_PATH),
            "instructions": _vacancy_gate_text(remaining, strong_model, "escalate", threshold),
        }
    run_status.finish()
    c = entry.get("screen_counts", {})
    already_strong = c.get("already_strong") or 0
    skip_note = (
        f', {already_strong} already scored by "{strong_model}" and skipped'
        if already_strong
        else ""
    )
    return (
        "advance",
        f'two-pass scoring complete — screened {c.get("screened", "?")} with "{cheap_model}", '
        f'escalated {c.get("escalated", "?")} to "{strong_model}" (screen score >= '
        f"{c.get('threshold', '?')}{skip_note}), {c.get('kept_cheap', '?')} kept their cheap score",
    )


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


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _run_counts(state: dict) -> dict:
    """Key run-level counters, harvested from stage state + fetch_stats, for the
    durable pipeline_run row and the end-of-run health check. Best-effort — a
    missing piece is simply omitted (never raises)."""
    counts: dict = {}
    new = _safe_int(_read_fetch_stats().get("total_new"))
    if new is not None:
        counts["new_vacancies"] = new
    try:
        screened = _stage(state, "vacancy_scoring").get("screen_counts", {}).get("screened")
        if screened is not None:
            counts["scored"] = _safe_int(screened)
    except Exception:
        pass
    try:
        tids = _stage(state, "company_scoring").get("target_ids")
        if tids is not None:
            counts["companies_scored"] = len(tids)
    except Exception:
        pass
    return counts


def _record_history(state: dict, opts: Opts) -> None:
    """Persist the run's per-stage snapshot to pipeline_run (best-effort)."""
    try:
        import pipeline_run

        pipeline_run.record(state, boards=opts.job_boards, counts=_run_counts(state))
    except Exception:
        pass


def _fetch_scope_phrase(opts: Opts) -> str:
    """The fetch stage's start line, made scope-aware so the announcement reflects
    what THIS run actually pulls — the tier scope and the resolved board count —
    e.g. 'pulling new S-tier vacancies from your companies across 2 board(s)'."""
    boards = opts.job_boards
    if not boards:
        board_part = "your companies (boards off)"
    elif boards == "all":
        board_part = "your companies across all boards"
    else:
        n = len([b for b in boards.split(",") if b.strip()])
        board_part = f"your companies across {n} board(s)"
    tier_part = f"{opts.tier}-tier " if opts.tier else ""
    return f"pulling new {tier_part}vacancies from {board_part}"


def _announce_start(name: str, opts: Opts) -> None:
    """Print the stage's plain-language start line and stamp the live card so the
    active stage (with a sane, freshly-reset elapsed) is visible even before a
    long stage writes its own heartbeat (and a prior run's card can't linger).

    The fetch line is scope-aware (tier + board count); every other stage uses
    its static STAGE_ABOUT phrasing."""
    about = _fetch_scope_phrase(opts) if name == "fetch" else STAGE_ABOUT.get(name, "")
    print(f"\n▶ {name}: {about}" if about else f"\n▶ {name}", flush=True)
    try:
        import run_status

        run_status.mark(name, note=about or None)
    except Exception:
        pass


def drive(state: dict, opts: Opts, observe: bool = False) -> int:
    """Run AUTO stages back-to-back; stop at the first unmet GATE. Idempotent.

    ``observe`` turns on the reporting side-channels — stage start announcements,
    the live-card heartbeat, and the durable pipeline_run history. main() sets it
    for real runs; the stage-machine unit tests call drive() without it so the
    ordering/gate logic is exercised with no console noise or DB writes."""
    while True:
        idx = state["cursor"]
        if idx >= len(STAGE_ORDER):
            state["finished"] = True
            state["gate"] = None
            _save_state(state)
            if observe:
                _record_history(state, opts)
            return EXIT_DONE

        name = STAGE_ORDER[idx]
        entry = _stage(state, name)
        entry.setdefault("started_at", _now())
        entry["status"] = "running"
        _save_state(state)
        if observe:
            _announce_start(name, opts)
            _record_history(state, opts)

        try:
            kind, info = HANDLERS[name](state, entry, opts)
        except Exception as exc:  # a handler crash is a stage error, not a driver crash
            entry["status"] = "error"
            entry["note"] = f"{type(exc).__name__}: {exc}"
            _save_state(state)
            if observe:
                _record_history(state, opts)
            print(f"\n✗ Stage '{name}' crashed: {entry['note']}", file=sys.stderr, flush=True)
            return EXIT_ERROR

        if kind in ("advance", "skip"):
            entry["status"] = "done" if kind == "advance" else "skipped"
            entry["note"] = info
            entry["finished_at"] = _now()
            state["cursor"] = idx + 1
            state["gate"] = None
            _save_state(state)
            if observe:
                _record_history(state, opts)
                print(f"  ▸ {name} done ({_age_str(entry.get('started_at'))}): {info}", flush=True)
            else:
                print(f"  ▸ {name}: {info}", flush=True)
            continue

        if kind == "gate":
            entry["status"] = "blocked_gate"
            entry["emitted"] = True
            entry["gate"] = info
            state["gate"] = {"stage": name, "action": info.get("action")}
            _save_state(state)
            if observe:
                _record_history(state, opts)
            _emit_gate(name, info)
            return EXIT_GATE

        if kind == "error":
            entry["status"] = "error"
            entry["note"] = info
            _save_state(state)
            if observe:
                _record_history(state, opts)
            print(f"\n✗ Stage '{name}' failed: {info}", file=sys.stderr, flush=True)
            return EXIT_ERROR

        if kind == "abort":
            entry["status"] = "aborted"
            entry["note"] = info
            _save_state(state)
            if observe:
                _record_history(state, opts)
            print(f"\n✗ {info}", file=sys.stderr, flush=True)
            return EXIT_ABORT

        raise RuntimeError(f"stage {name} returned unknown result kind {kind!r}")


# ---------------------------------------------------------------------------
# Human summary
# ---------------------------------------------------------------------------


def _health_lines(state: dict, opts: Opts) -> list[str]:
    """The end-of-run expected-range check: an HONEST health signal that
    catches a silent parser breakage instead of shrugging at a suspicious count.

    Compares this run's key counter (new vacancies) against the recent historical
    range from pipeline_run (simple min/max over the last K FINISHED runs — no ML)
    and prints an explicit in-range / OUT-OF-RANGE verdict. Two hard rules fire
    regardless of history: 0 new while sources are enabled, and every source
    erroring (which the fetch handler still reports as rc=0 success) — both mean
    the run is not healthy even though nothing crashed. Best-effort: any failure
    yields no line rather than a misleading one."""
    lines: list[str] = []
    counts = _run_counts(state)
    new = counts.get("new_vacancies")
    if new is None:
        return lines  # fetch never ran (e.g. an early gate) — nothing to judge

    stats = _read_fetch_stats()
    errored = len(stats.get("errors", {}) or {})
    orgs = stats.get("orgs", {}) or {}
    boards_on = bool(opts.job_boards)
    all_sources_failed = errored > 0 and not any(int(d.get("live", 0) or 0) for d in orgs.values())

    try:
        import pipeline_run

        history = pipeline_run.recent_new_vacancies(limit=10, exclude_run_id=state.get("run_id"))
    except Exception:
        history = []

    if new == 0 and (boards_on or orgs):
        verdict = (
            "OUT OF RANGE — 0 new vacancies while sources are enabled; suspect a parser "
            "breakage (a source silently returning nothing)"
        )
    elif all_sources_failed:
        verdict = f"OUT OF RANGE — every source errored ({errored}); the run is not healthy"
    elif history:
        lo, hi = min(history), max(history)
        if new < lo or new > hi:
            verdict = (
                f"OUT OF RANGE — {new} new vs recent {lo}-{hi} over last {len(history)} run(s)"
            )
        else:
            verdict = f"in range — {new} new (recent {lo}-{hi} over last {len(history)} run(s))"
    else:
        verdict = f"no baseline yet — {new} new this run (history starts building now)"

    lines.append(f"new vacancies: {verdict}")
    return lines


def _print_summary(state: dict, opts: Opts) -> None:
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

    from product_language import t

    bar = "=" * 70
    print(f"\n{bar}")
    print("  " + t("summary_done"))
    print(bar)
    if new is not None:
        print("  • " + t("summary_new_vac", n=new))
    print("  • " + t("summary_companies", active=active, candidates=candidates, scored=cand_scored))
    print("  • " + t("summary_verdicts", scored_unseen=scored_unseen, liked=liked))
    print("  • " + t("summary_publish", note=publish_note))
    print("  • " + t("summary_review_hint"))
    for line in _health_lines(state, opts):
        mark = "⚠ " if "OUT OF RANGE" in line else "✓ "
        print(f"  {mark}run health — {line}")
    print(bar, flush=True)


def _boards_summary(opts: Opts) -> str:
    """Human phrasing of the boards feeding THIS run (from the resolved set)."""
    from product_language import t

    boards = opts.job_boards
    if not boards:
        return t("boards_summary_none")
    if boards == "all":
        return t("boards_summary_all")
    return boards


def _overload_advice() -> str | None:
    """A propose-only suggestion to reduce volume when the review backlog is large.

    Returns ``None`` when the backlog is under the threshold or the DB can't be
    read. Never applies anything — it only names the three real levers to turn
    down (STRATEGY guardrail 8: propose, never self-apply)."""
    try:
        backlog = _scored_unseen()
    except Exception:
        return None
    if backlog < OVERLOAD_BACKLOG:
        return None
    from product_language import t

    return (
        "  " + t("overload_head", backlog=backlog, threshold=OVERLOAD_BACKLOG) + "\n"
        "     " + t("overload_hint") + "\n"
        "       " + t("overload_lever_boards") + "\n"
        "       " + t("overload_lever_limit") + "\n"
        "       " + t("overload_lever_filters")
    )


def _print_run_banner(opts: Opts) -> None:
    """At run start, show WHERE today's volume comes from and the limits in effect.

    Wrapped so a banner failure (e.g. DB not migrated yet) can never abort the
    run — the real DB checks belong to the preflight stage, not here. DB-derived
    counts degrade to "?" instead of raising (STRATEGY goal 1)."""
    try:
        import settings
        from product_language import t
        from scoring_settings import max_per_run

        vol = settings.volume()
        try:
            active = _scalar("SELECT count(*) FROM company WHERE status='active'")
        except Exception:
            active = "?"

        bar = "=" * 70
        print(f"\n{bar}")
        print("  " + t("banner_title"))
        print(bar)
        print("  " + t("banner_active", n=active, cap=vol["max_active_companies"]))
        if opts.tier:
            print(f"  Scope: {opts.tier}-tier companies only (this run; stored tiers untouched)")
        print("  " + t("banner_boards", boards=_boards_summary(opts)))
        print("  " + t("banner_scoring", limit=max_per_run(), digest=vol["digest_size"]))
        advice = _overload_advice()
        if advice:
            print(advice)
        print(bar, flush=True)
    except Exception:
        # A banner is informational; never let it break the run.
        pass


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
        help=(
            "Extra boards for THIS run (e.g. '80k_hours,idealist'), unioned ON TOP of the "
            "persisted enabled set (scripts/sources.py). Use enable-board to make one stick."
        ),
    )
    p.add_argument(
        "--skip-boards",
        type=str,
        default=None,
        help=(
            "Boards to DROP from this run's effective set (e.g. 'linkedin'), subtracted after "
            "--boards / the persisted enabled set resolve. 'all' skips every board (companies "
            "still fetch); an unknown board name warns loudly. This run only — the persisted "
            "enabled set is never modified."
        ),
    )
    p.add_argument(
        "--tier",
        type=str.upper,
        default=None,
        choices=["S", "A", "B", "C"],
        help=(
            "Scope THIS run to companies of one importance tier (S/A/B/C); other tiers are "
            "skipped for this fetch. One run only — no stored company tier/status is changed."
        ),
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

    # A present-but-unreadable checkpoint must never be silently ignored: that
    # would let --resume lie ("No run to resume") and a bare run clobber it. Stop
    # and say so — unless --new, whose documented job is to discard prior state.
    if existing is None and not args.new and _state_file_corrupt():
        print(
            f"✗ The run checkpoint at {STATE_PATH} exists but is unreadable "
            "(corrupt JSON). Not touching it. Inspect or fix it, or discard it "
            "and start over with: python3 scripts/run_daily.py --new",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_ABORT

    fresh_run = False
    try:
        if args.new:
            opts = Opts(
                job_boards=_resolve_boards(args.boards, args.skip_boards),
                tier=args.tier,
                full_rescore=args.full_rescore,
                no_publish=args.no_publish,
            )
            state = _new_state(opts)
            fresh_run = True
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
            # A bare invocation auto-resumes (by design). But --boards /
            # --full-rescore cannot be honoured on a resume, and silently
            # ignoring them is the trap: a user who meant "fresh run with these
            # flags" would get a stale resume with none of them. Refuse to guess.
            ignored = _ignored_resume_flags(args)
            if ignored:
                print(
                    f"\n✗ An unfinished run ({existing.get('run_id')}) is on disk. A bare "
                    f"invocation would RESUME it and silently ignore {', '.join(ignored)} "
                    "(a resume replays the options frozen at the checkpoint, not the CLI's). "
                    "Refusing to guess your intent — choose one:\n"
                    "  • resume that run, dropping those flags:  python3 scripts/run_daily.py --resume\n"
                    "  • discard it and start fresh with them:   "
                    "python3 scripts/run_daily.py --new <your flags>",
                    file=sys.stderr,
                    flush=True,
                )
                return EXIT_ABORT
            _print_resume_banner(existing)
            state = existing
            opts = _opts_from_state(state)
            if args.no_publish:
                opts.no_publish = True
        else:
            opts = Opts(
                job_boards=_resolve_boards(args.boards, args.skip_boards),
                tier=args.tier,
                full_rescore=args.full_rescore,
                no_publish=args.no_publish,
            )
            state = _new_state(opts)
            fresh_run = True
    except BoardResolveError as exc:
        print(
            "\n✗ Run aborted: could not load the persisted board set because the "
            f"database is unreachable ({exc}). This is a genuine outage, not a "
            "fresh clone (a fresh clone falls back to the board override). Fix the "
            "DB, then start a fresh run.",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_ABORT

    # Bind everything downstream to THIS run: run_status heartbeats (written by
    # this process AND its stage subprocesses, which inherit the env) carry the
    # id, so run_card.py can tell a live card from a prior run's leftover.
    os.environ["JOBS_RUN_ID"] = str(state.get("run_id", ""))
    if fresh_run:
        _print_run_banner(opts)
        # Replace any previous run's heartbeat immediately, stamped with this run
        # id, so a card polled before the first long stage never shows stale state.
        try:
            import run_status

            run_status.mark("starting", note="preparing the run")
        except Exception:
            pass

    rc = drive(state, opts, observe=True)

    if rc == EXIT_DONE:
        _print_summary(state, opts)
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
