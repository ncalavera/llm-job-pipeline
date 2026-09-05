"""Screening preparation (scripts/prepare_screening.py, screening preparation).

Deterministic: cohort selection is a pure function, validation is checked
against a posting text, and the save path runs against a migrated temp
SQLite DB. No model, no network, invented orgs only.
"""

import importlib
import json
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

MIGRATIONS = Path(__file__).resolve().parent.parent / "sql" / "migrations"

POSTING = (
    "We are hiring a Programme Manager.\n"
    "Fluent Spanish is required for this role.\n"
    "Experience with grant management is preferred.\n"
    "The role is based in Madrid; hybrid working is possible.\n"
) * 8  # long enough to pass the real-description floor


def _force_sqlite(monkeypatch, db_file):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db

    return db


@pytest.fixture()
def ps():
    sys.modules.pop("prepare_screening", None)
    import prepare_screening

    return importlib.reload(prepare_screening)


def _good_result(vac_id):
    return {
        "id": vac_id,
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
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_valid_result_keeps_required_vs_preferred(ps):
    clean, reason = ps.validate_result(_good_result("x"), POSTING)
    assert reason is None
    strengths = [r["strength"] for r in clean["posting_facts"]["requirements"]]
    assert strengths == ["required", "preferred"]
    assert clean["profile_comparison"][0]["finding"] == "possible_conflict"
    assert clean["unknowns"] == ["salary"]


def test_quote_not_in_posting_fails_the_result(ps):
    bad = _good_result("x")
    bad["posting_facts"]["requirements"][0]["quote"] = "Native Spanish is mandatory."
    clean, reason = ps.validate_result(bad, POSTING)
    assert clean is None and "quote not found" in reason


def test_quote_matching_ignores_whitespace_and_case(ps):
    ok = _good_result("x")
    ok["posting_facts"]["requirements"][0]["quote"] = "fluent   spanish is REQUIRED for this role."
    clean, reason = ps.validate_result(ok, POSTING)
    assert reason is None


def test_bad_enum_and_missing_facts_fail(ps):
    bad = _good_result("x")
    bad["posting_facts"]["work_mode"] = "office"
    assert ps.validate_result(bad, POSTING)[1].startswith("work_mode=")
    assert ps.validate_result({"id": "x"}, POSTING)[1] == "posting_facts missing"
    assert "subagent failed" in ps.validate_result({"id": "x", "failed": "no text"}, POSTING)[1]


def test_comparison_index_out_of_range_fails(ps):
    bad = _good_result("x")
    bad["profile_comparison"][0]["requirement"] = 7
    assert "out of range" in ps.validate_result(bad, POSTING)[1]


# ---------------------------------------------------------------------------
# Cohort selection
# ---------------------------------------------------------------------------


def test_pick_cohort_is_round_robin_across_strata(ps):
    rows = []
    for i in range(6):
        rows.append({"llm_score": 5, "company_status": "active", "first_seen": f"2026-01-0{i + 1}"})
    for i in range(6):
        rows.append(
            {"llm_score": None, "company_status": "candidate", "first_seen": f"2026-02-0{i + 1}"}
        )
    rows.append({"llm_score": 38, "company_status": "active", "first_seen": "2026-03-01"})
    picked = ps.pick_cohort(rows, 5)
    keys = [(ps._score_band(r["llm_score"]), r["company_status"]) for r in picked]
    # Three strata present -> the first five picks touch every stratum before any repeats.
    assert len(set(keys[:3])) == 3
    assert len(picked) == 5
    # Oldest first inside a stratum.
    active_low = [r for r in picked if r["llm_score"] == 5]
    assert active_low[0]["first_seen"] == "2026-01-01"


def test_eligible_skips_ready_unchanged_and_short_descriptions(ps):
    ready = {
        "full_description": POSTING,
        "screening_state": "ready",
        "screening_fingerprint": ps.fingerprint(POSTING),
    }
    assert not ps.eligible(ready)
    changed = dict(ready, screening_fingerprint="old:old")
    assert ps.eligible(changed)
    failed = dict(ready, screening_state="failed")
    assert ps.eligible(failed)
    assert not ps.eligible({"full_description": "Short.", "screening_state": None})


# ---------------------------------------------------------------------------
# Save path on a migrated SQLite DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import sqlite3

    db = _force_sqlite(monkeypatch, tmp_path / "prep.db")
    db.get_conn().commit()  # baseline schema is created on first connect
    raw = sqlite3.connect(tmp_path / "prep.db")
    for name in ("0025_add_vacancy_scoring_excluded_reason", "0027_add_vacancy_screening"):
        raw.executescript((MIGRATIONS / f"{name}.sqlite.sql").read_text(encoding="utf-8"))
    raw.commit()
    raw.close()
    sys.modules.pop("prepare_screening", None)
    import prepare_screening as ps

    yield db, importlib.reload(ps)
    db.close_conn()


def _seed(db, vac_id, desc=POSTING, company_status="candidate"):
    conn = db.get_conn()
    cur = conn.cursor()
    cid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO company (id, canonical_name, status) VALUES (%s, %s, %s)",
        (cid, f"Org {vac_id[:4]}", company_status),
    )
    cur.execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, full_description, status, "
        "first_seen, last_seen) VALUES (%s, %s, %s, %s, %s, 'unseen', '2026-01-01', '2026-01-01')",
        (vac_id, f"h-{vac_id}", cid, "Programme Manager", desc),
    )
    conn.commit()
    cur.close()


def test_save_writes_ready_and_failed_rows_and_never_a_status(env, tmp_path):
    db, ps = env
    good_id, bad_id = str(uuid.uuid4()), str(uuid.uuid4())
    _seed(db, good_id)
    _seed(db, bad_id)
    bad = _good_result(bad_id)
    bad["posting_facts"]["requirements"][0]["quote"] = "not in the posting"
    files = []
    for i, res in enumerate((_good_result(good_id), bad)):
        p = tmp_path / f"{i:03d}.json"
        p.write_text(json.dumps(res), encoding="utf-8")
        files.append(str(p))

    from types import SimpleNamespace

    ps.cmd_save(SimpleNamespace(files=files, prepared_by="opus"))
    cur = db.get_conn().cursor()
    cur.execute("SELECT id, screening_state, screening, screening_fingerprint, status FROM vacancy")
    rows = {r[0]: r for r in cur.fetchall()}
    cur.close()
    assert rows[good_id][1] == "ready" and rows[good_id][4] == "unseen"
    body = json.loads(rows[good_id][2])
    assert (
        body["model"] == "opus"
        and body["posting_facts"]["requirements"][0]["strength"] == "required"
    )
    assert rows[good_id][3] == ps.fingerprint(POSTING)
    assert (
        rows[bad_id][1] == "failed" and "quote not found" in json.loads(rows[bad_id][2])["failed"]
    )


def test_local_pool_excludes_inactive_and_prepared_rows(env, capsys):
    db, ps = env
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    _seed(db, a, company_status="candidate")
    _seed(db, b, company_status="inactive")
    _seed(db, c, company_status="active")
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE vacancy SET screening_state = 'ready', screening_fingerprint = %s WHERE id = %s",
        (ps.fingerprint(POSTING), c),
    )
    conn.commit()
    cur.close()

    class Args:
        limit = 10

    ps.cmd_local(Args)
    out = json.loads(capsys.readouterr().out)
    assert [p["id"] for p in out] == [a]
    assert out[0]["payload_kind"] == "screening" and "Fluent Spanish" in out[0]["user_msg"]
