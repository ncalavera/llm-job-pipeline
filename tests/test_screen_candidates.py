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
"""

import importlib
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
# 1. parse_decision — fail-safe to KEEP
# ---------------------------------------------------------------------------


def test_parse_decision_explicit_drop():
    d = sc.parse_decision('{"keep": false, "reason": "staffing agency"}')
    assert d["keep"] is False
    assert d["reason"] == "staffing agency"


def test_parse_decision_explicit_keep():
    d = sc.parse_decision('{"keep": true, "reason": "plausible fit"}')
    assert d["keep"] is True


def test_parse_decision_tolerates_fenced_json():
    d = sc.parse_decision('```json\n{"keep": false, "reason": "car dealership"}\n```')
    assert d["keep"] is False and d["reason"] == "car dealership"


def test_parse_decision_malformed_keeps():
    # A screen must NOT drop on an unreadable response — that would silently hide
    # a real employer.
    d = sc.parse_decision("the model rambled without any JSON")
    assert d["keep"] is True


def test_parse_decision_missing_keep_defaults_to_keep():
    d = sc.parse_decision('{"reason": "no verdict field"}')
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


def test_apply_drops_sets_inactive_and_removes_from_enrichment(dal):
    conn = dal.get_conn()
    # A tracked (already active) company + two fresh candidates: one a clear
    # mismatch, one a plausible fit.
    _insert_company(conn, "Clean Water Fund", status="active", alignment_score=80)
    _insert_company(conn, "Acme Staffing Agency", description="temp placement")
    _insert_company(conn, "Ocean Cleanup Initiative")

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


def test_dup_and_llm_drops_get_distinct_prefixes(dal):
    """Dedup drops are filterable apart from LLM drops: 'screen-dup:' vs 'screen:'."""
    conn = dal.get_conn()
    _insert_company(conn, "Save the Children", status="active", alignment_score=75)
    _insert_company(conn, "Save the Children International")  # dup of tracked
    _insert_company(conn, "Acme Staffing Agency")  # LLM clear mismatch

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
    _insert_company(conn, "Ocean Cleanup Initiative")
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
