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


@pytest.fixture()
def sqlite_dal_migrated(tmp_path, monkeypatch):
    """Like ``sqlite_dal``, but also replays the real ``sql/migrations/*``
    chain — needed for anything that touches a migration-only column
    (``scored_by``, migration 0009, is not folded into the frozen baseline;
    see sql/migrations/README.md)."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    for mod in (
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "migrate",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    import migrate

    importlib.reload(migrate)
    assert migrate.cmd_migrate(allow_destructive=False, do_backup=False) == 0

    import database_supabase as db

    yield db
    db.close_conn()


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


# ---------------------------------------------------------------------------
# cmd_save — score provenance (--scored-by, review fix)
# ---------------------------------------------------------------------------


def test_cmd_save_records_scored_by_when_flag_given(sqlite_dal_migrated, monkeypatch):
    """--scored-by on the CLI stamps every saved score with that model name —
    this is how the driver tells --save which two-pass model just scored the
    batch (see _vacancy_gate_text in scripts/run_daily.py)."""
    db = sqlite_dal_migrated
    vid = _seed_one_vacancy(db)

    monkeypatch.setitem(
        sys.modules,
        "report",
        types.SimpleNamespace(generate_dashboard=lambda *a, **k: None),
    )
    payload = [
        {
            "member_ids": [vid],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score": 55,
            "reasoning": "Cheap first look.",
            "short_summary": "A " * 120,
        }
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    import score_vacancies

    importlib.reload(score_vacancies)
    args = types.SimpleNamespace(archive=False, scored_by="haiku")
    score_vacancies.cmd_save(args)

    assert db.load_vacancies()[vid]["scored_by"] == "haiku"


def test_cmd_save_omitted_scored_by_leaves_column_null(sqlite_dal_migrated, monkeypatch):
    """No --scored-by (e.g. a manual/ad-hoc --save) leaves scored_by unset,
    matching the pre-two-pass behaviour — never a crash, never a fabricated
    provenance."""
    db = sqlite_dal_migrated
    vid = _seed_one_vacancy(db)

    monkeypatch.setitem(
        sys.modules,
        "report",
        types.SimpleNamespace(generate_dashboard=lambda *a, **k: None),
    )
    payload = [
        {
            "member_ids": [vid],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score": 55,
            "reasoning": "No provenance supplied.",
            "short_summary": "A " * 120,
        }
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    import score_vacancies

    importlib.reload(score_vacancies)
    args = types.SimpleNamespace(archive=False)  # no scored_by attribute at all
    score_vacancies.cmd_save(args)

    assert db.load_vacancies()[vid]["scored_by"] is None


def test_cmd_save_escalation_overwrites_screen_scored_by(sqlite_dal_migrated, monkeypatch):
    """The strong pass re-saving the same vacancy overwrites scored_by, exactly
    like it overwrites llm_score — a kept-cheap score never lingers labelled as
    confirmed, and a confirmed score never lingers labelled as cheap."""
    db = sqlite_dal_migrated
    vid = _seed_one_vacancy(db)

    monkeypatch.setitem(
        sys.modules,
        "report",
        types.SimpleNamespace(generate_dashboard=lambda *a, **k: None),
    )
    import score_vacancies

    importlib.reload(score_vacancies)

    screen_payload = [
        {
            "member_ids": [vid],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score": 55,
            "reasoning": "Screen pass.",
            "short_summary": "A " * 120,
        }
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(screen_payload)))
    score_vacancies.cmd_save(types.SimpleNamespace(archive=False, scored_by="haiku"))
    assert db.load_vacancies()[vid]["scored_by"] == "haiku"

    escalate_payload = [
        {
            "member_ids": [vid],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score": 78,
            "reasoning": "Escalation pass.",
            "short_summary": "B " * 120,
        }
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(escalate_payload)))
    score_vacancies.cmd_save(types.SimpleNamespace(archive=False, scored_by="opus"))

    saved = db.load_vacancies()[vid]
    assert saved["scored_by"] == "opus"
    assert saved["llm_score"] == 78


# ---------------------------------------------------------------------------
# cmd_save — score type/range validation
# ---------------------------------------------------------------------------


def _save_flat(db, monkeypatch, vid, score):
    monkeypatch.setitem(
        sys.modules, "report", types.SimpleNamespace(generate_dashboard=lambda *a, **k: None)
    )
    payload = [
        {
            "member_ids": [vid],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score": score,
            "reasoning": "r",
            "short_summary": "A " * 120,
        }
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    import score_vacancies

    importlib.reload(score_vacancies)
    score_vacancies.cmd_save(types.SimpleNamespace(archive=False))


@pytest.mark.parametrize("bad", [999, -5, 3.7])
def test_cmd_save_rejects_out_of_range_or_fractional_score(sqlite_dal, monkeypatch, bad):
    """A bare-LLM slip (999 / -5 / 3.7) must NOT reach the DB — and from there
    public/data.js — verbatim. The entry is skipped, the row stays unscored."""
    db = sqlite_dal
    vid = _seed_one_vacancy(db)
    _save_flat(db, monkeypatch, vid, bad)
    assert db.load_vacancies()[vid]["llm_score"] is None


def test_cmd_save_accepts_integer_valued_float(sqlite_dal, monkeypatch):
    """An in-range whole-number float (85.0) is coerced to the int 85, not
    rejected — the agent occasionally emits ``85.0``."""
    db = sqlite_dal
    vid = _seed_one_vacancy(db)
    _save_flat(db, monkeypatch, vid, 85.0)
    assert db.load_vacancies()[vid]["llm_score"] == 85


@pytest.mark.parametrize("bad", [999, -5, 3.7])
def test_cmd_save_rejects_bad_score_via_strict_score_data(sqlite_dal, monkeypatch, bad):
    """The strict pre-built ``score_data`` shape goes through the same
    validation as the flat shape — a slip can't sneak in via that path."""
    db = sqlite_dal
    vid = _seed_one_vacancy(db)
    monkeypatch.setitem(
        sys.modules, "report", types.SimpleNamespace(generate_dashboard=lambda *a, **k: None)
    )
    payload = [
        {
            "member_ids": [vid],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score_data": {"llm_score": bad, "llm_reasoning": "r", "llm_summary": "s"},
        }
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    import score_vacancies

    importlib.reload(score_vacancies)
    score_vacancies.cmd_save(types.SimpleNamespace(archive=False, scored_by=None))
    assert db.load_vacancies()[vid]["llm_score"] is None


def test_coerce_score_unit():
    import score_vacancies

    importlib.reload(score_vacancies)
    c = score_vacancies._coerce_score
    assert c(0) == 0 and c(100) == 100 and c(73) == 73
    assert c(85.0) == 85 and c("42") == 42 and c(" 7 ") == 7
    assert c(999) is None and c(-1) is None and c(3.7) is None
    assert c("high") is None and c(None) is None and c(True) is None


# ---------------------------------------------------------------------------
# cmd_save — empty member_ids is a skipped error, never a silent success
# (verified NOT a bug in a stress sweep; this locks the guard in)
# ---------------------------------------------------------------------------


def test_cmd_save_empty_member_ids_is_error_not_saved(sqlite_dal, monkeypatch, capsys):
    db = sqlite_dal
    vid = _seed_one_vacancy(db)
    monkeypatch.setitem(
        sys.modules, "report", types.SimpleNamespace(generate_dashboard=lambda *a, **k: None)
    )
    payload = [
        {
            "member_ids": [],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score": 80,
            "reasoning": "r",
            "short_summary": "A " * 120,
        }
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    import score_vacancies

    importlib.reload(score_vacancies)
    score_vacancies.cmd_save(types.SimpleNamespace(archive=False))

    out = capsys.readouterr()
    assert db.load_vacancies()[vid]["llm_score"] is None  # 0 rows touched
    assert "Saved 0 scores" in out.out  # NOT counted as a successful save
    assert "missing member_ids" in out.err
