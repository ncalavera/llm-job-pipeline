"""Tests for scripts/judge_roles.py: completeness check, the apply rule table,
seeded audit sampling, brief date injection, and the migration-0033
column-missing skip. No network, no model calls.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import judge_roles as jr  # noqa: E402


# ---------------------------------------------------------------------------
# Date injection
# ---------------------------------------------------------------------------


def test_inject_today_replaces_hardcoded_date():
    brief = "Rules. ...expired (deadline passed; today is 2026-09-17), direction..."
    out = jr.inject_today(brief, today="2026-10-02")
    assert "today is 2026-10-02" in out
    assert "2026-09-17" not in out


def test_inject_today_no_match_leaves_brief_unchanged():
    brief = "No date pattern here."
    assert jr.inject_today(brief, today="2026-10-02") == brief


def test_brief_version_is_stem_colon_hash(tmp_path):
    brief_path = tmp_path / "brief_judge_v4.md"
    brief_path.write_text("some rules")
    version = jr.brief_version(str(brief_path))
    assert version.startswith("brief_judge_v4:")
    assert len(version.split(":", 1)[1]) == 8


# ---------------------------------------------------------------------------
# Role payload parity (judge batch payload vs audit user_msg)
# ---------------------------------------------------------------------------


def test_role_fields_is_exactly_the_ten_bakeoff_fields():
    role = {
        "id": "r1",
        "org": "Acme",
        "title": "Ops",
        "posting": "text",
        "deadline": "2026-10-01",
        "locations": ["Remote"],
        "org_about": "about",
        "compensation": "USD 100k",
        "visa_sponsor": True,
        "us_eligibility": None,
        "kill_kind": "direction",  # bookkeeping key, must NOT leak into the payload
        "judge": {"verdict": "KILL"},
        "llm_score": 5,
    }
    fields = jr.role_fields(role)
    assert set(fields) == set(jr.ROLE_FIELDS)
    assert "kill_kind" not in fields and "judge" not in fields and "llm_score" not in fields


def test_payload_for_roles_have_exactly_the_role_fields():
    role = {k: None for k in jr.ROLE_FIELDS}
    payload = jr.payload_for([role], "system prompt")
    assert set(payload["roles"][0]) == set(jr.ROLE_FIELDS)


def test_role_payload_decodes_row_into_the_ten_fields():
    row = {
        "id": "abc",
        "org": "Acme",
        "title": "Ops",
        "full_description": "posting text",
        "deadline": None,
        "locations": '["Remote"]',
        "org_about": "x" * 600,
        "compensation": "USD 100k",
        "visa_sponsor": None,
        "us_eligibility": None,
    }
    role = jr.role_payload(row)
    assert set(role) == set(jr.ROLE_FIELDS)
    assert role["posting"] == "posting text"
    assert role["locations"] == ["Remote"]
    assert len(role["org_about"]) == 500  # truncated, matching dump.sh's LEFT(...,500)


def test_role_payload_sends_the_snippet_when_the_download_left_no_text():
    """A career-page role whose one download failed has no posting text; the
    judge reads its listing snippet instead of an empty posting."""
    role = jr.role_payload({"id": "a", "full_description": "", "snippet": "New York. USD 200k"})
    assert role["posting"] == "New York. USD 200k"


# ---------------------------------------------------------------------------
# Completeness check
# ---------------------------------------------------------------------------


def test_completeness_check_all_present():
    ids = ["a", "b"]
    results = [
        {"id": "a", "verdict": "KEEP"},
        {"id": "b", "verdict": "KILL", "kill_kind": "direction", "confidence": 5},
    ]
    valid, missing, dup, broken = jr.completeness_check(ids, results)
    assert set(valid) == {"a", "b"}
    assert missing == []
    assert dup == []
    assert broken == 0


def test_completeness_check_missing_id():
    valid, missing, dup, broken = jr.completeness_check(
        ["a", "b"], [{"id": "a", "verdict": "KEEP"}]
    )
    assert missing == ["b"]


def test_completeness_check_duplicate_id():
    ids = ["a"]
    results = [{"id": "a", "verdict": "KEEP"}, {"id": "a", "verdict": "UNSURE"}]
    valid, missing, dup, broken = jr.completeness_check(ids, results)
    assert dup == ["a"]
    assert valid["a"]["verdict"] == "UNSURE"  # last valid occurrence wins


def test_completeness_check_broken_json_is_not_a_list():
    valid, missing, dup, broken = jr.completeness_check(["a"], {"error": "JSON parse failed"})
    assert valid == {}
    assert missing == ["a"]
    assert broken == 1


def test_completeness_check_extra_id_ignored_as_broken():
    valid, missing, dup, broken = jr.completeness_check(
        ["a"], [{"id": "a", "verdict": "KEEP"}, {"id": "zzz", "verdict": "KEEP"}]
    )
    assert broken == 1
    assert missing == []


def test_completeness_check_kill_without_int_confidence_is_broken():
    valid, missing, dup, broken = jr.completeness_check(
        ["a"], [{"id": "a", "verdict": "KILL", "kill_kind": "direction", "confidence": "high"}]
    )
    assert broken == 1
    assert missing == ["a"]


def test_completeness_check_unknown_verdict_is_broken():
    valid, missing, dup, broken = jr.completeness_check(["a"], [{"id": "a", "verdict": "MAYBE"}])
    assert broken == 1


# ---------------------------------------------------------------------------
# Apply rule table
# ---------------------------------------------------------------------------

ROLE = {
    "id": "r1",
    "title": "Ops Manager",
    "posting": "We need someone with a valid US work permit.",
}
CFG = {"kill_confidence": 5}


def test_apply_kill_confidence5_quote_found_kills():
    verdict = {
        "verdict": "KILL",
        "kill_kind": "location",
        "reason": "US work permit required",
        "quote": "valid US work permit",
        "confidence": 5,
    }
    state, status, judge_json, counter = jr.apply_decision(ROLE, verdict, CFG, "m", "b1")
    assert state == "killed"
    assert status == {"status": "passed", "status_reason": "location: valid US work permit"}
    assert counter is None


def test_apply_kill_confidence4_holds_as_unsure():
    verdict = {
        "verdict": "KILL",
        "kill_kind": "location",
        "reason": "x",
        "quote": "valid US work permit",
        "confidence": 4,
    }
    state, status, judge_json, counter = jr.apply_decision(ROLE, verdict, CFG, "m", "b1")
    assert state == "unsure"
    assert status is None
    assert counter == "held_low_confidence"
    assert judge_json["held"] == "confidence 4"


def test_apply_kill_quote_missing_holds_as_unsure():
    verdict = {
        "verdict": "KILL",
        "kill_kind": "credential",
        "reason": "needs a law degree",
        "quote": "this exact phrase is not in the posting",
        "confidence": 5,
    }
    state, status, judge_json, counter = jr.apply_decision(ROLE, verdict, CFG, "m", "b1")
    assert state == "unsure"
    assert status is None
    assert counter == "quote_refused"
    assert judge_json["held"] == "quote not found"


def test_apply_direction_kill_confidence5_needs_no_quote():
    verdict = {
        "verdict": "KILL",
        "kill_kind": "direction",
        "reason": "commercial, no impact mission",
        "quote": "a payments startup",
        "confidence": 5,
    }
    state, status, judge_json, counter = jr.apply_decision(ROLE, verdict, CFG, "m", "b1")
    assert state == "killed"
    assert status["status_reason"] == "direction: a payments startup"


def test_apply_us_canada_location_kill_quotes_the_title():
    """v4 brief exception: quote may be the role TITLE, checked against posting+title."""
    role = {
        "id": "r2",
        "title": "Onsite Ops Lead (Austin, TX)",
        "posting": "Full time role, no remote option.",
    }
    verdict = {
        "verdict": "KILL",
        "kill_kind": "location",
        "reason": "onsite US only",
        "quote": "Onsite Ops Lead (Austin, TX)",
        "confidence": 5,
    }
    state, status, judge_json, counter = jr.apply_decision(role, verdict, CFG, "m", "b1")
    assert state == "killed"


def test_apply_keep_and_unsure_map_directly():
    keep_state, *_ = jr.apply_decision(ROLE, {"verdict": "KEEP", "reason": "fits"}, CFG, "m", "b1")
    unsure_state, *_ = jr.apply_decision(
        ROLE, {"verdict": "UNSURE", "reason": "unclear"}, CFG, "m", "b1"
    )
    assert keep_state == "keep"
    assert unsure_state == "unsure"


# ---------------------------------------------------------------------------
# Seeded audit sample determinism
# ---------------------------------------------------------------------------


def test_sample_for_audit_direction_and_high_score_always_included():
    kills = [
        {"id": "d1", "kill_kind": "direction", "llm_score": 5},
        {"id": "s1", "kill_kind": "location", "llm_score": 25},
        {"id": "low1", "kill_kind": "location", "llm_score": 3},
    ]
    sample = jr.sample_for_audit(kills, min_score=20, sample_pct=0, seed="2026-09-17")
    assert sample["d1"] == "direction"
    assert sample["s1"] == "score>=20"
    assert "low1" not in sample


def test_sample_for_audit_is_deterministic_for_the_same_seed():
    kills = [{"id": f"k{i}", "kill_kind": "location", "llm_score": 1} for i in range(20)]
    a = jr.sample_for_audit(kills, min_score=20, sample_pct=30, seed="run-42")
    b = jr.sample_for_audit(kills, min_score=20, sample_pct=30, seed="run-42")
    assert a == b
    c = jr.sample_for_audit(kills, min_score=20, sample_pct=30, seed="run-43")
    assert a != c  # different seed, overwhelmingly likely a different draw


# ---------------------------------------------------------------------------
# Column-missing skip (migration 0033 not applied)
# ---------------------------------------------------------------------------


def test_judge_columns_ready_false_without_columns(monkeypatch):
    import database_supabase

    monkeypatch.setattr(database_supabase, "_vacancy_has_column", lambda col: False)
    assert jr.judge_columns_ready() is False


def test_run_judge_stage_skips_cleanly_without_columns(monkeypatch):
    import database_supabase

    monkeypatch.setattr(database_supabase, "_vacancy_has_column", lambda col: False)
    cfg = {"brief_path": "/tmp/does-not-need-to-exist.md", "max_per_run": 10}
    result = jr.run_judge_stage(cfg)
    assert "skipped" in result
    assert "migration 0033" in result["skipped"]


def test_run_judge_stage_skips_when_brief_path_empty():
    result = jr.run_judge_stage({"brief_path": "", "max_per_run": 10})
    assert result["skipped"] == "no judge brief configured ([judge] brief_path is empty)"


def test_run_audit_stage_skips_when_review_brief_path_empty():
    result = jr.run_audit_stage({"review_brief_path": ""}, seed="2026-09-17")
    assert "skipped" in result


def test_audit_due_only_for_fresh_unaudited_kills():
    cutoff = "2026-01-02T00:00:00+00:00"
    fresh = {"judge": {"judged_at": "2026-01-02T22:00:00+00:00"}}
    assert jr._audit_due(fresh, cutoff)
    assert not jr._audit_due({**fresh, "audit": {"verdict": "UPHOLD"}}, cutoff)
    assert not jr._audit_due({"judge": {"judged_at": "2026-01-01T22:00:00+00:00"}}, cutoff)
    assert not jr._audit_due({"judge": {}}, cutoff)


def test_save_judge_marks_too_senior_level_kill_as_north_star():
    class Cur:
        def __init__(self, log):
            self.log = log

        def execute(self, sql, params=()):
            self.log.append(params)

        def fetchone(self):
            return ({},)

        def close(self):
            pass

    class Conn:
        def __init__(self):
            self.log = []

        def cursor(self):
            return Cur(self.log)

    def saved(reason, kind="level"):
        conn = Conn()
        jr.save_judge(conn, "id", {"kill_kind": kind, "reason": reason}, "killed", None)
        return conn.log[-1][0].adapted

    assert saved("Too senior: C-level post, 15+ years")["north_star"] is True
    assert "north_star" not in saved("Entry-level internship")
    assert "north_star" not in saved("Too senior: x", kind="experience")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
