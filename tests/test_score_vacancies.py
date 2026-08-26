"""Tests for score_vacancies.py: _sanitize_text, _parse_json, cmd_save/cmd_local
persistence and validation, and the script<->agent scoring contract.

Absorbed tests/test_scoring_contract.py (the --local / --save seam: real-UUID
member_ids, full-summary persistence, graceful rejection of malformed replies).
"""

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


@pytest.mark.parametrize("bad", [999, 3.7])
def test_cmd_save_rejects_out_of_range_or_fractional_score(sqlite_dal, monkeypatch, bad):
    """A bare-LLM slip (999 / 3.7) must NOT reach the DB — and from there
    public/data.js — verbatim. The entry is skipped, the row stays unscored.

    (The ``-5`` case was dropped 2026-08: test_coerce_score_unit already pins
    the pure function's boundary for a negative value; this end-to-end test
    only needs one out-of-range and one fractional example.)"""
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


def test_cmd_save_rejects_bad_score_via_strict_score_data(sqlite_dal, monkeypatch):
    """The strict pre-built ``score_data`` shape goes through the same
    validation as the flat shape — a slip can't sneak in via that path.

    (Only the ``999`` case is kept 2026-08: test_coerce_score_unit already
    pins the pure function's boundary for high, low and fractional values;
    this DB-level test only needs to prove one out-of-range value is
    rejected end to end via this second code path.)"""
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
            "score_data": {"llm_score": 999, "llm_reasoning": "r", "llm_summary": "s"},
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


# ---------------------------------------------------------------------------
# cmd_save — BUG-5: one malformed result must not kill the whole batch
# ---------------------------------------------------------------------------


def _write_flat_result_file(path, vid, score, title="Head of Community"):
    path.write_text(
        json.dumps(
            {
                "member_ids": [vid],
                "org": "Acme Robotics",
                "title": title,
                "score": score,
                "reasoning": "r",
                "short_summary": "A " * 120,
            }
        ),
        encoding="utf-8",
    )


def test_cmd_save_files_mode_skips_malformed_and_saves_rest(
    sqlite_dal, monkeypatch, tmp_path, capsys
):
    """--files reads each subagent result file independently: one malformed
    file (truncated by a kill / unescaped quote) is named and skipped, the
    rest still save — no all-or-nothing failure."""
    db = sqlite_dal
    vid = _seed_one_vacancy(db)
    monkeypatch.setitem(
        sys.modules, "report", types.SimpleNamespace(generate_dashboard=lambda *a, **k: None)
    )

    good = tmp_path / "c1.json"
    _write_flat_result_file(good, vid, 78)
    bad = tmp_path / "c2.json"
    bad.write_text('{"member_ids": ["x", "score": 40}', encoding="utf-8")  # malformed

    import score_vacancies

    importlib.reload(score_vacancies)
    score_vacancies.cmd_save(types.SimpleNamespace(archive=False, files=[str(good), str(bad)]))

    saved = db.load_vacancies()[vid]
    assert saved["llm_score"] == 78  # the good file saved despite the bad one

    out = capsys.readouterr()
    assert "c2.json" in out.err  # bad file named
    assert "malformed" in out.err.lower()
    assert "Skipped 1 malformed file" in out.out  # summary lists it


def test_cmd_save_files_mode_truncated_json_is_reported_not_crashed(
    sqlite_dal, monkeypatch, tmp_path, capsys
):
    """A file truncated mid-write (e.g. by a spend-limit kill) fails its own
    parse, is reported by name, and does not raise out of cmd_save."""
    db = sqlite_dal
    vid = _seed_one_vacancy(db)
    monkeypatch.setitem(
        sys.modules, "report", types.SimpleNamespace(generate_dashboard=lambda *a, **k: None)
    )

    good = tmp_path / "c1.json"
    _write_flat_result_file(good, vid, 60)
    truncated = tmp_path / "c39.json"
    truncated.write_text('{"member_ids": ["x"], "score": 4', encoding="utf-8")  # cut mid-write

    import score_vacancies

    importlib.reload(score_vacancies)
    # Must not raise.
    score_vacancies.cmd_save(
        types.SimpleNamespace(archive=False, files=[str(good), str(truncated)])
    )

    assert db.load_vacancies()[vid]["llm_score"] == 60
    out = capsys.readouterr()
    assert "c39.json" in out.err


def test_cmd_save_stdin_invalid_json_reports_error_not_crash(sqlite_dal, monkeypatch, capsys):
    """Without --files, stdin must still be one valid JSON blob (can't split a
    corrupted array after the fact) — but a parse failure there is reported
    cleanly, not an unhandled traceback."""
    monkeypatch.setattr(sys, "stdin", io.StringIO('[{"member_ids": ["x", "score": 1}]'))
    import score_vacancies

    importlib.reload(score_vacancies)
    score_vacancies.cmd_save(types.SimpleNamespace(archive=False))  # no 'files' attr, no crash

    out = capsys.readouterr()
    assert "not valid JSON" in out.err
    assert "--files" in out.err


# ===========================================================================
# --- from test_scoring_contract.py ---
#
# The script<->agent scoring seam, both directions.
#
# `score_vacancies.py --local` emits a JSON list the orchestrator hands to a
# scorer agent; `--save` ingests the agent's reply back into the DB. The
# load-bearing contract:
#
#   * the --local payload identifies rows by ``member_ids`` (real DB UUIDs),
#     NOT the local ``id`` (which only groups duplicate-location rows);
#   * --save writes the score to every member_id, persists the full
#     short_summary, and stamps llm_scored_at;
#   * a malformed reply (wrong payload_kind, missing score, unknown UUID) is
#     rejected gracefully — it never crashes and never corrupts good rows.
#
# Runs on an isolated temp SQLite DB; the scorer is faked (no model, no
# network). Reuses the ``sqlite_dal`` fixture above (was ``dal`` in the
# source file — genuinely the same setup, repointed here).
# ===========================================================================


def _stub_report(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "report",
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
    db.save_vacancies("Globex", "A", [_job(t) for t in titles])
    db.get_conn().commit()
    return {v["title"]: vid for vid, v in db.load_vacancies().items()}


def _local(monkeypatch):
    import score_vacancies

    importlib.reload(score_vacancies)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    args = types.SimpleNamespace(
        force=False,
        include_passed=False,
        no_candidates=False,
        limit=None,
        offset=0,
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


def test_local_payload_uses_real_uuid_member_ids(sqlite_dal, monkeypatch):
    ids = _seed(sqlite_dal, ["Operations Lead", "Data Analyst"])
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


def test_save_persists_score_and_full_summary(sqlite_dal, monkeypatch):
    ids = _seed(sqlite_dal, ["Operations Lead"])
    payload = _local(monkeypatch)
    item = payload[0]
    _save(
        monkeypatch,
        [
            {
                "payload_kind": "vacancy",
                "member_ids": item["member_ids"],
                "org": item["org"],
                "title": item["title"],
                "score": 73,
                "reasoning": "Strong ops fit.",
                "short_summary": _LONG,
                "hard_requirements": ["5y ops"],
                "tags": ["ops"],
            }
        ],
    )
    v = sqlite_dal.load_vacancies()[ids["Operations Lead"]]
    assert v["llm_score"] == 73
    assert v["llm_summary"] == _LONG  # FULL summary persisted, not truncated
    assert v["llm_scored_at"]
    assert v["llm_hard_requirements"] == ["5y ops"]


def test_save_maps_each_member_id(sqlite_dal, monkeypatch):
    """Two location-rows of the same role share one score entry → both updated."""
    sqlite_dal.ensure_company("Globex", status="active")
    # Same title, two locations → merge keeps ONE row with 2 locations, so this
    # exercises the single-member path; add a genuinely separate role too.
    sqlite_dal.save_vacancies("Globex", "A", [_job("Ops Lead", "Berlin, Germany")])
    sqlite_dal.save_vacancies("Globex", "A", [_job("Ops Lead", "London, United Kingdom")])
    sqlite_dal.save_vacancies("Globex", "A", [_job("Analyst")])
    sqlite_dal.get_conn().commit()
    payload = _local(monkeypatch)
    save = [
        {
            "payload_kind": "vacancy",
            "member_ids": p["member_ids"],
            "org": p["org"],
            "title": p["title"],
            "score": 50,
            "reasoning": "ok",
            "short_summary": _LONG,
            "hard_requirements": [],
            "tags": [],
        }
        for p in payload
    ]
    _save(monkeypatch, save)
    for v in sqlite_dal.load_vacancies().values():
        assert v["llm_score"] == 50


# ---------------------------------------------------------------------------
# Graceful rejection of malformed replies
# ---------------------------------------------------------------------------


def test_save_rejects_wrong_payload_kind(sqlite_dal, monkeypatch, capsys):
    ids = _seed(sqlite_dal, ["Operations Lead"])
    payload = _local(monkeypatch)
    item = payload[0]
    _save(
        monkeypatch,
        [
            {
                "payload_kind": "company",  # wrong kind → skipped
                "member_ids": item["member_ids"],
                "org": item["org"],
                "title": item["title"],
                "score": 99,
                "short_summary": _LONG,
            }
        ],
    )
    # The good row was NOT scored (the bad entry was rejected, not applied).
    assert sqlite_dal.load_vacancies()[ids["Operations Lead"]]["llm_score"] is None
    assert "wrong payload_kind" in capsys.readouterr().err


def test_save_rejects_missing_score(sqlite_dal, monkeypatch, capsys):
    ids = _seed(sqlite_dal, ["Operations Lead"])
    payload = _local(monkeypatch)
    item = payload[0]
    _save(
        monkeypatch,
        [
            {
                "payload_kind": "vacancy",
                "member_ids": item["member_ids"],
                "org": item["org"],
                "title": item["title"],
                # no "score" and no "score_data"
                "short_summary": _LONG,
            }
        ],
    )
    assert sqlite_dal.load_vacancies()[ids["Operations Lead"]]["llm_score"] is None
    assert "missing both score_data and a top-level score" in capsys.readouterr().err


def test_save_warns_on_unknown_uuid_without_crashing(sqlite_dal, monkeypatch, capsys):
    _seed(sqlite_dal, ["Operations Lead"])
    _save(
        monkeypatch,
        [
            {
                "payload_kind": "vacancy",
                "member_ids": ["00000000-0000-0000-0000-000000000000"],  # not in DB
                "org": "Globex",
                "title": "Ghost Role",
                "score": 42,
                "reasoning": "x",
                "short_summary": _LONG,
                "hard_requirements": [],
                "tags": [],
            }
        ],
    )
    # No crash; a warning is printed and the good row stays unscored.
    assert "not found in DB" in capsys.readouterr().err


def test_save_empty_payload_is_noop(sqlite_dal, monkeypatch, capsys):
    _seed(sqlite_dal, ["Operations Lead"])
    _save(monkeypatch, [])
    assert "No results to save" in capsys.readouterr().out
