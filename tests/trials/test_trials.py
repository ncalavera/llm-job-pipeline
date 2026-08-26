"""The persona trials — end-to-end reproductions of the real first-user-test
failures, run through the production loaders against a real profile fixture.

Absorbed, in numeric order, from test_trial_t1_engineer.py, test_trial_t2_peak_day.py,
test_trial_t3_walk_away.py, test_trial_t4_honest_demo.py, test_trial_t5_russian_user.py,
test_trial_t6_overflow.py. Shared harness helpers (persona swapping, module cache
invalidation, seeding) live in trial_harness.py; each trial's own docstring below
names the specific failure it guards.

Everything is offline: temp SQLite files, recorded/synthetic data, and request
counters — never a network call or a live model.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

import trial_harness as h


# --- from test_trial_t1_engineer.py ---
#
# Trial T1 — "engineer from scratch".
#
# Reproduces the first user-test failure where the maintainer's public-good job
# boards shipped as product defaults and steered an engineer tester into the wrong
# domain, with the scoring rubric carrying an effective-altruism worldview.
#
# The persona is a synthetic software engineer (``profile_engineer.md``) driven
# through the real loaders. The trial fails if the product regresses to that
# failure: any board auto-enabled on a fresh clone, an impact/EA board proposed to
# an engineer, or an EA worldview frame surviving in the rendered scoring prompt.
#
# Board-recommendation MECHANICS are unit-tested in ``test_profile_targeting.py``
# and the EA-free COMPANY prompt in ``test_company_scoring_profile_driven.py``;
# this trial is the persona-level integration that ties the real fixture file to
# the shipped board catalogue and the rendered prompt.

# Boards whose audience is purely effective-altruism / humanitarian / nonprofit,
# with no engineering overlap. An engineer must never be steered onto these.
IMPACT_ONLY_BOARDS = {
    "80k_hours",
    "reliefweb",
    "impactpool",
    "idealist",
    "consultants_for_impact",
}


def test_fresh_clone_auto_enables_no_board(monkeypatch, tmp_path):
    """A first-time clone (migrated, empty) fetches nothing until the user opts in."""
    dal = h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")

    assert dal.get_enabled_boards() == [], "no board may be enabled on a fresh clone"
    assert dal.get_company_fitness_map() == {}, "no companies tracked before onboarding"

    import config

    assert config.JOB_BOARDS == {}, "JOB_BOARDS unset must fetch zero boards"


def test_engineer_recommendations_skip_impact_only_boards(monkeypatch, tmp_path):
    """Board proposals come from the engineer's own profile, not an impact default."""
    h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")

    import profile_targeting as pt
    import prompts

    ids = [r["id"] for r in pt.recommend_boards(prompts._load_user_profile())]

    assert "linkedin" in ids, "the general board (queries from the profile) always fits"
    assert "arbeitnow" in ids, "an engineering board should match an engineer"
    leaked = IMPACT_ONLY_BOARDS.intersection(ids)
    assert not leaked, f"impact/EA boards proposed to an engineer: {sorted(leaked)}"


def test_half_edited_engineer_profile_keeps_impact_boards_off(monkeypatch, tmp_path):
    """A profile only HALF-edited from the example template must not leak boards.

    The clean ``profile_engineer.md`` above proves the mechanism when the user
    deletes all the guidance. But a real first user rarely does: they fill in
    name / experience / roles and leave the shipped scaffolding — the bracketed
    "Domain preferences" placeholder (which lists *public policy*, healthcare, …)
    and the ``e.g. "…Operations Manager…"`` sample bullets. Left in, those
    example words get keyword-matched as if the engineer had chosen impact /
    nonprofit work, so impact-only boards get recommended — the regression.

    This is the persona-level failure the clean fixture could never catch — it
    carries no scaffolding to leak. Assert the engineer still gets the general
    board plus a real engineering board, and NONE of the impact-only boards.
    """
    h.use_persona(monkeypatch, profile="profile_engineer_half_edited.md", db_path=tmp_path / "db")

    import profile_targeting as pt
    import prompts

    ids = [r["id"] for r in pt.recommend_boards(prompts._load_user_profile())]

    assert "linkedin" in ids, "the general board always fits"
    assert "arbeitnow" in ids, "the engineer's real roles still match an engineering board"
    leaked = IMPACT_ONLY_BOARDS.intersection(ids)
    assert not leaked, f"leftover example scaffolding recommended impact boards: {sorted(leaked)}"


def test_engineer_scoring_prompt_has_no_ea_frame(monkeypatch, tmp_path):
    """The rendered vacancy prompt is built from the engineer's field, EA-free."""
    h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")

    import prompts
    from test_no_hardcoded_data import WORLDVIEW_TOKEN

    vacancy_prompt = prompts.VACANCY_SCORING_PROMPT
    company_prompt = prompts.COMPANY_SCORING_PROMPT

    # Profile-driven, not sector-fixed: the engineer's own domain rides in, the
    # unrelated persona's clinical vocabulary does not.
    assert "developer tools" in vacancy_prompt
    assert "developer tools" in company_prompt
    assert "Clinical Nurse Specialist" not in vacancy_prompt

    worldview_hits = WORLDVIEW_TOKEN.findall(vacancy_prompt)
    assert not worldview_hits, (
        f"EA/worldview frame leaked into the vacancy prompt: {worldview_hits}"
    )


def test_prompt_tracks_a_third_disjoint_field(monkeypatch, tmp_path):
    """A designer's prompt is built from design, not the engineer's or medic's field.

    The same render path must resolve THREE independent fields — engineer above,
    a product designer here, and (in a sibling guard) a nurse — so "scored against
    the candidate's own field" is not a two-way special case. This is where the
    designer persona genuinely earns its keep: board targeting cannot cleanly
    separate a designer from an engineer (they share the remote-software boards),
    but the rendered rubric does.
    """
    h.use_persona(monkeypatch, profile="profile_designer.md", db_path=tmp_path / "db")

    import prompts

    vacancy_prompt = prompts.VACANCY_SCORING_PROMPT

    assert "product design" in vacancy_prompt and "design systems" in vacancy_prompt
    assert "developer tools" not in vacancy_prompt  # not the engineer's field
    assert "distributed systems" not in vacancy_prompt
    assert "Clinical Nurse Specialist" not in vacancy_prompt  # not the medic's field


# --- from test_trial_t2_peak_day.py ---
#
# Trial T2 — "$20 plan, peak day".
#
# Reproduces the first user-test failure where scoring cost was fine on a Max plan
# but exhausted a $20 Claude Code plan on a heavy day. A peak day of 988 fresh
# vacancies is replayed against a budget persona (Sonnet, shipped limits on). The
# run must stay inside the spike-day safety cap, report an honest "scored X of Y",
# and keep requests × prompt size under a per-run input-token budget.
#
# The cap-cut MESSAGE and the model defaults are unit-tested in
# ``test_scoring_settings.py``; this trial is the peak-scale slice that also bounds
# the token cost — the thing that decides whether a $20 plan survives a burst day.
#
# The 988 vacancies come from a deterministic in-test factory (seeded, not a huge
# checked-in JSON), and scoring never calls a model: ``score_vacancies.py --local``
# only emits the per-vacancy prompts a subagent would score, so cost is measured as
# request count and prompt size.
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


# --- from test_trial_t3_walk_away.py ---
#
# Trial T3 — "launch and walk away".
#
# Reproduces the first user-test failure where operating the tool required
# stage-order knowledge that lived only in the maintainer's head. The persona
# starts the daily cycle and leaves: the driver must own the stage order, ask
# nothing mid-pipeline when there is nothing to judge, and end on a summary that
# explains every number in words.
#
# The gate/resume state machine is unit-tested in ``test_run_daily.py``; this trial
# adds the walk-away guarantees — no question when the judgment surface is empty,
# and a self-explanatory end-of-run summary composed against a real seeded DB.
def test_stage_order_and_handlers_are_owned_by_the_driver(monkeypatch, tmp_path):
    """The canonical order lives in code, and every stage has a handler."""
    h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")
    import run_daily as rd

    # One source of truth for "what happens when" — not a runbook, not memory.
    assert rd.STAGE_ORDER == [
        "validate_profile",
        "preflight",
        "onboarding",
        "learning_review",
        "fetch",
        "enrich",
        "filter",
        "company_scoring",
        "vacancy_scoring",
        "verdicts",
        "publish",
    ]
    # Every stage is executable; no orphan stage the operator must drive by hand.
    assert set(rd.HANDLERS) == set(rd.STAGE_ORDER)


def test_empty_judgment_surface_asks_nothing(monkeypatch, tmp_path):
    """With nothing to decide, the judgment stages advance/skip — no gate emitted."""
    dal = h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")
    # A tracked company but zero scored/liked roles: an established, quiet day.
    dal.ensure_company("Acme Cloud", status="active")
    dal.get_conn().commit()

    import run_daily as rd

    state = rd._new_state(rd.Opts())
    state["first_run"] = False  # companies present — not the onboarding case

    for stage in ("learning_review", "verdicts"):
        action, _note = rd.HANDLERS[stage](state, rd._stage(state, stage), rd.Opts())
        assert action != "gate", f"{stage} asked a question with nothing to decide"

    allowed, reasons = rd.check_publish_gate(state, fetch_stats={"orgs": {}})
    assert allowed and reasons == [], "a clean run must be allowed to publish silently"


def test_final_summary_explains_every_number(monkeypatch, tmp_path, capsys):
    """The one end screen is self-explanatory: numbers carried by words, none '?'."""
    dal = h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")
    h.seed_roles(dal, "Acme Cloud", [("Staff Engineer", "Own the platform. " * 20)])
    h.seed_roles(dal, "Globex Data", [("Data Engineer", "Build the pipeline. " * 20)])
    ids = list(dal.load_vacancies().keys())
    dal.update_vacancy_fields(ids[0], llm_score=78, status="unseen")
    dal.update_vacancy_fields(ids[1], status="liked")
    dal.get_conn().commit()

    import run_daily as rd

    monkeypatch.setattr(rd, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(rd, "FETCH_STATS_PATH", tmp_path / "fetch_stats.json")
    (tmp_path / "fetch_stats.json").write_text(
        json.dumps({"total_new": 6, "orgs": {}}), encoding="utf-8"
    )

    state = rd._new_state(rd.Opts())
    rd._stage(state, "publish")["note"] = "published to public/data.js"
    rd._print_summary(state, rd.Opts())

    out = capsys.readouterr().out
    # Every figure arrives with the word that explains it (English persona).
    assert "new vacancies saved this run" in out
    assert "active companies" in out
    assert "await your verdict" in out
    assert "publish:" in out
    assert "/jobs-review" in out  # what to do next, spelled out
    # No DB read degraded to the "?" placeholder — the numbers are real.
    assert "?" not in out


# --- from test_trial_t4_honest_demo.py ---
#
# Trial T4 — "honest demo".
#
# Reproduces the first user-test failure where the SQLite-vs-Supabase split read as
# infrastructure trivia with no guidance. The persona tries the tool with no
# ``.env`` at all: simple mode must work end to end, name itself honestly, and —
# when they later move to Supabase — fail with the message the docs promised.
#
# Most of this trial is a manual protocol (actually standing up Supabase). The
# cheap slices are automated here: simple mode works with no ``.env``, and the
# messages the code emits are the ones INSTALL-EASY.md documents. The loader
# mechanics themselves are unit-tested in ``test_env_loader.py`` and
# ``test_simple_mode_no_psycopg2.py``; this trial ties those messages to the docs.
INSTALL_EASY = os.path.join(h.REPO_ROOT, "INSTALL-EASY.md")
DB_BACKEND_SRC = os.path.join(h.SCRIPTS, "db_backend.py")

# The exact strings a user reads. Both must appear in the code that emits them AND
# in the doc that promises them — a drift on either side is a T4 failure.
SQLITE_BANNER = "Backend: local SQLite"
PSYCOPG2_MESSAGE = "psycopg2 is not installed"


def test_simple_mode_works_with_no_env(monkeypatch, tmp_path):
    """No .env, no Supabase, empty local DB — the demo actually runs and stores data."""
    dal = h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")

    import db_backend

    assert db_backend.IS_SQLITE, "with no SUPABASE_DB_URL the demo must stay on SQLite"

    saved = h.seed_roles(dal, "Demo Co", [("Platform Engineer", "Run the platform. " * 12)])
    assert saved == 1
    loaded = dal.load_vacancies()
    assert any(v["org"] == "Demo Co" for v in loaded.values()), "the demo round-trips a vacancy"


def test_backend_banner_is_the_documented_sqlite_message(monkeypatch, tmp_path):
    """The banner names SQLite honestly and matches what INSTALL-EASY.md shows."""
    h.use_persona(
        monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db", migrate=False
    )

    import db_backend

    buf = io.StringIO()
    db_backend.print_backend_banner(buf)
    banner = buf.getvalue()

    assert SQLITE_BANNER in banner
    assert "Postgres" not in banner and "Supabase" not in banner, "no false parity promise"
    assert "WARNING" not in banner, "a plain simple-mode run has nothing to warn about"

    doc = _read(INSTALL_EASY)
    assert SQLITE_BANNER in doc, "the demo banner the user sees must be documented verbatim"


def test_supabase_transition_failure_message_is_documented():
    """The move-to-Supabase failure message the docs promise is the one the code emits."""
    doc = _read(INSTALL_EASY)
    src = _read(DB_BACKEND_SRC)

    assert PSYCOPG2_MESSAGE in src, "the code must emit the documented psycopg2 message"
    assert PSYCOPG2_MESSAGE in doc, "INSTALL-EASY.md must promise the message the code emits"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --- from test_trial_t5_russian_user.py ---
#
# Trial T5 — "Russian user".
#
# A user who picks Russian at onboarding must get the whole product in Russian from
# that single choice: the agent's replies, the run banner + summary, the Telegram
# digest, and the dashboard chrome. One profile key drives all of them.
#
# The resolver order and per-surface translation are unit-tested in
# ``test_product_language.py``; the public English shell staying Cyrillic-free is
# in ``test_dashboard_no_cyrillic.py``. This trial is the persona integration: one
# ``## OUTPUT_LANGUAGE`` choice flips every named surface at once, and the Russian
# product has no missing translation that would silently fall back to English.
RU_PROFILE = "## OUTPUT_LANGUAGE\n\nRussian\n\n## TARGET_ROLES\n\n- Product Designer, UX Designer\n"

# Load-bearing surfaces that MUST read as Russian (not an accidental English copy).
_MUST_TRANSLATE = ("summary_done", "banner_title", "digest_header", "tab_today")


def test_one_choice_switches_all_surfaces_to_russian(monkeypatch, tmp_path):
    profile = tmp_path / "profile_ru.md"
    profile.write_text(RU_PROFILE, encoding="utf-8")
    h.swap_profile(monkeypatch, str(profile))

    import i18n
    import product_language as pl

    # The one resolved product language — drives the agent's replies in the runbooks.
    assert pl.resolve() == "ru"

    # Run banner + summary (run_daily.py reads these through product_language.t).
    assert pl.t("banner_title") == "/jobs-new — объём на сегодня"
    assert pl.t("summary_done") == "✓ /jobs-new завершён"

    # Telegram digest copy.
    assert "Дайджест" in pl.t("digest_header")

    # Dashboard chrome (baked into data.js from the same table).
    assert i18n.strings("ru")["tab_today"] == "Сегодня"


def test_russian_product_has_no_missing_translation(monkeypatch, tmp_path):
    """Every English key exists in Russian, so no surface silently reverts to English."""
    import i18n

    en_keys = set(i18n.STRINGS["en"])
    ru_keys = set(i18n.STRINGS["ru"])
    missing = en_keys - ru_keys
    assert not missing, f"Russian product is missing translations for: {sorted(missing)}"

    # The load-bearing surfaces are genuinely translated, not English left in place.
    for key in _MUST_TRANSLATE:
        assert i18n.STRINGS["ru"][key] != i18n.STRINGS["en"][key], (
            f"{key} not translated to Russian"
        )


# --- from test_trial_t6_overflow.py ---
#
# Trial T6 — "overflow".
#
# Reproduces the first user-test failure where the volume of fetched content
# overwhelmed the tester with no obvious levers to shrink it. A wide profile with
# many boards must still leave the volume levers visible, keep the "Today" cockpit
# bounded to the loudest tier, and — once the review backlog builds — surface a
# learning screen that proposes cuts (never applies them).
#
# The banner volumes and the overload lever copy are unit-tested in
# ``test_volume_settings.py``; board recommendation in ``test_profile_targeting.py``.
# This trial is the persona integration: a wide impact profile fans out to many
# boards, the run banner shows the levers AND the cut advice together under an
# overflow backlog, and "Today" stays gated strictly above the catalog floor.
# The pure-impact boards an engineer never gets (see trial T1) — an ops/impact
# profile SHOULD get them. Same machinery, opposite persona: proof it is
# profile-driven, not a fixed default in either direction.
IMPACT_BOARDS = {"idealist", "impactpool", "reliefweb"}

TODAY_JS = os.path.join(h.REPO_ROOT, "public", "modules", "today.js")


def test_wide_impact_profile_fans_out_to_impact_boards(monkeypatch):
    """A broad ops/impact profile proposes many boards, including the impact ones."""
    h.swap_profile(monkeypatch, "profile_ops_impact.md")

    import profile_targeting as pt
    import prompts

    recs = pt.recommend_boards(prompts._load_user_profile())
    ids = {r["id"] for r in recs}

    assert "linkedin" in ids
    assert IMPACT_BOARDS.issubset(ids), f"impact boards missing for an impact profile: {ids}"
    assert len(recs) >= 6, "a wide profile should fan out to many boards"


def test_run_banner_shows_levers_and_cut_advice_under_overflow(monkeypatch):
    """Under an overflow backlog, the banner shows the volume dials AND the cuts."""
    h.swap_profile(monkeypatch, "profile_engineer.md")

    import run_daily as rd

    # Overflow day: many companies tracked, backlog well past the overload proxy.
    monkeypatch.setattr(rd, "_scalar", lambda *a, **k: 250)
    monkeypatch.setattr(rd, "_scored_unseen", lambda: rd.OVERLOAD_BACKLOG + 70)

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rd._print_run_banner(rd.Opts(job_boards="all"))
    out = buf.getvalue()

    # The volume dials are on screen.
    assert "today's volume" in out
    assert "150" in out and "Digest size: 5" in out
    assert "all defined boards" in out

    # The cut advice fires and names the three real levers — suggestion only.
    assert "Review backlog" in out
    assert "disable-board" in out
    assert "daily_scoring_limit" in out
    assert "HARD_FILTERS" in out
    assert "nothing changes unless you do it" in out


def test_today_cockpit_is_bounded_above_the_catalog_floor(monkeypatch, tmp_path):
    """ "Today" surfaces only high-signal unseen roles, so an overflow day can't dump into it.

    The Today rework replaced the single "New 70+" list with score-gated
    Closing-soon and Don't-rot strips; the gate for unseen roles now lives in
    derive.js as APPLYABLE_MIN_SCORE (every other block is bounded by an explicit
    user verdict, not the flood). The bound must still sit above the catalog
    floor and at/above the protect line.
    """
    h.use_persona(
        monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db", migrate=False
    )

    derive_js = os.path.join(h.REPO_ROOT, "public", "modules", "derive.js")
    with open(derive_js, encoding="utf-8") as fh:
        derive_src = fh.read()
    m = re.search(r"APPLYABLE_MIN_SCORE\s*=\s*(\d+)", derive_src)
    assert m, "derive.js must define the APPLYABLE_MIN_SCORE gate for the cockpit"
    unseen_gate = int(m.group(1))

    import config

    # The unseen-role gate sits above the catalog floor and at/above the protect
    # line — the cockpit is a high-signal subset, never the full flood.
    assert unseen_gate > config.CATALOG_MIN_SCORE
    assert unseen_gate >= config.PROTECT_SCORE
