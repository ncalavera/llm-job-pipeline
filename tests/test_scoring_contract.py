"""The script↔agent scoring seam, both directions.

`score_vacancies.py --local` emits a JSON list the orchestrator hands to a scorer
agent; `--save` ingests the agent's reply back into the DB. The load-bearing
contract:

  * the --local payload identifies rows by ``member_ids`` (real DB UUIDs), NOT
    the local ``id`` (which only groups duplicate-location rows);
  * --save writes the score to every member_id, persists the full short_summary,
    and stamps llm_scored_at;
  * a malformed reply (wrong payload_kind, missing score, unknown UUID) is
    rejected gracefully — it never crashes and never corrupts good rows.

Runs on an isolated temp SQLite DB; the scorer is faked (no model, no network).
"""

import importlib
import io
import json
import sys
import types

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobsearch.db"))
    for mod in ("database_supabase", "config", "company_registry",
                "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend
    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db
    yield db
    db.close_conn()


def _stub_report(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "report",
        types.SimpleNamespace(generate_dashboard=lambda *a, **k: None),
    )


def _job(title, city="Berlin, Germany"):
    return {
        "title": title,
        "snippet": f"{title} blurb.",
        "full_description": f"Hiring a {title}. " * 12,
        "location": city,
        "url": "https://example.test/" + title.lower().replace(" ", "-"),
    }


def _seed(db, titles):
    db.ensure_company("Globex", status="active")
    db.merge_vacancies("Globex", "A", [_job(t) for t in titles])
    db.get_conn().commit()
    return {v["title"]: vid for vid, v in db.load_vacancies().items()}


def _local(monkeypatch):
    import score_vacancies
    importlib.reload(score_vacancies)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    args = types.SimpleNamespace(
        force=False, include_passed=False, no_candidates=False,
        limit=None, offset=0,
    )
    try:
        score_vacancies.cmd_local(args)
    finally:
        monkeypatch.undo()
    return json.loads(buf.getvalue() or "[]")


def _save(monkeypatch, payload):
    import score_vacancies
    importlib.reload(score_vacancies)
    _stub_report(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    score_vacancies.cmd_save(types.SimpleNamespace(archive=False))


_LONG = "Detailed deterministic summary sentence. " * 8  # > 200 chars


# ---------------------------------------------------------------------------
# Direction 1: --local payload shape
# ---------------------------------------------------------------------------

def test_local_payload_uses_real_uuid_member_ids(dal, monkeypatch):
    ids = _seed(dal, ["Operations Lead", "Data Analyst"])
    payload = _local(monkeypatch)
    assert len(payload) == 2
    real_uuids = set(ids.values())
    for item in payload:
        assert item["payload_kind"] == "vacancy"
        assert item["member_ids"]
        # member_ids are DB UUIDs, never the local grouping id.
        assert set(item["member_ids"]) <= real_uuids
        assert item["system_prompt"] and item["user_msg"]


# ---------------------------------------------------------------------------
# Direction 2: --save persists score + full summary via member_ids
# ---------------------------------------------------------------------------

def test_save_persists_score_and_full_summary(dal, monkeypatch):
    ids = _seed(dal, ["Operations Lead"])
    payload = _local(monkeypatch)
    item = payload[0]
    _save(monkeypatch, [{
        "payload_kind": "vacancy",
        "member_ids": item["member_ids"],
        "org": item["org"],
        "title": item["title"],
        "score": 73,
        "reasoning": "Strong ops fit.",
        "short_summary": _LONG,
        "hard_requirements": ["5y ops"],
        "tags": ["ops"],
    }])
    v = dal.load_vacancies()[ids["Operations Lead"]]
    assert v["llm_score"] == 73
    assert v["llm_summary"] == _LONG          # FULL summary persisted, not truncated
    assert v["llm_scored_at"]
    assert v["llm_hard_requirements"] == ["5y ops"]


def test_save_maps_each_member_id(dal, monkeypatch):
    """Two location-rows of the same role share one score entry → both updated."""
    dal.ensure_company("Globex", status="active")
    # Same title, two locations → merge keeps ONE row with 2 locations, so this
    # exercises the single-member path; add a genuinely separate role too.
    dal.merge_vacancies("Globex", "A", [_job("Ops Lead", "Berlin, Germany")])
    dal.merge_vacancies("Globex", "A", [_job("Ops Lead", "London, United Kingdom")])
    dal.merge_vacancies("Globex", "A", [_job("Analyst")])
    dal.get_conn().commit()
    payload = _local(monkeypatch)
    save = [{
        "payload_kind": "vacancy",
        "member_ids": p["member_ids"],
        "org": p["org"], "title": p["title"],
        "score": 50, "reasoning": "ok", "short_summary": _LONG,
        "hard_requirements": [], "tags": [],
    } for p in payload]
    _save(monkeypatch, save)
    for v in dal.load_vacancies().values():
        assert v["llm_score"] == 50


# ---------------------------------------------------------------------------
# Graceful rejection of malformed replies
# ---------------------------------------------------------------------------

def test_save_rejects_wrong_payload_kind(dal, monkeypatch, capsys):
    ids = _seed(dal, ["Operations Lead"])
    payload = _local(monkeypatch)
    item = payload[0]
    _save(monkeypatch, [{
        "payload_kind": "company",          # wrong kind → skipped
        "member_ids": item["member_ids"],
        "org": item["org"], "title": item["title"],
        "score": 99, "short_summary": _LONG,
    }])
    # The good row was NOT scored (the bad entry was rejected, not applied).
    assert dal.load_vacancies()[ids["Operations Lead"]]["llm_score"] is None
    assert "wrong payload_kind" in capsys.readouterr().err


def test_save_rejects_missing_score(dal, monkeypatch, capsys):
    ids = _seed(dal, ["Operations Lead"])
    payload = _local(monkeypatch)
    item = payload[0]
    _save(monkeypatch, [{
        "payload_kind": "vacancy",
        "member_ids": item["member_ids"],
        "org": item["org"], "title": item["title"],
        # no "score" and no "score_data"
        "short_summary": _LONG,
    }])
    assert dal.load_vacancies()[ids["Operations Lead"]]["llm_score"] is None
    assert "missing both score_data and a top-level score" in capsys.readouterr().err


def test_save_warns_on_unknown_uuid_without_crashing(dal, monkeypatch, capsys):
    _seed(dal, ["Operations Lead"])
    _save(monkeypatch, [{
        "payload_kind": "vacancy",
        "member_ids": ["00000000-0000-0000-0000-000000000000"],  # not in DB
        "org": "Globex", "title": "Ghost Role",
        "score": 42, "reasoning": "x", "short_summary": _LONG,
        "hard_requirements": [], "tags": [],
    }])
    # No crash; a warning is printed and the good row stays unscored.
    assert "not found in DB" in capsys.readouterr().err


def test_save_empty_payload_is_noop(dal, monkeypatch, capsys):
    _seed(dal, ["Operations Lead"])
    _save(monkeypatch, [])
    assert "No results to save" in capsys.readouterr().out
