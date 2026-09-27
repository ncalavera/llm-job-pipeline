"""Screener junk task (U3): the combining rule, the code checks and the
override query. Runs without nikita_tools — the engine is always a fake here."""

import importlib
import sys
from datetime import date, timedelta

import pytest

import junk_task

LONG = "Operations lead for a global health charity. " * 20  # > 400 chars


def noul(p):
    return {"type": "noul", "noul": p}


def choice(probs):
    return {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs}


def answers(**over):
    """A clean job: every junk answer low, type job, English."""
    base = {q: noul(0.1) for q, spec in junk_task.QUESTIONS.items() if spec["type"] == "noul"}
    base["english"] = noul(0.99)
    base["type"] = choice({"job": 0.99, "programme": 0.01, "not_opportunity": 0.0})
    base["function"] = choice({o: (0.99 if o == "other" else 0.0) for o in junk_task.QUESTIONS["function"]["criteria"]})
    base["pay"] = choice({o: (0.99 if o == "not_stated" else 0.0) for o in junk_task.QUESTIONS["pay"]["criteria"]})
    base["years"] = choice({o: (0.99 if o == "not_stated" else 0.0) for o in junk_task.QUESTIONS["years"]["criteria"]})
    for k, v in over.items():
        base[k] = v
    return base


def role(**over):
    r = {"id": "r1", "org": "Org", "title": "Ops Lead", "posting": LONG, "locations": ["Remote"],
         "deadline": None, "override": None}
    r.update(over)
    return r


def test_clean_job_is_scored():
    assert junk_task.decide(answers(), role())[0] is False


# --- Acceptance examples -------------------------------------------------------


def test_ae1_right_to_work_skips():
    assert junk_task.decide(answers(right_to_work=noul(0.98)), role()) == (True, "right_to_work", 0.98)


def test_ae2_ai_safety_employer_does_not_rescue_engineering():
    fn = {o: 0.0 for o in junk_task.QUESTIONS["function"]["criteria"]}
    fn["engineering"] = 0.97
    fn["other"] = 0.03
    skip, question, p = junk_task.decide(answers(function=choice(fn)), role(org="AI Safety Institute"))
    assert (skip, question) == (True, "function")
    assert p == pytest.approx(0.97)


def test_ae3_unpaid_programme_is_scored():
    a = answers(type=choice({"job": 0.1, "programme": 0.9, "not_opportunity": 0.0}), unpaid=noul(0.99))
    assert junk_task.decide(a, role())[0] is False


def test_ae4_override_scores_and_records_it():
    skip, question, p = junk_task.decide(answers(right_to_work=noul(0.97)), role(override="status:applied"))
    assert skip is False
    assert question == "override:status:applied"


def test_ae5_short_posting_scores_without_model_call():
    calls = []

    def engine(state, questions):
        calls.append(state)
        return answers(right_to_work=noul(0.99))

    assert junk_task.check(role(posting="x" * 200), engine=engine)["skip"] is False
    assert calls == []


# --- Other U3 scenarios --------------------------------------------------------


def test_deadline_yesterday_is_expired_without_model():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    calls = []
    rec = junk_task.check(role(deadline=yesterday), engine=lambda s, q: calls.append(s))
    assert (rec["skip"], rec["question"], rec["p"]) == (True, "expired", None)
    assert calls == []


def test_deadline_today_is_not_expired():
    assert junk_task.decide(answers(), role(deadline=date.today().isoformat()))[0] is False


def test_ai_safety_research_exception_scores():
    a = answers(domain_experience=noul(0.98), ai_safety_research=noul(0.9))
    assert junk_task.decide(a, role())[0] is False


def test_domain_experience_without_exception_skips():
    a = answers(domain_experience=noul(0.98), ai_safety_research=noul(0.01))
    assert junk_task.decide(a, role())[:2] == (True, "domain_experience")


def test_non_english_scores_flagged():
    a = answers(english=noul(0.2), right_to_work=noul(0.99))
    assert junk_task.decide(a, role()) == (False, "non_english", None)


def test_cut_is_at_or_above():
    assert junk_task.decide(answers(place=noul(0.94)), role())[0] is False
    assert junk_task.decide(answers(place=noul(0.95)), role()) == (True, "place", 0.95)


def test_far_below_pay_skips_jobs_only():
    pay = {o: 0.0 for o in junk_task.QUESTIONS["pay"]["criteria"]}
    pay["under_2000"] = 0.97
    pay["not_stated"] = 0.03
    assert junk_task.decide(answers(pay=choice(pay)), role())[:2] == (True, "pay")
    prog = choice({"job": 0.0, "programme": 1.0, "not_opportunity": 0.0})
    assert junk_task.decide(answers(pay=choice(pay), type=prog), role())[0] is False


def test_far_too_senior_years_skips():
    yrs = {o: 0.0 for o in junk_task.QUESTIONS["years"]["criteria"]}
    yrs["13_plus"] = 0.96
    yrs["not_stated"] = 0.04
    assert junk_task.decide(answers(years=choice(yrs)), role())[:2] == (True, "years")


def test_not_an_opportunity_skips():
    a = answers(type=choice({"job": 0.0, "programme": 0.02, "not_opportunity": 0.98}))
    assert junk_task.decide(a, role()) == (True, "type", 0.98)


def test_baseline_answer_shape_uses_its_cut():
    assert junk_task.decide({"p_junk": 0.91, "cut": 0.9}, role()) == (True, "baseline", 0.91)
    assert junk_task.decide({"p_junk": 0.89, "cut": 0.9}, role())[0] is False
    assert junk_task.decide({"p_junk": 0.99, "cut": 0.9}, role(override="north_star"))[0] is False


def test_engine_tuple_shape_and_usage():
    rec = junk_task.check(role(), engine=lambda s, q: (answers(credential=noul(0.99)), {"input_tokens": 5}))
    assert (rec["skip"], rec["question"], rec["usage"]) == (True, "credential", {"input_tokens": 5})


def test_state_holds_profile_and_trimmed_role():
    state = junk_task.build_state(role(posting="y" * 9000))
    assert set(state) == {"profile", "role"}
    assert len(state["role"]["posting"]) == junk_task.POSTING_CAP
    assert set(state["role"]) == {"title", "org", "locations", "posting"}


def test_task_version_changes_with_engine_and_is_stable():
    assert junk_task.task_version("jev") == junk_task.task_version("jev")
    assert junk_task.task_version("jev") != junk_task.task_version("baseline")


def test_questions_match_jev_schema():
    for qid, spec in junk_task.QUESTIONS.items():
        assert spec["type"] in {"noul", "choice"}, qid
        assert spec["instructions"], qid
        if spec["type"] == "noul":
            assert set(spec["criteria"]) == {"true", "false"}, qid
        else:
            assert len(spec["criteria"]) >= 2, qid


# --- Overrides on the sqlite fixture -------------------------------------------


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobsearch.db"))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend", "migrate"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import migrate

    assert migrate.cmd_migrate(allow_destructive=True, do_backup=False) == 0
    return db_backend.get_conn()


def _company(conn, cid):
    conn.cursor().execute("INSERT INTO company (id, canonical_name) VALUES (%s, %s)", (cid, cid))


def _vacancy(conn, vid, cid, status, screening=None):
    conn.cursor().execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, first_seen, last_seen, status, screening) "
        "VALUES (%s, %s, %s, %s, '2026-09-01', '2026-09-01', %s, %s)",
        (vid, vid, cid, vid, status, screening),
    )


def test_overrides_query(conn):
    for cid in ("star", "passed", "applied", "app_row", "liked"):
        _company(conn, cid)
    _vacancy(conn, "v1", "star", "unseen", '{"north_star": true}')
    _vacancy(conn, "v2", "passed", "passed", '{"north_star": false}')
    _vacancy(conn, "v3", "applied", "applied")
    _vacancy(conn, "v4", "app_row", "unseen")
    _vacancy(conn, "v5", "liked", "liked")
    conn.cursor().execute("INSERT INTO application (company_id) VALUES (%s)", ("app_row",))
    conn.commit()

    got = junk_task.overrides(conn, ["star", "passed", "applied", "app_row", "liked", "unknown"])
    assert got == {"star": "north_star", "applied": "status:applied", "app_row": "application",
                   "liked": "status:liked"}
    assert junk_task.overrides(conn, []) == {}


# --- Profile card from the gitignored file (never in the public repo) ---------


def _profile_file(tmp_path, monkeypatch, pay):
    import json

    path = tmp_path / "junk_profile.json"
    path.write_text(json.dumps({"pay_target_eur_month": pay, "card": {"lives_in": "Elsewhere"}}))
    monkeypatch.setenv("JUNK_PROFILE_PATH", str(path))


def test_state_profile_is_the_card_from_the_profile_file():
    assert junk_task.build_state(role())["profile"]["lives_in"] == "Test City (UTC+0)"


def test_pay_target_comes_from_the_profile_file(tmp_path, monkeypatch):
    pay = {o: 0.0 for o in junk_task.QUESTIONS["pay"]["criteria"]}
    pay["2000_3499"] = 0.97
    pay["not_stated"] = 0.03
    assert junk_task.decide(answers(pay=choice(pay)), role())[:2] == (True, "pay")  # fixture: 8000
    _profile_file(tmp_path, monkeypatch, 5000)
    assert junk_task.decide(answers(pay=choice(pay)), role())[0] is False


def test_task_version_changes_with_the_profile(tmp_path, monkeypatch):
    before = junk_task.task_version("jev")
    _profile_file(tmp_path, monkeypatch, 8000)
    assert junk_task.task_version("jev") != before


def test_missing_profile_loads_as_none(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNK_PROFILE_PATH", str(tmp_path / "absent.json"))
    assert junk_task.load_profile() is None
