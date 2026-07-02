"""Tests for the company scoring flow (scripts/score_companies.py).

Mirrors test_score_vacancies.py: the external boundary (the LLM) is replaced by
a plain Python dict standing in for a parsed model response, and we assert the
real scoring→persistence logic writes the company's mission_fit / alignment /
tier fields. Fully offline on the local SQLite backend.

What's exercised:
  * _parse_json / _extract_enrichment — the pure parse + normalize step that
    turns a raw LLM response into the enrichment dict the DAL persists.
  * save_company_enrichment — the persistence the --api backend calls, asserted
    to round-trip alignment_score, mission_fit and tier.

Note on cmd_save: score_companies.cmd_save now imports ``Json`` from
``db_backend`` (the backend-aware shim), so its parameter binding works on both
the SQLite and Postgres backends. See test_cmd_save_persists_on_sqlite.
"""

import importlib
import sys

import pytest


@pytest.fixture()
def sc(tmp_path, monkeypatch):
    """SQLite-backed DAL + a fresh score_companies module bound to a temp DB."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    for mod in (
        "score_companies",
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
    import score_companies

    importlib.reload(score_companies)

    ns = type("ScEnv", (), {})()
    ns.dal = dal
    ns.mod = score_companies
    yield ns
    dal.close_conn()


# ---------------------------------------------------------------------------
# _parse_json — LLM response extraction (the external-boundary parse step)
# ---------------------------------------------------------------------------


def test_parse_json_plain(sc):
    out = sc.mod._parse_json('{"alignment_score": 70}')
    assert out["alignment_score"] == 70


def test_parse_json_fenced(sc):
    out = sc.mod._parse_json('```json\n{"alignment_score": 55}\n```')
    assert out["alignment_score"] == 55


def test_parse_json_nested_with_preamble(sc):
    text = 'Analysis:\n{"about": {"description": "x"}, "mission_fit": {"alignment_score": 80}}'
    out = sc.mod._parse_json(text)
    assert out["mission_fit"]["alignment_score"] == 80


def test_parse_json_invalid_returns_error(sc):
    out = sc.mod._parse_json("not json")
    assert "error" in out and "raw" in out


# ---------------------------------------------------------------------------
# _extract_enrichment — normalize a parsed LLM result into an enrichment dict
# ---------------------------------------------------------------------------


def test_extract_enrichment_from_mission_fit(sc):
    result = {
        "about": {"description": "A robotics nonprofit", "sector": "Robotics"},
        "mission_fit": {
            "alignment_score": 72,
            "alignment_label": "strong",
            "strengths": ["mission overlap"],
            "risks": ["early stage"],
        },
    }
    enr = sc.mod._extract_enrichment(result)
    assert enr is not None
    assert enr["alignment_score"] == 72
    assert enr["mission_fit"]["alignment_label"] == "strong"
    assert enr["mission_fit"]["strengths"] == ["mission overlap"]
    # about carries the enrichment provenance stamp.
    assert enr["about"]["about_source"] == "llm_from_markdown"


def test_extract_enrichment_top_level_alignment_fallback(sc):
    """alignment_score at the top level (not nested) is still picked up."""
    result = {"alignment_score": 40, "mission_fit": {}}
    enr = sc.mod._extract_enrichment(result)
    assert enr is not None
    assert enr["alignment_score"] == 40


def test_extract_enrichment_missing_alignment_is_none(sc):
    assert sc.mod._extract_enrichment({"about": {"description": "x"}}) is None


def test_extract_enrichment_out_of_range_is_none(sc):
    assert sc.mod._extract_enrichment({"alignment_score": 250}) is None
    assert sc.mod._extract_enrichment({"alignment_score": -5}) is None


# ---------------------------------------------------------------------------
# Scoring → persistence: parse a (fake) LLM result and persist via the DAL,
# exactly as the --api backend does. Asserts mission_fit / alignment land.
# ---------------------------------------------------------------------------


def test_score_result_persists_to_company_row(sc):
    db = sc.dal
    db.ensure_company("Acme Robotics", status="candidate")
    db.get_conn().commit()

    # Stand-in for a parsed model response (no network, no LLM).
    fake_llm_result = sc.mod._parse_json(
        '{"about": {"description": "Robotics for good", "sector": "Robotics"},'
        ' "mission_fit": {"alignment_score": 78, "alignment_label": "strong",'
        ' "strengths": ["impact"], "approach": "apply directly"}}'
    )
    enr = sc.mod._extract_enrichment(fake_llm_result)
    assert enr is not None

    # Persist through the same boundary cmd_api uses.
    db.save_company_enrichment(
        "Acme Robotics",
        about=enr["about"],
        mission_fit=enr["mission_fit"],
        alignment_score=enr["alignment_score"],
    )
    db.get_conn().commit()

    saved = db.load_company_enrichment("Acme Robotics")
    assert saved["alignment_score"] == 78
    assert saved["mission_fit"]["alignment_label"] == "strong"
    assert saved["mission_fit"]["strengths"] == ["impact"]
    assert saved["about"]["description"] == "Robotics for good"


def test_calculate_company_tier_assigns_letter(sc):
    """A high alignment score maps to a tier letter via the shared helper."""
    tier, composite = sc.dal.calculate_company_tier(95)
    assert tier in {"S", "A", "B", "C"}
    assert composite == 95.0
    # None alignment → no tier.
    assert sc.dal.calculate_company_tier(None) == (None, None)


# ---------------------------------------------------------------------------
# cmd_save persists on the SQLite backend via db_backend's Json shim.
# ---------------------------------------------------------------------------


def test_cmd_save_persists_on_sqlite(sc):
    """score_companies.cmd_save binds JSON columns with db_backend's Json shim,
    which the SQLite cursor serializes, so the full stdin→DB save path persists
    on the SQLite backend (previously a Postgres-only path that silently no-op'd
    because it used the real psycopg2 Json)."""
    import io
    import json
    import types

    db = sc.dal
    cid = db.ensure_company("Acme Robotics", status="candidate")
    db.get_conn().commit()

    payload = [
        {
            "payload_kind": "company",
            "id": str(cid),
            "canonical_name": "Acme Robotics",
            "enrichment": {
                "about": {"description": "x", "sector": "Robotics"},
                "mission_fit": {"alignment_score": 70, "alignment_label": "ok"},
                "alignment_score": 70,
            },
        }
    ]
    monkeypatch_stdin = io.StringIO(json.dumps(payload))
    old_stdin = sys.stdin
    sys.stdin = monkeypatch_stdin
    try:
        sc.mod.cmd_save(types.SimpleNamespace(no_auto_review=True))
    finally:
        sys.stdin = old_stdin

    saved = db.load_company_enrichment("Acme Robotics")
    assert saved["alignment_score"] == 70
    assert saved["mission_fit"]["alignment_label"] == "ok"
    assert saved["about"]["description"] == "x"
    assert saved["about"]["about_source"] == "llm_subagent"
