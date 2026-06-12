"""End-to-end DAL tests on the local SQLite backend.

These exercise the real data-access layer (database_supabase) against a
throwaway SQLite file — no Supabase, no psycopg2. They lock the easy-mode
contract: every public DAL function works identically on SQLite.

The module forces the SQLite backend by clearing SUPABASE_DB_URL and pointing
JOBSEARCH_DB_PATH at a temp file BEFORE importing the DAL, then reloads the
backend/registry/DAL chain so module-level state picks up the env.
"""

import importlib
import os
import sys

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    """Fresh SQLite-backed DAL bound to an isolated temp database."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    # Reload the connection/registry/DAL chain so IS_SQLITE and the singleton
    # connection are rebuilt for this temp DB.
    for mod in ("database_supabase", "config", "company_registry",
                "db_conn", "db_backend"):
        sys.modules.pop(mod, None)

    import db_backend
    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"

    import database_supabase as db
    yield db

    db.close_conn()


def _commit(db):
    db.get_conn().commit()


def _fake_job(title="Head of Community", city="Berlin, Germany"):
    return {
        "title": title,
        "snippet": "Lead community efforts.",
        "full_description": "Lead our global community programme. " * 8,
        "location": city,
        "url": "https://example.org/job/" + title.lower().replace(" ", "-"),
    }


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def test_schema_autocreated(dal):
    """First connection creates the three tables with zero manual steps."""
    cur = dal.get_conn().cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {r[0] for r in cur.fetchall()}
    cur.close()
    assert {"company", "vacancy", "archived_hash"} <= tables


# ---------------------------------------------------------------------------
# Company ensure / resolve
# ---------------------------------------------------------------------------

def test_ensure_and_resolve_company(dal):
    cid = dal.ensure_company("Acme Robotics", status="active")
    assert cid
    # Idempotent: same name resolves to the same id.
    assert str(dal.resolve_company_id("Acme Robotics")) == str(cid)
    assert str(dal.ensure_company("Acme Robotics")) == str(cid)


def test_resolve_unknown_company_is_none(dal):
    assert dal.resolve_company_id("Nonexistent Co") is None


# ---------------------------------------------------------------------------
# Merge + load
# ---------------------------------------------------------------------------

def test_merge_then_load(dal):
    dal.ensure_company("Acme Robotics", status="active")
    new = dal.merge_vacancies("Acme Robotics", "A", [_fake_job()])
    _commit(dal)
    assert new == 1

    vacs = dal.load_vacancies()
    assert len(vacs) == 1
    v = next(iter(vacs.values()))
    assert v["title"] == "Head of Community"
    assert v["org"] == "Acme Robotics"
    # JSONB locations decoded back to a list of dicts.
    assert isinstance(v["locations"], list)
    assert v["locations"][0]["city"] == "Berlin"


def test_merge_is_idempotent_by_dedup_hash(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.merge_vacancies("Acme Robotics", "A", [_fake_job()])
    _commit(dal)
    again = dal.merge_vacancies("Acme Robotics", "A", [_fake_job()])
    _commit(dal)
    assert again == 0
    assert len(dal.load_vacancies()) == 1


def test_merge_second_location_merges_into_one_row(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.merge_vacancies("Acme Robotics", "A", [_fake_job(city="Berlin, Germany")])
    dal.merge_vacancies("Acme Robotics", "A", [_fake_job(city="London, UK")])
    _commit(dal)
    vacs = dal.load_vacancies()
    assert len(vacs) == 1
    cities = {loc.get("city") for v in vacs.values() for loc in v["locations"]}
    assert {"Berlin", "London"} <= cities


# ---------------------------------------------------------------------------
# Status updates
# ---------------------------------------------------------------------------

def test_status_update_and_readback(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.merge_vacancies("Acme Robotics", "A", [_fake_job()])
    _commit(dal)
    vid = next(iter(dal.load_vacancies()))

    dal.update_vacancy_status(vid, "liked")
    _commit(dal)
    assert dal.get_vacancy_statuses()[vid] == "liked"

    dal.batch_update_statuses({vid: "to_apply"})
    _commit(dal)
    assert dal.get_vacancy_statuses()[vid] == "to_apply"

    # Protected = any non-unseen status.
    assert vid in dal.get_protected_ids()


def test_scoring_roundtrip(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.merge_vacancies("Acme Robotics", "A", [_fake_job()])
    _commit(dal)
    vid = next(iter(dal.load_vacancies()))

    dal.update_llm_score(vid, {
        "llm_score": 72,
        "llm_reasoning": "strong fit",
        "llm_summary": "Great match for the role.",
        "llm_hard_requirements": ["5y experience"],
    })
    _commit(dal)
    v = dal.load_vacancies()[vid]
    assert v["llm_score"] == 72
    assert v["llm_hard_requirements"] == ["5y experience"]


# ---------------------------------------------------------------------------
# Archived flows
# ---------------------------------------------------------------------------

def test_archived_hash_roundtrip(dal):
    dal.record_archived_hashes([("abc123", "score_below_threshold")])
    _commit(dal)
    assert "abc123" in dal.get_archived_hashes()


def test_gone_from_source_archives_unseen(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.merge_vacancies("Acme Robotics", "A", [
        _fake_job(title="Head of Community"),
        _fake_job(title="Programme Lead"),
    ])
    _commit(dal)
    # Fresh listing only contains one of the two roles → the other is gone.
    archived = dal.archive_gone_vacancies(
        "Acme Robotics", [{"title": "Head of Community"}]
    )
    _commit(dal)
    assert archived == 1
    # The gone role is now archived; the live catalog (archived excluded) shows
    # only the surviving role.
    statuses = {v["title"]: v["status"] for v in dal.load_vacancies().values()}
    assert statuses["Programme Lead"] == "archived"
    live = {t for t, s in statuses.items() if s != "archived"}
    assert live == {"Head of Community"}


def test_delete_vacancies_by_uuid_list(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.merge_vacancies("Acme Robotics", "A", [
        _fake_job(title="Role One"),
        _fake_job(title="Role Two"),
    ])
    _commit(dal)
    ids = list(dal.load_vacancies().keys())
    deleted = dal.delete_vacancies(ids[:1])
    _commit(dal)
    assert deleted == 1
    assert len(dal.load_vacancies()) == 1


# ---------------------------------------------------------------------------
# Candidate companies + auto-review
# ---------------------------------------------------------------------------

def test_auto_review_candidates(dal):
    dal.ensure_company("Highfit Inc", status="candidate")
    dal.save_company_enrichment("Highfit Inc", alignment_score=80)
    dal.ensure_company("Lowfit Inc", status="candidate")
    dal.save_company_enrichment("Lowfit Inc", alignment_score=10)
    _commit(dal)

    result = dal.auto_review_candidates(approve_threshold=60, reject_threshold=25)
    assert "Highfit Inc" in result["approved"]
    assert "Lowfit Inc" in result["rejected"]


def test_candidate_company_vacancies_hidden_until_active(dal):
    dal.ensure_company("Pending Co", status="candidate")
    dal.merge_vacancies("Pending Co", "B", [_fake_job(title="Mystery Role")])
    _commit(dal)
    # Default load shows only active companies.
    assert dal.load_vacancies() == {}
    # Candidate-inclusive load surfaces it.
    assert len(dal.load_vacancies(include_candidate_companies=True)) == 1


# ---------------------------------------------------------------------------
# Enrichment + fitness map
# ---------------------------------------------------------------------------

def test_enrichment_roundtrip(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_company_enrichment(
        "Acme Robotics",
        about={"hq_location": "Berlin"},
        mission_fit={"summary": "aligned"},
        alignment_score=70,
    )
    _commit(dal)
    enr = dal.load_company_enrichment("Acme Robotics")
    assert enr["about"]["hq_location"] == "Berlin"
    assert enr["alignment_score"] == 70

    fitness = dal.get_company_fitness_map()
    assert fitness["Acme Robotics"]["status"] == "active"


# ---------------------------------------------------------------------------
# Validation + expiry
# ---------------------------------------------------------------------------

def test_validate_db_clean(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.merge_vacancies("Acme Robotics", "A", [_fake_job()])
    _commit(dal)
    assert dal.validate_db() == []


def test_pass_expired_vacancies(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.merge_vacancies("Acme Robotics", "A", [_fake_job()])
    _commit(dal)
    vid = next(iter(dal.load_vacancies()))
    # Force a past deadline on the unseen vacancy.
    dal.update_vacancy_fields(vid, deadline="2000-01-01")
    _commit(dal)
    n = dal.pass_expired_vacancies()
    _commit(dal)
    assert n == 1
    assert dal.get_vacancy_statuses()[vid] == "passed"
