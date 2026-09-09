"""Cheap relevance screen before paid company enrichment.

Three layers, cheapest first:

1. Pure decision parsing — a malformed / ambiguous model response never DROPs
   (a screen must fail safe to KEEP).
2. Already-tracked dedup — a fresh candidate that variant-matches a company we
   already track is dropped without spending an LLM call, via the SAME matcher
   the save layer uses.
3. The LLM screen (mocked) — drops a CLEAR mismatch with a reason, keeps a
   plausible fit, keeps a borderline/unfamiliar name; the mock proves paid
   enrichment is never asked about a dropped or duplicate company; a second
   profile flips the outcome (the screen is profile-driven, not hardcoded).

Plus a SQLite integration test proving a dropped candidate is set inactive and
therefore no longer selected for enrichment/scoring.

Fully offline: every LLM call is a stub. No Firecrawl / Exa / Anthropic calls.

Absorbed tests/test_candidate_enrichment_chain.py — the only coverage of
run_daily's 5-step company-scoring orchestration order (junk filter -> find
websites -> scrape -> collect evidence -> WANT-score), the FIRECRAWL-unset
skip, the per-run-cap propagation into three subprocess calls, and the money-
valve screen-crash withholding.
"""

import importlib
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import screen_candidates as sc  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ENGINEER = FIXTURES / "profile_engineer.md"
MEDIC = FIXTURES / "profile_medic.md"


# ---------------------------------------------------------------------------
# 1. parse_screen_verdict — fail-safe to KEEP
# ---------------------------------------------------------------------------


def test_parse_screen_verdict_explicit_drop():
    d = sc.parse_screen_verdict('{"keep": false, "reason": "staffing agency"}')
    assert d["keep"] is False
    assert d["reason"] == "staffing agency"


def test_parse_screen_verdict_explicit_keep():
    d = sc.parse_screen_verdict('{"keep": true, "reason": "plausible fit"}')
    assert d["keep"] is True


def test_parse_screen_verdict_tolerates_fenced_json():
    d = sc.parse_screen_verdict('```json\n{"keep": false, "reason": "car dealership"}\n```')
    assert d["keep"] is False and d["reason"] == "car dealership"


def test_parse_screen_verdict_malformed_keeps():
    # A screen must NOT drop on an unreadable response — that would silently hide
    # a real employer.
    d = sc.parse_screen_verdict("the model rambled without any JSON")
    assert d["keep"] is True


def test_parse_screen_verdict_missing_keep_defaults_to_keep():
    d = sc.parse_screen_verdict('{"reason": "no verdict field"}')
    assert d["keep"] is True


# ---------------------------------------------------------------------------
# 2. dedupe_tracked — already-tracked skip via the save-layer matcher
# ---------------------------------------------------------------------------


def test_dedupe_drops_tracked_variant():
    candidates = [
        {"id": "1", "canonical_name": "Save the Children International"},
        {"id": "2", "canonical_name": "Some Brand New Org"},
    ]
    to_screen, dups = sc.dedupe_tracked(candidates, tracked_names=["Save the Children"])
    assert [r["canonical_name"] for r in to_screen] == ["Some Brand New Org"]
    assert len(dups) == 1
    row, reason, kind = dups[0]
    assert row["id"] == "1"
    assert "already tracked as Save the Children" in reason
    assert kind == "dup"


def test_dedupe_keeps_non_matching_names():
    candidates = [{"id": "9", "canonical_name": "Distinct New Foundation"}]
    to_screen, dups = sc.dedupe_tracked(candidates, tracked_names=["Global Partners"])
    assert len(to_screen) == 1 and not dups


# ---------------------------------------------------------------------------
# 3. LLM screen (mocked) — drop clear, keep plausible, keep borderline
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Records every (system, user) call and answers by keyword in the name."""

    def __init__(self, drop_substrings):
        self.drop_substrings = drop_substrings
        self.seen_names: list[str] = []

    def __call__(self, system: str, user: str) -> str:
        self.seen_names.append(user)
        if any(s in user for s in self.drop_substrings):
            return '{"keep": false, "reason": "clear mismatch"}'
        return '{"keep": true, "reason": "plausible fit"}'


def test_screen_drops_clear_mismatch_keeps_others():
    candidates = [
        {"id": "1", "canonical_name": "Acme Staffing Agency", "description": "we place temps"},
        {"id": "2", "canonical_name": "Malaria Research Institute", "description": ""},
        {"id": "3", "canonical_name": "Obscure Unknown Org", "description": ""},
    ]
    llm = _FakeLLM(drop_substrings=["Staffing"])
    decisions = sc.screen_candidates(candidates, "SYSTEM", llm)
    by_id = {d["row"]["id"]: d for d in decisions}
    assert by_id["1"]["keep"] is False and by_id["1"]["reason"]
    assert by_id["2"]["keep"] is True  # plausible fit
    assert by_id["3"]["keep"] is True  # borderline / unknown → kept


def test_screen_error_fails_safe_to_keep():
    def boom(system, user):
        raise RuntimeError("api down")

    decisions = sc.screen_candidates(
        [{"id": "1", "canonical_name": "X", "description": ""}], "SYSTEM", boom
    )
    assert decisions[0]["keep"] is True


def test_snippet_reaches_the_model():
    seen = {}

    def spy(system, user):
        seen["user"] = user
        return '{"keep": true, "reason": "ok"}'

    sc.screen_candidates(
        [{"id": "1", "canonical_name": "Foo", "description": "biotech nonprofit"}], "SYS", spy
    )
    assert "Foo" in seen["user"] and "biotech nonprofit" in seen["user"]


# ---------------------------------------------------------------------------
# run_screen — dedup + screen; enrichment never asked about drops
# ---------------------------------------------------------------------------


def test_run_screen_excludes_dup_and_dropped_from_llm(monkeypatch):
    candidates = [
        {"id": "1", "canonical_name": "Save the Children International", "description": ""},
        {"id": "2", "canonical_name": "Payday Loans LLC", "description": "short-term lending"},
        {"id": "3", "canonical_name": "Clean Water Fund", "description": ""},
    ]
    monkeypatch.setattr(sc, "load_fresh_candidates", lambda conn, limit=0: candidates)
    monkeypatch.setattr(sc, "load_tracked_names", lambda conn: ["Save the Children"])
    monkeypatch.setattr(sc, "screen_system_prompt", lambda: "SYSTEM")

    llm = _FakeLLM(drop_substrings=["Payday Loans"])
    summary = sc.run_screen(object(), llm, limit=0)

    kept = {r["canonical_name"] for r, _ in summary["keep"]}
    dropped = {r["canonical_name"]: kind for r, _, kind in summary["drop"]}
    assert kept == {"Clean Water Fund"}
    assert dropped == {"Save the Children International": "dup", "Payday Loans LLC": "llm"}
    # The already-tracked duplicate was dropped by dedup — the paid LLM was never
    # asked about it (enrichment cost avoided before any spend).
    assert not any("Save the Children" in name for name in llm.seen_names)


def test_run_screen_no_credentials_defers_to_pending(monkeypatch):
    """No direct API key: nothing is dropped by the LLM cut — the remaining
    candidates go to `pending` (subagent payloads) and stay kept until decisions
    come back via --save."""
    candidates = [
        {"id": "1", "canonical_name": "Anything At All", "description": ""},
        {"id": "2", "canonical_name": "Another Org", "description": ""},
    ]
    monkeypatch.setattr(sc, "load_fresh_candidates", lambda conn, limit=0: candidates)
    monkeypatch.setattr(sc, "load_tracked_names", lambda conn: [])

    summary = sc.run_screen(object(), call_llm=None, limit=0)
    assert not summary["drop"]
    assert len(summary["pending"]) == 2
    assert summary["llm_ran"] is False
    assert summary["screened"] == 0  # nothing was LLM-screened here


# ---------------------------------------------------------------------------
# 4. Profile-driven — a different profile flips the same candidate's outcome
# ---------------------------------------------------------------------------


def test_screen_system_prompt_injects_active_profile(monkeypatch):
    """The rendered screen instruction carries the profile's own targeting, not a
    hardcoded company-type list."""
    monkeypatch.setenv("USER_PROFILE_PATH", str(ENGINEER))
    eng = sc.screen_system_prompt()
    monkeypatch.setenv("USER_PROFILE_PATH", str(MEDIC))
    med = sc.screen_system_prompt()
    assert eng != med
    assert "developer tools" in eng and "developer tools" not in med
    assert "Clinical Nurse Specialist" in med and "Clinical Nurse Specialist" not in eng


def test_outcome_follows_the_profile(monkeypatch):
    """A profile-aware fake model drops a company for the engineer profile and
    keeps the same company for the medic profile — proving the decision is driven
    by the profile that reaches the prompt, not a fixed rule."""

    def profile_aware_llm(system, user):
        # Drop only when the ACTIVE profile is the engineer's (its rendered
        # rubric names developer tools); keep otherwise.
        if "developer tools" in system:
            return '{"keep": false, "reason": "outside engineer field"}'
        return '{"keep": true, "reason": "fits this profile"}'

    company = [{"id": "1", "canonical_name": "Community Health Clinic", "description": ""}]

    monkeypatch.setenv("USER_PROFILE_PATH", str(ENGINEER))
    d_eng = sc.screen_candidates(company, sc.screen_system_prompt(), profile_aware_llm)
    assert d_eng[0]["keep"] is False

    monkeypatch.setenv("USER_PROFILE_PATH", str(MEDIC))
    d_med = sc.screen_candidates(company, sc.screen_system_prompt(), profile_aware_llm)
    assert d_med[0]["keep"] is True


# ---------------------------------------------------------------------------
# 5. SQLite integration — a dropped candidate is set inactive and no longer
#    selected for enrichment/scoring.
# ---------------------------------------------------------------------------


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"
    import database_supabase as db  # noqa: F401  (initializes the schema)

    yield db_backend
    db_backend.close_conn()


def _insert_company(conn, name, *, status="candidate", alignment_score=None, description=""):
    cid = str(uuid.uuid4())
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO company (id, canonical_name, status, alignment_score, description) "
        "VALUES (%s, %s, %s, %s, %s)",
        (cid, name, status, alignment_score, description),
    )
    conn.commit()
    cur.close()
    return cid


def _insert_earning_vacancy(conn, company_id, *, llm_score=90, status="unseen"):
    """Give a candidate company an EARNING vacancy so it passes the vacancy-first
    gate (R3): load_fresh_candidates selects only candidates with a vacancy scored
    at/above the paid floor or one the user liked. Without this a fresh candidate
    is a free name-only row that never reaches the screen."""
    vid = str(uuid.uuid4())
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, first_seen, last_seen, "
        "status, llm_score) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (vid, vid, company_id, "Some Role", "2026-01-01", "2026-01-01", status, llm_score),
    )
    conn.commit()
    cur.close()
    return vid


def test_apply_drops_sets_inactive_and_removes_from_enrichment(dal):
    conn = dal.get_conn()
    # A tracked (already active) company + two fresh candidates: one a clear
    # mismatch, one a plausible fit.
    _insert_company(conn, "Clean Water Fund", status="active", alignment_score=80)
    acme = _insert_company(conn, "Acme Staffing Agency", description="temp placement")
    ocean = _insert_company(conn, "Ocean Cleanup Initiative")
    _insert_earning_vacancy(conn, acme)  # both candidates have earned enrichment
    _insert_earning_vacancy(conn, ocean)

    llm = _FakeLLM(drop_substrings=["Staffing"])
    summary = sc.run_screen(conn, llm, limit=0)
    sc.apply_drops(conn, summary["drop"])

    # Only the plausible fit remains selectable for enrichment/scoring.
    remaining = {r["canonical_name"] for r in sc.load_fresh_candidates(conn)}
    assert remaining == {"Ocean Cleanup Initiative"}

    # The dropped candidate is inactive with a screen: reason (auditable).
    cur = conn.cursor()
    cur.execute(
        "SELECT status, status_reason FROM company WHERE canonical_name = %s",
        ("Acme Staffing Agency",),
    )
    status, reason = cur.fetchone()
    cur.close()
    assert status == "inactive"
    assert reason.startswith("screen:")


def test_load_tracked_names_excludes_fresh_candidates(dal):
    conn = dal.get_conn()
    _insert_company(conn, "Active Org", status="active", alignment_score=70)
    _insert_company(conn, "Scored Candidate", status="candidate", alignment_score=42)
    _insert_company(conn, "Fresh Candidate", status="candidate")

    tracked = set(sc.load_tracked_names(conn))
    assert "Active Org" in tracked
    assert "Scored Candidate" in tracked  # already scored → not a fresh candidate
    assert "Fresh Candidate" not in tracked


def test_load_fresh_candidates_gates_on_earning_vacancy(dal):
    """Vacancy-first gate (R3): a candidate is selected for the paid chain ONLY
    when a vacancy has earned it — scored at/above the floor, or liked."""
    conn = dal.get_conn()
    none_id = _insert_company(conn, "No Vacancy Org")
    low_id = _insert_company(conn, "Low Score Org")
    edge_id = _insert_company(conn, "Edge Score Org")
    liked_id = _insert_company(conn, "Liked Low Org")
    _insert_earning_vacancy(conn, low_id, llm_score=59)  # below floor
    _insert_earning_vacancy(conn, edge_id, llm_score=60)  # exactly the floor
    _insert_earning_vacancy(conn, liked_id, llm_score=10, status="liked")  # liked wins

    selected = {r["canonical_name"] for r in sc.load_fresh_candidates(conn, min_vacancy_score=60)}
    assert selected == {"Edge Score Org", "Liked Low Org"}
    assert "No Vacancy Org" not in selected  # a stranger with no earning role waits
    assert "Low Score Org" not in selected


def test_count_unearned_candidates(dal):
    conn = dal.get_conn()
    earned = _insert_company(conn, "Earned Org")
    _insert_company(conn, "Waiting Org A")
    _insert_company(conn, "Waiting Org B")
    _insert_earning_vacancy(conn, earned, llm_score=80)

    assert sc.count_unearned_candidates(conn, min_vacancy_score=60) == 2


def test_load_fresh_candidates_degrades_when_description_missing(dal, capsys):
    """Schema drift: a live DB missing company.description must degrade to
    name-only selection with a warning, never crash the whole valve (KTD3)."""
    conn = dal.get_conn()
    cid = _insert_company(conn, "Drifted Org", description="")
    _insert_earning_vacancy(conn, cid, llm_score=90)
    cur = conn.cursor()
    cur.execute("ALTER TABLE company DROP COLUMN description")
    conn.commit()
    cur.close()

    rows = sc.load_fresh_candidates(conn, min_vacancy_score=60)
    assert {r["canonical_name"] for r in rows} == {"Drifted Org"}
    assert rows[0]["description"] == ""  # blank snippet, name-only screening
    assert "description unavailable" in capsys.readouterr().out


def test_dup_and_llm_drops_get_distinct_prefixes(dal):
    """Dedup drops are filterable apart from LLM drops: 'screen-dup:' vs 'screen:'."""
    conn = dal.get_conn()
    _insert_company(conn, "Save the Children", status="active", alignment_score=75)
    dup = _insert_company(conn, "Save the Children International")  # dup of tracked
    acme = _insert_company(conn, "Acme Staffing Agency")  # LLM clear mismatch
    _insert_earning_vacancy(conn, dup)
    _insert_earning_vacancy(conn, acme)

    llm = _FakeLLM(drop_substrings=["Staffing"])
    summary = sc.run_screen(conn, llm, limit=0)
    sc.apply_drops(conn, summary["drop"])

    cur = conn.cursor()
    cur.execute("SELECT canonical_name, status_reason FROM company WHERE status = 'inactive'")
    reasons = dict(cur.fetchall())
    cur.close()
    assert reasons["Save the Children International"].startswith("screen-dup:")
    assert reasons["Acme Staffing Agency"].startswith("screen:")


# ---------------------------------------------------------------------------
# 6. No-API-key path — pending payloads + --save decisions (the --local-style
#    subagent protocol run_daily gates on)
# ---------------------------------------------------------------------------


def test_build_screen_payloads_shape():
    payloads = sc.build_screen_payloads(
        [{"id": "abc", "canonical_name": "Foo Org", "description": "does things"}], "SYSTEM"
    )
    assert payloads == [
        {
            "payload_kind": "company_screen",
            "id": "abc",
            "canonical_name": "Foo Org",
            "system_prompt": "SYSTEM",
            "user_msg": sc.build_user_message("Foo Org", "does things"),
        }
    ]


def test_cmd_save_applies_drops_and_skips_unknown(dal, tmp_path, monkeypatch):
    """Subagent decisions round-trip: keep=false drops with a 'screen:' reason,
    keep=true leaves the row a candidate, an unknown id is skipped (kept)."""
    conn = dal.get_conn()
    drop_id = _insert_company(conn, "Acme Staffing Agency")
    keep_id = _insert_company(conn, "Ocean Cleanup Initiative")
    _insert_earning_vacancy(conn, drop_id)  # both earned → decidable via --save
    _insert_earning_vacancy(conn, keep_id)
    monkeypatch.setattr(sc, "get_conn", lambda: conn)

    f1 = tmp_path / "r1.json"
    f1.write_text(
        '{"id": "%s", "keep": false, "reason": "staffing agency"}' % drop_id, encoding="utf-8"
    )
    f2 = tmp_path / "r2.json"
    f2.write_text(
        '{"id": "%s", "keep": true, "reason": "plausible fit"}' % keep_id, encoding="utf-8"
    )
    f3 = tmp_path / "r3.json"
    f3.write_text('{"id": "no-such-id", "keep": false, "reason": "whatever"}', encoding="utf-8")

    rc = sc.cmd_save([str(f1), str(f2), str(f3)])
    assert rc == 0

    cur = conn.cursor()
    cur.execute("SELECT status, status_reason FROM company WHERE id = %s", (drop_id,))
    status, reason = cur.fetchone()
    assert status == "inactive" and reason == "screen: staffing agency"
    cur.execute("SELECT status FROM company WHERE id = %s", (keep_id,))
    assert cur.fetchone()[0] == "candidate"  # kept rows are never modified
    cur.close()


def test_cmd_save_malformed_file_keeps_its_companies(dal, tmp_path, monkeypatch):
    """A malformed result file is named and skipped — its companies stay kept
    (fail-safe), the rest still apply."""
    conn = dal.get_conn()
    ok_id = _insert_company(conn, "Acme Staffing Agency")
    ocean_id = _insert_company(conn, "Ocean Cleanup Initiative")
    _insert_earning_vacancy(conn, ok_id)
    _insert_earning_vacancy(conn, ocean_id)
    monkeypatch.setattr(sc, "get_conn", lambda: conn)

    good = tmp_path / "good.json"
    good.write_text('{"id": "%s", "keep": false, "reason": "agency"}' % ok_id, encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text('{"id": "truncated...', encoding="utf-8")

    assert sc.cmd_save([str(good), str(bad)]) == 0
    remaining = {r["canonical_name"] for r in sc.load_fresh_candidates(conn)}
    assert remaining == {"Ocean Cleanup Initiative"}


# ---------------------------------------------------------------------------
# 7. Run marker — "ran, kept all" is auditable apart from "deferred/fail-safe"
# ---------------------------------------------------------------------------


def test_write_summary_records_llm_ran_and_fail_safe(tmp_path):
    row = {"id": "1", "canonical_name": "X"}
    summary = {
        "total": 3,
        "keep": [(row, "plausible fit"), (row, "screen error, kept (fail-safe): Boom")],
        "drop": [(row, "already tracked as Y", "dup")],
        "pending": [],
        "screened": 2,
        "fail_safe_keeps": 1,
        "llm_ran": True,
    }
    record = sc.write_summary(summary, applied=True, path=tmp_path / "summary.json")
    assert record["screened"] == 2
    assert record["llm_ran"] is True
    assert record["drops"] == 1 and record["dup_drops"] == 1 and record["llm_drops"] == 0
    assert record["fail_safe_keeps"] == 1  # kept-by-default ≠ kept-by-decision
    assert (tmp_path / "summary.json").exists()


def test_write_summary_no_key_run_is_distinguishable(tmp_path):
    row = {"id": "1", "canonical_name": "X"}
    summary = {
        "total": 1,
        "keep": [],
        "drop": [],
        "pending": [row],
        "screened": 0,
        "fail_safe_keeps": 0,
        "llm_ran": False,
    }
    record = sc.write_summary(summary, applied=True, path=tmp_path / "summary.json")
    assert record["llm_ran"] is False
    assert record["pending_count"] == 1
    assert record["payload_path"]  # run_daily gates on this


# ---------------------------------------------------------------------------
# 8. run_daily gate wiring for the no-key path
# ---------------------------------------------------------------------------


def test_run_daily_screen_gate_text_names_the_protocol():
    import run_daily

    text = run_daily._screen_gate_text(4, "haiku")
    assert '"haiku"' in text
    assert "screen_candidates.py --save" in text
    assert '"keep": true|false' in text
    assert (
        "borderline or unknown from the name alone → keep" in text.lower()
        or "borderline" in text.lower()
    )


def test_run_daily_gate_preview_knows_screen_action():
    import run_daily

    assert "relevance-screen 3" in run_daily._gate_preview("screen_companies", 3)


def test_run_daily_no_key_gates_then_resumes_past_screen(dal, monkeypatch, tmp_path):
    """No direct API key: company_scoring emits the screen gate BEFORE any paid
    enrichment step, and on --resume continues past the screen (without
    re-running it) into the paid chain."""
    import importlib
    import json
    import subprocess

    import run_daily

    importlib.reload(run_daily)
    monkeypatch.setattr(run_daily, "STATE_PATH", tmp_path / "run_state.json")
    monkeypatch.setattr(run_daily, "CO_PAYLOAD_PATH", tmp_path / "co_payload.json")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    conn = dal.get_conn()
    cid = _insert_company(conn, "Nova Harbor")
    cur = conn.cursor()
    cur.execute("UPDATE company SET website = %s WHERE id = %s", ("https://novaharbor.org", cid))
    conn.commit()
    cur.close()

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)

    def fake_run_capture(cmd, opts):
        calls.append(cmd)
        payloads = [
            {
                "payload_kind": "company",
                "id": str(cid),
                "canonical_name": "Nova Harbor",
                "url": "https://novaharbor.org",
                "system_prompt": "sp",
                "user_msg": "um",
            }
        ]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payloads), stderr="")

    monkeypatch.setattr(run_daily, "_run_capture", fake_run_capture)

    screen_calls = []

    def fake_screen(opts):
        screen_calls.append(1)
        return {
            "total": 1,
            "screened": 0,
            "llm_ran": False,
            "drops": 0,
            "dup_drops": 0,
            "llm_drops": 0,
            "fail_safe_keeps": 0,
            "pending_count": 1,
            "payload_path": str(tmp_path / "screen_payload.json"),
        }

    monkeypatch.setattr(run_daily, "_screen_candidates", fake_screen)

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, payload = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    # Gate emitted BEFORE any paid step ran.
    assert kind == "gate" and payload["action"] == "screen_companies"
    assert entry["phase"] == "screen"
    assert entry["screen"]["llm_ran"] is False  # run marker persisted in state
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert not any("find_company_urls" in s or "fetch_companies" in s for s in joined)

    # Driver marks the gate emitted; agent screens + saves, then --resume.
    entry["emitted"] = True
    kind2, payload2 = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert len(screen_calls) == 1  # the screen did NOT re-run on resume
    assert entry["phase"] == "screened"
    assert kind2 == "gate" and payload2["action"] == "score_companies"
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert any("fetch_companies" in s for s in joined)  # paid chain resumed


# ===========================================================================
# --- from test_candidate_enrichment_chain.py ---
#
# The company_scoring stage runs the full candidate chain (find site + evidence).
#
# Before WANT-scoring, run_daily's company_scoring stage must:
#   1. drop structural junk       (filter_companies.py --apply)
#   2. find missing websites      (find_company_urls.py)         <- pt 2
#   3. scrape about-pages         (fetch_companies.py --limit N)
#   4. collect primary evidence   (collect_company_evidence.py)  <- pt 1
#   5. build WANT payloads        (score_companies.py --local)
#
# These tests stub the subprocess boundary (_run / _run_capture) and assert
# the deterministic ORDER of the shelled commands, plus the FIRECRAWL-unset
# short circuit. DB helpers run for real on an isolated temp SQLite DB.
# ===========================================================================


def _force_sqlite_and_run_daily(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobsearch.db"))
    for mod in (
        "run_daily",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as dal
    import run_daily

    importlib.reload(run_daily)
    monkeypatch.setattr(run_daily, "STATE_PATH", tmp_path / "run_state.json")
    monkeypatch.setattr(run_daily, "FETCH_STATS_PATH", tmp_path / "fetch_stats.json")
    monkeypatch.setattr(run_daily, "CO_PAYLOAD_PATH", tmp_path / "co_payload.json")
    return run_daily, dal


@pytest.fixture()
def rd(monkeypatch, tmp_path):
    run_daily, dal = _force_sqlite_and_run_daily(monkeypatch, tmp_path)
    yield run_daily, dal
    dal.close_conn()


# A benign successful screen record (llm_ran, nothing pending): the stubbed _run
# in these chain tests never writes the real screen summary file, so the driver's
# money valve would otherwise (correctly) treat the screen as crashed. These tests
# assert the PAID chain order, not the screen, so we stub a clean screen result.
_OK_SCREEN = {
    "total": 0,
    "screened": 0,
    "llm_ran": True,
    "drops": 0,
    "dup_drops": 0,
    "llm_drops": 0,
    "fail_safe_keeps": 0,
    "pending_count": 0,
}


def _seed_candidate(dal, name, website):
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO company (canonical_name, status, website) VALUES (%s, 'candidate', %s)",
        (name, website),
    )
    dal.get_conn().commit()
    cur.execute("SELECT id FROM company WHERE canonical_name = %s", (name,))
    cid = cur.fetchone()[0]
    cur.close()
    return cid


def test_ghost_and_names_helpers(rd):
    run_daily, dal = rd
    scored = _seed_candidate(dal, "Nova Harbor", "https://novaharbor.org")
    _seed_candidate(dal, "Ghost Org", "")  # no website -> ghost

    assert run_daily._ghost_candidate_count() == 1
    names = run_daily._candidate_names_to_score(10)
    assert names == ["Nova Harbor"]  # only the website-bearing candidate is scorable
    assert str(scored)  # id exists


def test_company_scoring_runs_full_chain_in_order(rd, monkeypatch):
    run_daily, dal = rd
    cid = _seed_candidate(dal, "Nova Harbor", "https://novaharbor.org")
    _seed_candidate(dal, "Ghost Org", "")  # exercises the backfill branch
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    calls = []

    def fake_run(cmd, opts):
        calls.append(cmd)
        return 0

    def fake_run_capture(cmd, opts):
        calls.append(cmd)
        payloads = [
            {
                "payload_kind": "company",
                "id": str(cid),
                "canonical_name": "Nova Harbor",
                "url": "https://novaharbor.org",
                "system_prompt": "sp",
                "user_msg": "um",
            }
        ]
        import json

        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payloads), stderr="")

    monkeypatch.setattr(run_daily, "_run", fake_run)
    monkeypatch.setattr(run_daily, "_run_capture", fake_run_capture)
    monkeypatch.setattr(run_daily, "_screen_candidates", lambda opts: _OK_SCREEN)

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, payload = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "gate"
    assert payload["action"] == "score_companies"

    joined = [" ".join(str(x) for x in c) for c in calls]

    # The five steps appear in the required order.
    def idx(substr):
        return next(i for i, s in enumerate(joined) if substr in s)

    assert idx("filter_companies.py") < idx("find_company_urls.py")
    assert idx("find_company_urls.py") < idx("fetch_companies.py")
    assert idx("fetch_companies.py") < idx("collect_company_evidence.py")
    assert idx("collect_company_evidence.py") < idx("score_companies.py")
    # evidence collected for exactly the scorable set (the website-bearing candidate)
    assert any("collect_company_evidence.py" in s and "Nova Harbor" in s for s in joined)


def test_company_scoring_skips_when_firecrawl_unset(rd, monkeypatch):
    run_daily, dal = rd
    _seed_candidate(dal, "Nova Harbor", "https://novaharbor.org")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)
    monkeypatch.setattr(
        run_daily,
        "_run_capture",
        lambda cmd, opts: (
            calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        ),
    )

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, note = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "skip"
    assert "FIRECRAWL_API_KEY unset" in note
    # No enrichment subprocess ran without a key.
    assert calls == [], "no find/collect/score subprocess should run without Firecrawl"


def test_backfill_caps_ghost_search_at_max_per_run(rd, monkeypatch):
    """_backfill_candidate_websites must never let find_company_urls.py run
    unbounded: it fires one paid Firecrawl search() per ghost candidate
    (STRATEGY guardrail 3: cost), so it is capped at the same per-run safety
    net already used for scoring (scoring_settings.max_per_run). More ghosts
    than the cap must still call find_company_urls.py once with --limit set
    to the cap, and print how many were deferred (no silent caps)."""
    import scoring_settings

    monkeypatch.setattr(scoring_settings, "max_per_run", lambda: 2)
    run_daily, dal = rd
    for i in range(5):
        _seed_candidate(dal, f"Ghost Org {i}", "")  # no website -> ghost

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)

    run_daily._backfill_candidate_websites(run_daily.Opts())

    assert len(calls) == 1
    cmd = calls[0]
    assert "find_company_urls.py" in " ".join(cmd)
    assert cmd[-2:] == ["--limit", "2"], cmd


def test_backfill_skips_limit_flag_reasoning_when_under_cap(rd, monkeypatch, capsys):
    """Fewer ghosts than the cap still passes the exact ghost count as --limit
    (a no-op cap) and prints the plain (uncapped) message, not the deferred one."""
    import scoring_settings

    monkeypatch.setattr(scoring_settings, "max_per_run", lambda: 50)
    run_daily, dal = rd
    _seed_candidate(dal, "Ghost Org", "")

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)

    run_daily._backfill_candidate_websites(run_daily.Opts())

    assert calls[0][-2:] == ["--limit", "1"]
    out = capsys.readouterr().out
    assert "deferred" not in out


def test_company_scoring_caps_paid_chain_at_max_per_run(rd, monkeypatch, capsys):
    """company_scoring must cap the whole PAID chain — Firecrawl scrape,
    evidence collection, WANT-scoring — at scoring_settings.max_per_run()
    (STRATEGY guardrail 3: cost). The bug: the stage passed the UNCAPPED candidate
    count as an explicit --limit to score_companies, and score_companies only
    applies its own cap when --limit is None, so the cap was always bypassed. All
    three paid steps must receive the capped count/names, and the deferred count
    must be reported (no silent drops)."""
    import scoring_settings

    monkeypatch.setattr(scoring_settings, "max_per_run", lambda: 2)
    run_daily, dal = rd
    ids = [_seed_candidate(dal, f"Cand {i}", f"https://cand{i}.org") for i in range(5)]
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)
    monkeypatch.setattr(run_daily, "_screen_candidates", lambda opts: _OK_SCREEN)

    def fake_run_capture(cmd, opts):
        calls.append(cmd)
        import json

        payloads = [
            {
                "payload_kind": "company",
                "id": str(ids[i]),
                "canonical_name": f"Cand {i}",
                "url": f"https://cand{i}.org",
                "system_prompt": "sp",
                "user_msg": "um",
            }
            for i in range(2)
        ]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payloads), stderr="")

    monkeypatch.setattr(run_daily, "_run_capture", fake_run_capture)

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, payload = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "gate"

    def cmd_with(substr):
        return next(c for c in calls if any(substr in str(x) for x in c))

    # Firecrawl scrape capped at 2, not the 5 available.
    fetch = cmd_with("fetch_companies.py")
    assert fetch[-2:] == ["--limit", "2"], fetch
    # WANT-scoring capped at 2 too — passing 5 here suppressed score_companies'
    # own internal cap (the actual bug this test guards against).
    score = cmd_with("score_companies.py")
    assert score[-2:] == ["--limit", "2"], score
    # Evidence collected for EXACTLY the capped set (2 names), not all 5.
    evidence = cmd_with("collect_company_evidence.py")
    names_arg = evidence[evidence.index("--company") + 1]
    assert names_arg.split(",") == ["Cand 0", "Cand 1"], names_arg
    # Deferral reported to the run output (3 = 5 available − 2 cap).
    assert "3 deferred to a later run" in capsys.readouterr().out
    # And surfaced at the scoring gate note.
    assert "3 candidate(s) deferred" in payload["instructions"]


def test_company_scoring_closes_valve_when_screen_crashes(rd, monkeypatch):
    """Money valve (R2): a crashed cheap screen (returns None) withholds ALL paid
    enrichment this cycle — no website search, scrape, or evidence subprocess runs
    — records a BLOCKING warning, and returns a skip with the withheld-count note."""
    run_daily, dal = rd
    _seed_candidate(dal, "Nova Harbor", "https://novaharbor.org")
    _seed_candidate(dal, "Ghost Org", "")  # a ghost too
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)
    monkeypatch.setattr(
        run_daily,
        "_run_capture",
        lambda cmd, opts: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "[]", ""),
    )
    # Screen crashed: no run marker written → _screen_candidates returns None.
    monkeypatch.setattr(run_daily, "_screen_candidates", lambda opts: None)

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, note = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "skip"
    assert "paid enrichment withheld" in note
    # No PAID enrichment subprocess ran (only the free junk prefilter may have).
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert not any(
        s
        for s in joined
        if "find_company_urls" in s
        or "fetch_companies" in s
        or "collect_company_evidence" in s
        or "score_companies" in s
    )
    # A blocking warning was recorded → publish gate will treat the run as dirty.
    blocking = [w for w in state["warnings"] if w["blocking"]]
    assert blocking and "withheld" in blocking[0]["message"]


def test_company_scoring_advances_when_no_candidates(rd, monkeypatch):
    run_daily, dal = rd
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    # No candidates seeded; the junk-filter + backfill run, then n == 0 -> advance.
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: 0)
    monkeypatch.setattr(run_daily, "_screen_candidates", lambda opts: _OK_SCREEN)
    monkeypatch.setattr(
        run_daily,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr=""),
    )

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, note = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "advance"
    assert "no new candidate companies" in note
