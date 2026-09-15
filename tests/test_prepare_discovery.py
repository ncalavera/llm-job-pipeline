import sys
import pytest
from pathlib import Path

S = str(Path(__file__).resolve().parent.parent / "scripts")
if S not in sys.path:
    sys.path.insert(0, S)
import prepare_discovery as d

POSTING = (
    "We are hiring a Programme Manager.\nFluent Spanish is required for this role.\nExperience with grant management is preferred.\nThe role is based in Madrid; hybrid working is possible.\n"
) * 8

import importlib
import sqlite3
import uuid
from datetime import date

MIGRATIONS = Path(__file__).resolve().parent.parent / "sql" / "migrations"


def _force_sqlite(monkeypatch, db_file):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    import database_supabase as db

    return db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = _force_sqlite(monkeypatch, tmp_path / "discovery.db")
    db.get_conn().commit()
    raw = sqlite3.connect(tmp_path / "discovery.db")
    for name in (
        "0013_add_source_board",
        "0025_add_vacancy_scoring_excluded_reason",
        "0027_add_vacancy_screening",
    ):
        raw.executescript((MIGRATIONS / f"{name}.sqlite.sql").read_text())
    raw.commit()
    raw.close()
    sys.modules.pop("prepare_screening", None)
    import prepare_screening as ps

    yield db, importlib.reload(ps)
    db.close_conn()


def _seed(db, vid, desc=POSTING, company_status="candidate", score=None):
    c = db.get_conn()
    cur = c.cursor()
    cid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO company (id,canonical_name,status) VALUES (%s,%s,%s)",
        (cid, "Org " + vid[:4], company_status),
    )
    cur.execute(
        "INSERT INTO vacancy (id,dedup_hash,company_id,title,full_description,status,first_seen,last_seen,llm_score) VALUES (%s,%s,%s,%s,%s,'unseen',%s,%s,%s)",
        (
            vid,
            "h-" + vid,
            cid,
            "Programme Manager",
            desc,
            date.today().isoformat(),
            date.today().isoformat(),
            score,
        ),
    )
    c.commit()
    cur.close()


def _set_state(db, vid, state, fp):
    c = db.get_conn()
    cur = c.cursor()
    cur.execute(
        "UPDATE vacancy SET screening_state=%s,screening_fingerprint=%s WHERE id=%s",
        (state, fp, vid),
    )
    c.commit()
    cur.close()


def test_select_uses_existing_score_and_fingerprint(monkeypatch):
    row = {
        "id": "1",
        "org": "O",
        "title": "T",
        "full_description": "A real posting " * 20,
        "llm_score": 42,
        "status": "unseen",
        "screening_state": "ready",
        "screening_fingerprint": d._fp("A real posting " * 20),
        "first_seen": "2026-01-01",
    }
    monkeypatch.setattr("database_supabase.load_vacancies", lambda **kw: {"1": row})
    monkeypatch.setattr(d, "_cap", lambda n: 10)
    assert d.select_payloads() == []


def test_completion_skips_stale_and_accepts_current(monkeypatch):
    text = "posting " * 50
    row = {
        "id": "1",
        "full_description": text,
        "status": "unseen",
        "scoring_excluded_reason": None,
        "llm_score": 3,
        "screening_state": "ready",
        "screening_fingerprint": d._fp(text),
        "company_status": "candidate",
    }
    p = {"id": "1", "fingerprint": d._fp(text), "scoring": None, "screening": None}

    class Cur:
        def execute(self, *a):
            pass

        def fetchone(self):
            return row

        def close(self):
            pass

    class Conn:
        def cursor(self, **kw):
            return Cur()

    import database_supabase

    monkeypatch.setattr(database_supabase, "get_conn", lambda: Conn())
    assert d.completion([p])["1"] == "ready"
    assert d.completion([{**p, "fingerprint": "bad"}])["1"] == "skipped"


def test_score_validation_rejects_short_summary():
    out, err = d._validate_score({"score": 20, "reasoning": "ok", "short_summary": "short"}, {})
    assert out is None and "summary" in err


def test_pure_validate_checks_identity_and_posting_quotes():
    text = "A real posting sentence. " * 20
    payload = {
        "id": "1",
        "fingerprint": d._fp(text),
        "scoring": None,
        "screening": {"user_msg": "**Posting text:**\n" + text},
    }
    facts = {
        "posting_facts": {
            "duties": "x",
            "function": "operations",
            "seniority": "unknown",
            "employment_type": "unknown",
            "compensation": None,
            "location": None,
            "work_mode": "unknown",
            "work_authorisation": None,
            "deadline": None,
            "requirements": [
                {
                    "kind": "skill",
                    "value": "x",
                    "strength": "unknown",
                    "quote": "A real posting sentence.",
                }
            ],
        },
        "profile_comparison": [
            {"requirement": 0, "profile_factor": "x", "finding": "unknown", "note": "x"}
        ],
        "unknowns": [],
        "work_profile": {
            "activities": [],
            "technical_depth": {"level": "unknown", "quote": None},
            "purpose": {"kind": "unknown", "quote": None},
        },
    }
    result = {"id": "1", "fingerprint": payload["fingerprint"], "scoring": None, "screening": facts}
    assert d.validate_result(payload, result)["id"] == "1"
    result["id"] = "2"
    try:
        d.validate_result(payload, result)
    except ValueError as e:
        assert "id" in str(e)
    else:
        assert False


def _good_result(vid):
    return {
        "id": vid,
        "posting_facts": {
            "duties": "Runs programmes.",
            "function": "programme management",
            "seniority": "mid",
            "employment_type": "permanent",
            "compensation": None,
            "location": "Madrid",
            "work_mode": "hybrid",
            "work_authorisation": None,
            "deadline": None,
            "requirements": [
                {
                    "kind": "language",
                    "value": "Spanish",
                    "strength": "required",
                    "quote": "Fluent Spanish is required for this role.",
                },
                {
                    "kind": "experience",
                    "value": "grant management",
                    "strength": "preferred",
                    "quote": "Experience with grant management is preferred.",
                },
            ],
        },
        "profile_comparison": [
            {
                "requirement": 0,
                "profile_factor": "Spanish B1",
                "finding": "possible_conflict",
                "note": "B1 < fluent",
            },
            {"requirement": 1, "profile_factor": "grants", "finding": "match", "note": "ok"},
        ],
        "unknowns": ["salary"],
        "work_profile": {
            "activities": [],
            "technical_depth": {"level": "unknown", "quote": None},
            "purpose": {"kind": "unknown", "quote": None},
        },
    }


# ---------------------------------------------------------------------------
# Combined discovery round-trip on the real SQLite compatibility backend
# ---------------------------------------------------------------------------


def test_discovery_roundtrip_mixed_selection_and_cas(env, monkeypatch):
    db, ps = env
    import prepare_discovery as d

    d = importlib.reload(d)

    ids = {name: str(uuid.uuid4()) for name in ("high", "current", "stale", "cached", "empty")}
    _seed(db, ids["high"], score=45)
    _seed(db, ids["current"], score=5)
    _seed(db, ids["stale"], score=5)
    _seed(db, ids["cached"], score=None)
    _seed(db, ids["empty"], desc="")
    _set_state(db, ids["current"], "ready", ps.fingerprint(POSTING))
    _set_state(db, ids["cached"], "ready", ps.fingerprint(POSTING))
    monkeypatch.setattr(d, "_cap", lambda limit: 20)
    payloads = d.select_payloads()
    byid = {p["id"]: p for p in payloads}
    assert ids["high"] not in byid and ids["empty"] not in byid
    assert ids["current"] not in byid
    assert byid[ids["cached"]]["scoring"] is not None and byid[ids["cached"]]["screening"] is None
    assert byid[ids["stale"]]["scoring"] is None and byid[ids["stale"]]["screening"] is not None

    def score_result(vid):
        return {
            "score": 33,
            "reasoning": "A specific scoring rationale for this vacancy.",
            "short_summary": " ".join(["Фактическое резюме роли и её обязанностей."] * 35),
            "hard_requirements": [],
            "country": "Spain",
            "work_mode": "remote",
            "us_eligibility": "outside_us_ok",
            "deadline": None,
        }

    results = []
    for vid in (ids["cached"], ids["stale"]):
        item = {
            "id": vid,
            "fingerprint": byid[vid]["fingerprint"],
            "scoring": (score_result(vid) if byid[vid]["scoring"] is not None else None),
            "screening": None,
        }
        if byid[vid]["screening"] is not None:
            facts = _good_result(vid)
            item["screening"] = facts
        results.append(item)
    summary = d.save_results(results, payloads, model="haiku")
    assert summary["scored"] == 1 and summary["prepared"] == 1 and not summary["errors"]
    cur = db.get_conn().cursor()
    cur.execute("SELECT id,llm_score,screening_state,status FROM vacancy")
    rows = {str(r[0]): r for r in cur.fetchall()}
    cur.close()
    assert rows[ids["high"]][1] == 45 and rows[ids["high"]][3] == "unseen"
    assert rows[ids["stale"]][2] == "ready" and rows[ids["stale"]][3] == "unseen"
    assert d.completion(payloads)[ids["stale"]] == "ready"
    again = d.save_results(results, payloads, model="haiku")
    assert again["scored"] == 0 and again["prepared"] == 0


def test_discovery_rejects_duplicate_unknown_and_stale_without_writes(env):
    db, ps = env
    import prepare_discovery as d

    vid = str(uuid.uuid4())
    _seed(db, vid)
    payload = d.build_payload(
        {
            "id": vid,
            "org": "O",
            "title": "T",
            "full_description": POSTING,
            "llm_score": None,
            "screening_state": None,
        }
    )
    result = {"id": vid, "fingerprint": "stale", "scoring": None, "screening": None}
    try:
        d.validate_result(payload, result)
    except ValueError as exc:
        assert "fingerprint" in str(exc)
    else:
        assert False
    assert d.save_results([{"id": vid}, {"id": vid}], [payload])["errors"]
    assert d.save_results([{"id": str(uuid.uuid4())}], [payload])["errors"]


@pytest.mark.parametrize("mutation", ["decision", "excluded"])
def test_completion_skips_changed_decision_or_exclusion(env, mutation):
    db, ps = env
    import prepare_discovery as d

    d = importlib.reload(d)
    vid = str(uuid.uuid4())
    _seed(db, vid, score=None)
    payload = d.select_payloads(limit=20)
    p = next(p for p in payload if p["id"] == vid)
    cur = db.get_conn().cursor()
    if mutation == "decision":
        cur.execute("UPDATE vacancy SET status='passed' WHERE id=%s", (vid,))
    else:
        cur.execute(
            "UPDATE vacancy SET scoring_excluded_reason=%s WHERE id=%s", ("manual exclusion", vid)
        )
    db.get_conn().commit()
    cur.close()
    assert d.completion([p])[vid] == "skipped"


def test_a_rejected_employers_role_is_still_selected_and_scored(env):
    """The inverse of the old rule: an inactive (rejected) company's role is
    scored like any other. A board posts roles from employers nobody approved;
    leaving them unscored puts a blank card on /today forever."""
    db, ps = env
    import prepare_discovery as d

    d = importlib.reload(d)
    vid = str(uuid.uuid4())
    _seed(db, vid, company_status="inactive", score=None)
    p = next(p for p in d.select_payloads(limit=20) if p["id"] == vid)
    assert d.completion([p])[vid] == "pending"
    result = {
        "id": vid,
        "fingerprint": p["fingerprint"],
        "scoring": {
            "score": 61,
            "reasoning": "A specific scoring rationale for this vacancy.",
            "short_summary": " ".join(["Фактическое резюме роли и её обязанностей."] * 35),
            "hard_requirements": [],
            "country": "Spain",
            "work_mode": "remote",
            "us_eligibility": "outside_us_ok",
            "deadline": None,
        },
        "screening": _good_result(vid) if p["screening"] is not None else None,
    }
    counts = d.save_results([result], [p], model="haiku")
    assert counts["scored"] == 1, counts
    cur = db.get_conn().cursor()
    cur.execute("SELECT llm_score, status FROM vacancy WHERE id=%s", (vid,))
    assert tuple(cur.fetchone()) == (61, "unseen")
    cur.close()


def test_board_roles_take_the_night_cap_before_company_site_rows(env, monkeypatch):
    """The cap is small; a board role is what /today shows, so it goes first."""
    db, ps = env
    import prepare_discovery as d

    d = importlib.reload(d)
    old_site, new_board = str(uuid.uuid4()), str(uuid.uuid4())
    _seed(db, old_site, score=None)
    _seed(db, new_board, score=None)
    cur = db.get_conn().cursor()
    cur.execute("UPDATE vacancy SET first_seen='2020-01-01' WHERE id=%s", (old_site,))
    cur.execute("UPDATE vacancy SET source_board='Probably Good' WHERE id=%s", (new_board,))
    db.get_conn().commit()
    cur.close()
    monkeypatch.setattr(d, "_cap", lambda limit: 1)
    assert [p["id"] for p in d.select_payloads()] == [new_board]


def test_a_filtered_out_role_is_never_offered_to_the_scorer(env):
    """The geography / profile filter still decides: a row the filter pass
    excluded costs no model call."""
    db, ps = env
    import prepare_discovery as d

    d = importlib.reload(d)
    vid = str(uuid.uuid4())
    _seed(db, vid, score=None)
    cur = db.get_conn().cursor()
    cur.execute("UPDATE vacancy SET scoring_excluded_reason='US-only' WHERE id=%s", (vid,))
    db.get_conn().commit()
    cur.close()
    assert not [p for p in d.select_payloads(limit=20) if p["id"] == vid]


def test_save_accepts_whitespace_but_rejects_changed_body(env):
    db, ps = env
    import prepare_discovery as d

    d = importlib.reload(d)
    vid = str(uuid.uuid4())
    _seed(db, vid, score=None)
    p = next(p for p in d.select_payloads(limit=20) if p["id"] == vid)
    cur = db.get_conn().cursor()
    cur.execute("UPDATE vacancy SET full_description=%s WHERE id=%s", (" " + POSTING, vid))
    db.get_conn().commit()
    cur.close()
    result = {
        "id": vid,
        "fingerprint": p["fingerprint"],
        "scoring": {
            "score": 20,
            "reasoning": "valid rationale",
            "short_summary": " ".join(["Достаточно подробное резюме роли."] * 40),
            "hard_requirements": [],
            "country": "",
            "work_mode": "remote",
            "us_eligibility": "unclear",
            "deadline": None,
        },
        "screening": _good_result(vid),
    }
    summary = d.save_results([result], [p], model="haiku")
    assert summary["skipped"] == 0 and summary["scored"] == 1
    cur = db.get_conn().cursor()
    cur.execute("SELECT llm_score FROM vacancy WHERE id=%s", (vid,))
    assert cur.fetchone()[0] == 20
    cur.close()
    cur = db.get_conn().cursor()
    cur.execute(
        "UPDATE vacancy SET full_description=%s, llm_score=NULL WHERE id=%s",
        ("Changed body " + POSTING, vid),
    )
    db.get_conn().commit()
    cur.close()
    stale = d.save_results([result], [p], model="haiku")
    assert stale["skipped"] >= 1


def test_organization_summary_roundtrip_preserves_existing_description(env):
    db, ps = env
    vid = str(uuid.uuid4())
    _seed(db, vid)
    p = next(p for p in d.select_payloads(limit=20) if p["id"] == vid)
    summary = "Builds software that helps charities coordinate volunteers."
    result = {
        "id": vid,
        "fingerprint": p["fingerprint"],
        "scoring": {
            "score": 60,
            "reasoning": "Relevant programme experience.",
            "short_summary": "A detailed account of the role's responsibilities. " * 6,
            "organization_summary": summary,
        },
        "screening": _good_result(vid),
    }
    assert "organization_summary" in p["scoring"]["system_prompt"]
    invalid = {**result, "scoring": {**result["scoring"], "organization_summary": ["invalid"]}}
    assert d.save_results([invalid], [p])["errors"]
    assert d.save_results([result], [p])["scored"] == 1
    cur = db.get_conn().cursor()
    cur.execute(
        "SELECT description FROM company WHERE id=(SELECT company_id FROM vacancy WHERE id=%s)",
        (vid,),
    )
    assert cur.fetchone()[0] == summary
    # The legacy score saver shares the same company-fill rule and transaction.
    db.update_llm_score(
        vid,
        {
            "llm_score": 60,
            "llm_summary": "Role summary",
            "organization_summary": "Must not overwrite existing research.",
        },
    )
    cur.execute(
        "SELECT description FROM company WHERE id=(SELECT company_id FROM vacancy WHERE id=%s)",
        (vid,),
    )
    assert cur.fetchone()[0] == summary
    cur.execute("UPDATE company SET description=NULL")
    db.update_llm_score(
        vid,
        {
            "llm_score": 60,
            "llm_summary": "Role summary",
            "organization_summary": summary,
        },
    )
    cur.execute(
        "SELECT description FROM company WHERE id=(SELECT company_id FROM vacancy WHERE id=%s)",
        (vid,),
    )
    assert cur.fetchone()[0] == summary
    db.get_conn().rollback()
    cur.close()
