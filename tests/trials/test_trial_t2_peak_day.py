"""Trial T2 — "$20 plan, peak day".

Reproduces the first user-test failure where scoring cost was fine on a Max plan
but exhausted a $20 Claude Code plan on a heavy day. A peak day of 988 fresh
vacancies is replayed against a budget persona (Sonnet, shipped limits on). The
run must stay inside the spike-day safety cap, report an honest "scored X of Y",
and keep requests × prompt size under a per-run input-token budget.

The cap-cut MESSAGE and the model defaults are unit-tested in
``test_scoring_settings.py``; this trial is the peak-scale slice that also bounds
the token cost — the thing that decides whether a $20 plan survives a burst day.

The 988 vacancies come from a deterministic in-test factory (seeded, not a huge
checked-in JSON), and scoring never calls a model: ``score_vacancies.py --local``
only emits the per-vacancy prompts a subagent would score, so cost is measured as
request count and prompt size.
"""

from __future__ import annotations

import io
import json
import sys

import trial_harness as h

PEAK_DAY_VACANCIES = 988

# The shipped spike-day safety net: config/defaults.toml [volume] daily_scoring_limit
# (also scoring_settings.DEFAULT_MAX_PER_RUN). A budget persona with no VOLUME
# overrides inherits exactly this.
SAFETY_CAP = 150

# Per-run INPUT token budget for a peak day on a $20 (Sonnet) plan. Estimated as
# chars/4 across the capped request set. Generous headroom over a realistic run
# (~340k) so the check is not brittle, but far below an uncapped 988-request day
# (~2.2M) — so it fails loudly if the cap or the description truncation regresses.
PEAK_DAY_TOKEN_BUDGET = 900_000

# The scorer truncates a description at 8000 chars; a single user message is that
# plus the short template + org/title/location. This bound proves the truncation
# still holds (its removal is the other way a burst day blows the budget).
MAX_USER_MSG_CHARS = 8300

_ROLE_STEMS = (
    "Backend Engineer",
    "Platform Engineer",
    "Site Reliability Engineer",
    "Data Engineer",
    "Infrastructure Engineer",
    "Developer Advocate",
    "Staff Engineer",
    "Cloud Engineer",
)
_ORGS = tuple(f"Peakday Labs {i:02d}" for i in range(12))


def _peak_day_roles(n: int):
    """A deterministic day of ``n`` distinct (org, title) vacancies.

    Titles carry the running index so every role is globally unique (no dedup
    collapse) and none hit the universal-junk filter; descriptions vary in length
    so the token estimate reflects a realistic spread.
    """
    by_org: dict[str, list] = {org: [] for org in _ORGS}
    for i in range(n):
        org = _ORGS[i % len(_ORGS)]
        stem = _ROLE_STEMS[i % len(_ROLE_STEMS)]
        title = f"{stem} {i:04d}"
        body = "We build and operate distributed systems and developer tooling. "
        desc = body * (4 + (i % 9))  # ~260–850 chars
        by_org[org].append((title, desc))
    return by_org


def _run_local_scorer():
    """Run ``score_vacancies.cmd_local`` in-process; return (payloads, stderr)."""
    import score_vacancies as sv

    args = sv.build_parser().parse_args(["--local"])  # no --limit → the cap applies
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        sv.cmd_local(args)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return json.loads(out.getvalue()), err.getvalue()


def test_peak_day_stays_in_cap_and_budget(monkeypatch, tmp_path):
    dal = h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")

    saved = 0
    for org, roles in _peak_day_roles(PEAK_DAY_VACANCIES).items():
        saved += h.seed_roles(dal, org, roles)
    assert saved == PEAK_DAY_VACANCIES, "the seeded peak day must land intact"

    # Budget persona: cheap Sonnet strong model, shipped spike-day cap — both from
    # defaults, nothing configured. This is what a $20-plan user actually runs.
    import scoring_settings as ss

    assert ss.scoring_model() == "sonnet"
    assert ss.max_per_run() == SAFETY_CAP

    payloads, stderr = _run_local_scorer()

    # 1. The run fits inside the safety cap even though 988 are available.
    assert len(payloads) == SAFETY_CAP, "scoring must stop at the spike-day cap"

    # 2. "Scored X of Y" is shown, and the deferral is stated (nothing silent).
    assert f"Scoring {SAFETY_CAP} of {PEAK_DAY_VACANCIES}" in stderr
    assert f"Per-run cap reached ({SAFETY_CAP})" in stderr
    assert "Scoring model: sonnet" in stderr

    # 3. Requests × prompt size stay under the per-run input-token budget.
    sizes = [len(p["system_prompt"]) + len(p["user_msg"]) for p in payloads]
    assert max(len(p["user_msg"]) for p in payloads) <= MAX_USER_MSG_CHARS
    est_tokens = sum(sizes) // 4
    assert est_tokens <= PEAK_DAY_TOKEN_BUDGET, (
        f"peak-day input estimate {est_tokens} tokens exceeds the "
        f"per-run budget of {PEAK_DAY_TOKEN_BUDGET}"
    )
    # Worst-case bound too: requests × the largest prompt must clear the budget.
    assert len(payloads) * (max(sizes) // 4) <= PEAK_DAY_TOKEN_BUDGET
