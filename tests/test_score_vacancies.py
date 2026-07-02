"""Tests for _sanitize_text, _parse_json."""

import importlib
import io
import json
import sys
import types

import pytest
from score_vacancies import _sanitize_text, _parse_json


# ---------------------------------------------------------------------------
# _sanitize_text
# ---------------------------------------------------------------------------


def test_ST01_crlf_normalized():
    result = _sanitize_text("line1\r\nline2")
    assert result == "line1\nline2"


def test_ST02_nbsp_replaced():
    result = _sanitize_text("hello\xa0world")
    assert result == "hello world"


def test_ST03_control_chars_removed():
    result = _sanitize_text("\x01 text")
    assert result == " text"


def test_ST04_normal_text_unchanged():
    text = "Hello World\nWith newline\tand tab"
    assert _sanitize_text(text) == text


# ---------------------------------------------------------------------------
# _parse_json
# ---------------------------------------------------------------------------


def test_PJ01_plain_valid_json():
    result = _parse_json('{"score": 75, "reasoning": "Good match"}')
    assert result["score"] == 75
    assert result["reasoning"] == "Good match"


def test_PJ02_fenced_json_block():
    text = '```json\n{"score": 60, "reasoning": "Partial"}\n```'
    result = _parse_json(text)
    assert result["score"] == 60


def test_PJ03_preamble_before_json():
    text = 'Here is my analysis:\n\n{"score": 80, "reasoning": "Strong fit"}'
    result = _parse_json(text)
    # Should parse the valid JSON object from the text
    # (plain json.loads fails, brace_match regex extracts it)
    assert "score" in result


def test_PJ04_invalid_returns_error_dict():
    result = _parse_json("this is not json at all")
    assert "error" in result
    assert "raw" in result


# ---------------------------------------------------------------------------
# cmd_save — accepts the documented FLAT shape (finding #2)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_dal(tmp_path, monkeypatch):
    """Fresh SQLite-backed DAL on an isolated temp DB (no Supabase)."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    import database_supabase as db

    yield db
    db.close_conn()


def _seed_one_vacancy(db):
    db.ensure_company("Acme Robotics", status="active")
    db.save_vacancies(
        "Acme Robotics",
        "A",
        [
            {
                "title": "Head of Community",
                "snippet": "Lead community efforts.",
                "full_description": "Lead our global community programme. " * 8,
                "location": "Berlin, Germany",
                "url": "https://acme.example/job/hoc",
            }
        ],
    )
    db.get_conn().commit()
    return next(iter(db.load_vacancies()))


def test_cmd_save_accepts_flat_documented_shape(sqlite_dal, monkeypatch):
    """The flat shape from AGENTS.md / jobs-score.md must persist correctly.

    Docs tell agents to emit {score, reasoning, tags, hard_requirements,
    short_summary} + member_ids — NOT a pre-built nested score_data. cmd_save
    must build score_data via _make_score_data and write llm_score/llm_summary.
    """
    db = sqlite_dal
    vid = _seed_one_vacancy(db)

    # Stub out the dashboard regeneration (writes files / needs report assets).
    monkeypatch.setitem(
        sys.modules,
        "report",
        types.SimpleNamespace(generate_dashboard=lambda *a, **k: None),
    )

    # The documented FLAT payload — no payload_kind, no nested score_data.
    payload = [
        {
            "member_ids": [vid],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score": 78,
            "reasoning": "Strong fit on community + ops leadership.",
            "tags": ["community", "operations"],
            "hard_requirements": ["5y community leadership"],
            "short_summary": "A " * 120,  # long enough to clear the 200-char warning
        }
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    import score_vacancies

    importlib.reload(score_vacancies)
    args = types.SimpleNamespace(archive=False)
    score_vacancies.cmd_save(args)

    saved = db.load_vacancies()[vid]
    assert saved["llm_score"] == 78
    assert saved["llm_summary"].strip().startswith("A")
    assert saved["llm_hard_requirements"] == ["5y community leadership"]


def test_cmd_save_still_accepts_strict_nested_shape(sqlite_dal, monkeypatch):
    """Backward compat: the strict pre-built score_data shape still works."""
    db = sqlite_dal
    vid = _seed_one_vacancy(db)

    monkeypatch.setitem(
        sys.modules,
        "report",
        types.SimpleNamespace(generate_dashboard=lambda *a, **k: None),
    )
    payload = [
        {
            "payload_kind": "vacancy",
            "member_ids": [vid],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score_data": {
                "llm_score": 64,
                "llm_reasoning": "Solid.",
                "llm_summary": "B " * 120,
                "llm_hard_requirements": [],
            },
        }
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    import score_vacancies

    importlib.reload(score_vacancies)
    args = types.SimpleNamespace(archive=False)
    score_vacancies.cmd_save(args)

    assert db.load_vacancies()[vid]["llm_score"] == 64
