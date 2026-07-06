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
    assert dups[0][0]["id"] == "1"
    assert "already tracked as Save the Children" in dups[0][1]


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
    dropped = {r["canonical_name"] for r, _ in summary["drop"]}
    assert kept == {"Clean Water Fund"}
    assert dropped == {"Save the Children International", "Payday Loans LLC"}
    # The already-tracked duplicate was dropped by dedup — the paid LLM was never
    # asked about it (enrichment cost avoided before any spend).
    assert not any("Save the Children" in name for name in llm.seen_names)


def test_run_screen_no_credentials_keeps_all(monkeypatch):
    candidates = [
        {"id": "1", "canonical_name": "Anything At All", "description": ""},
        {"id": "2", "canonical_name": "Another Org", "description": ""},
    ]
    monkeypatch.setattr(sc, "load_fresh_candidates", lambda conn, limit=0: candidates)
    monkeypatch.setattr(sc, "load_tracked_names", lambda conn: [])

    summary = sc.run_screen(object(), call_llm=None, limit=0)
    assert len(summary["keep"]) == 2 and not summary["drop"]
    assert summary["llm_ran"] is False


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
