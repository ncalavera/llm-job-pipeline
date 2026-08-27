"""Tests for telegram_digest.py — pure builders plus the tiered send.

The tiered-send tests run against a fresh temp SQLite DB (migration 0020
applied from the real file) with ``tg_call`` faked — no network, no Postgres.
"""

import importlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import telegram_digest as td

MIGRATION_0020 = (
    Path(__file__).resolve().parent.parent
    / "sql"
    / "migrations"
    / "0020_add_vacancy_scoring_excluded_reason.sqlite.sql"
)


ROW = {
    "id": "11111111-2222-3333-4444-555555555555",
    "org": "Example Org",
    "title": "Senior Advisor <& Co>",
    "llm_score": 89,
    "llm_summary": "A short vacancy summary.",
    "full_description": None,
    "snippet": None,
    "compensation": None,
    "locations": [
        {
            "url": "https://example.com/jobs/1?a=1&b=2",
            "city": "London",
            "country": "UK",
            "region": "europe",
            "work_mode": "hybrid",
            "compensation": "£90k",
        }
    ],
}


def test_top_line_escapes_html_and_has_all_parts():
    line = td.build_top_line(dict(ROW), 1)
    assert "&lt;&amp; Co&gt;" in line  # HTML in the title is escaped
    assert "1. <b>Example Org</b>" in line
    assert "🎯 89" in line
    assert 'href="https://example.com/jobs/1?a=1&amp;b=2"' in line
    assert "A short vacancy summary." in line


def test_summary_fallback_to_description():
    row = dict(ROW, llm_summary=None, full_description="word " * 300)
    s = td.vacancy_summary(row)
    assert s.endswith("…")
    assert len(s) <= td.SUMMARY_FALLBACK_CHARS + 1


def test_vacancy_url_missing_locations():
    assert td.vacancy_url({"locations": None}) is None
    assert td.vacancy_url({"locations": [{"city": "X"}]}) is None


def test_keyboard_callback_roundtrip():
    kb = td.build_keyboard(ROW["id"])
    like_btn, pass_btn = kb["inline_keyboard"][0]
    assert td.parse_callback(like_btn["callback_data"]) == (ROW["id"], "liked")
    assert td.parse_callback(pass_btn["callback_data"]) == (ROW["id"], "passed")
    assert len(like_btn["callback_data"].encode()) <= 64  # Telegram limit


def test_keyboard_marks_chosen():
    kb = td.build_keyboard(ROW["id"], chosen="liked")
    assert kb["inline_keyboard"][0][0]["text"].startswith("✅")
    assert "✅" not in kb["inline_keyboard"][0][1]["text"]


def test_parse_callback_rejects_garbage():
    assert td.parse_callback(None) is None
    assert td.parse_callback("") is None
    assert td.parse_callback("x:y:z") is None
    assert td.parse_callback("v:abc:q") is None  # unknown action
    assert td.parse_callback("v::l") is None  # empty id
    assert td.parse_callback("v:abc:l:extra") is None


def test_digest_keyboard_one_row_per_top_vacancy():
    rows = [dict(ROW), dict(ROW, id="99999999-2222-3333-4444-555555555555")]
    kb = td.build_digest_keyboard(rows)
    assert len(kb["inline_keyboard"]) == 2
    for i, krow in enumerate(kb["inline_keyboard"], 1):
        like, pas = krow
        assert str(i) in like["text"] and "👍" in like["text"]
        assert str(i) in pas["text"] and "👎" in pas["text"]
        assert td.parse_callback(like["callback_data"])[1] == "liked"
        assert td.parse_callback(pas["callback_data"])[1] == "passed"
        assert len(like["callback_data"].encode()) <= 64


def test_rebuild_markup_marks_only_the_tapped_row():
    rows = [dict(ROW), dict(ROW, id="99999999-2222-3333-4444-555555555555")]
    kb = td.build_digest_keyboard(rows)
    marked = td.rebuild_markup(kb, rows[1]["id"], "passed")
    assert "✅" not in marked["inline_keyboard"][0][0]["text"]
    assert "✅" not in marked["inline_keyboard"][0][1]["text"]
    assert "✅" not in marked["inline_keyboard"][1][0]["text"]
    assert marked["inline_keyboard"][1][1]["text"].startswith("✅")
    # Flipping the choice moves the mark instead of stacking a second one.
    flipped = td.rebuild_markup(marked, rows[1]["id"], "liked")
    assert flipped["inline_keyboard"][1][0]["text"].startswith("✅")
    assert not flipped["inline_keyboard"][1][1]["text"].startswith("✅")


def test_split_message_splits_at_line_boundaries_in_order():
    blocks = [f"line {i:03d}" for i in range(100)]
    parts = td.split_message(blocks, limit=200)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 200
    assert "\n".join(parts) == "\n".join(blocks)


# --- expiring-role alert (standalone `alert` mode keeps working) -------------

EXPIRING_ROW = dict(
    ROW,
    deadline=None,
    last_seen="2026-06-20",
)


def test_expiring_message_is_loud_and_complete():
    # Default product language (the example profile is English) → English copy.
    msg = td.build_expiring_message(dict(EXPIRING_ROW))
    assert "About to disappear" in msg  # loud, distinct header
    assert "Example Org — Senior Advisor" in msg
    assert "🎯 89/100" in msg
    assert "last seen 2026-06-20" in msg  # why it is expiring
    assert "href=" in msg


# --- The digest speaks the ONE product language ---


def test_digest_default_language_is_english():
    """With no override + the example (English) profile, copy is English."""
    line = td.build_top_line(dict(ROW), 1)
    assert "open →" in line
    assert td.build_keyboard(ROW["id"])["inline_keyboard"][0][0]["text"] == "👍 Liked"


def test_digest_switches_to_russian(monkeypatch):
    """PRODUCT_LANGUAGE=ru flips every user-facing string to Russian."""
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    line = td.build_top_line(dict(ROW), 1)
    assert "открыть →" in line

    expiring = td.build_expiring_message(dict(EXPIRING_ROW))
    assert "Вот-вот пропадёт" in expiring
    assert "последний раз виден 2026-06-20" in expiring

    kb = td.build_keyboard(ROW["id"])
    assert kb["inline_keyboard"][0][0]["text"] == "👍 В избранное"
    assert kb["inline_keyboard"][0][1]["text"] == "👎 Отказ"

    # Buttons still carry the same callback contract — only the label changed.
    assert td.parse_callback(kb["inline_keyboard"][0][0]["callback_data"]) == (
        ROW["id"],
        "liked",
    )


def test_expiring_keyboard_has_three_actions_that_map():
    kb = td.build_expiring_keyboard(ROW["id"])
    btns = kb["inline_keyboard"][0]
    assert len(btns) == 3
    mapped = [td.parse_callback(b["callback_data"]) for b in btns]
    assert mapped == [
        (ROW["id"], "liked"),
        (ROW["id"], "passed"),
        (ROW["id"], "applied"),  # «уже подал» → applied
    ]
    for b in btns:
        assert len(b["callback_data"].encode()) <= 64


def test_expiring_alert_fires_once_per_role():
    """fetch gates on expiring_alerted_at IS NULL; sending stamps it — so a
    second run finds nothing to re-send."""
    assert "expiring_alerted_at is null" in td.SELECT_EXPIRING_SQL.lower()
    assert "status = 'expiring'" in td.SELECT_EXPIRING_SQL.lower()

    captured = {}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

    class _Conn:
        def cursor(self, *a, **k):
            return _Cur()

        def commit(self):
            pass

    td.mark_alerted(_Conn(), ROW["id"])
    assert "expiring_alerted_at = now()" in captured["sql"].lower()
    assert captured["params"] == (ROW["id"],)


def test_send_expiring_alerts_empty_is_noop():
    # No rows → no network call, returns 0.
    assert td.send_expiring_alerts(None, "tok", "chat", []) == 0


# ===========================================================================
# Tiered send (U3) — temp SQLite + faked tg_call
# ===========================================================================


def _args(**over):
    base = dict(limit=5, min_score=None, dry_run=False)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture()
def denv(tmp_path, monkeypatch):
    """Fresh migrated SQLite + telegram env + captured tg_call."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "digest.db"))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db

    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(MIGRATION_0020.read_text(encoding="utf-8"))
    conn.commit()
    cur.close()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    state_file = tmp_path / "digest_state.json"
    run_state = tmp_path / "run_state.json"
    monkeypatch.setenv("DIGEST_STATE_FILE", str(state_file))
    monkeypatch.setenv("DIGEST_RUN_STATE_FILE", str(run_state))

    calls = []

    def fake_tg(token, method, payload, timeout=15, retries=2):
        calls.append((method, payload))
        return {}

    monkeypatch.setattr(td, "tg_call", fake_tg)
    monkeypatch.setattr(td.time, "sleep", lambda s: None)

    yield SimpleNamespace(
        db=db, calls=calls, state_file=state_file, run_state=run_state, tmp=tmp_path
    )
    db.close_conn()


def _seed(
    db,
    org,
    title,
    *,
    company_status="active",
    status="unseen",
    score=None,
    summary="A believable one-line summary of the role.",
    reason=None,
    first_seen=None,
    deadline=None,
    digest_sent_at=None,
    url="https://example.test/job",
):
    """Insert one vacancy row directly and return its id."""
    db.ensure_company(org, status=company_status)
    company_id = db.resolve_company_id(org)
    dedup = db.make_vacancy_id(db.resolve_canonical_name(org), f"{title}")
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO vacancy (dedup_hash, company_id, title, full_description, llm_summary, "
        "first_seen, last_seen, locations, status, llm_score, scoring_excluded_reason, "
        "deadline, digest_sent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            dedup,
            company_id,
            title,
            "A real job description long enough to pass every content gate. " * 4,
            summary,
            (first_seen or date.today()).isoformat(),
            date.today().isoformat(),
            json.dumps([{"location": "Berlin, Germany", "url": url}]),
            status,
            score,
            reason,
            deadline.isoformat() if deadline else None,
            digest_sent_at,
        ),
    )
    cur.execute("SELECT id FROM vacancy WHERE dedup_hash = ?", (dedup,))
    vac_id = cur.fetchone()[0]
    cur.close()
    conn.commit()
    return vac_id


def _col(db, vac_id, col):
    cur = db.get_conn().cursor()
    cur.execute(f"SELECT {col} FROM vacancy WHERE id = ?", (vac_id,))
    val = cur.fetchone()[0]
    cur.close()
    return val


def _write_run_state(
    path,
    *,
    updated_at=None,
    counts=None,
    no_progress=False,
    target_ids=None,
    vac_carried=None,
    co_carried=None,
    rolled_over=None,
    pending_verdicts=None,
    excluded_count=None,
):
    """Fixture mirroring the run_daily.py state shape the digest reads.

    Field contract (U4 must write exactly this):
      * top-level ``no_progress: true`` when the scoring session exited without
        saving a single score; the digest then renders the AE7 header line with
        N = len(stages[vacancy_scoring].target_ids).
      * optional top-level ``counts`` {"new_vacancies": F, "scored": S}
        (run_daily._run_counts persisted into the state).
      * stages[filter].filter.excluded_count → the D in the header.
      * stages[vacancy_scoring].carried_over / stages[company_scoring].carried_over
        → the U in the header + the tier-4 carried-over line.
      * stages[learning_review].rolled_over, stages[verdicts].pending_verdicts
        → their own tier-4 lines.
    """
    vac_stage = {"name": "vacancy_scoring", "status": "done"}
    if target_ids is not None:
        vac_stage["target_ids"] = list(target_ids)
    if vac_carried is not None:
        vac_stage["carried_over"] = vac_carried
    co_stage = {"name": "company_scoring", "status": "done"}
    if co_carried is not None:
        co_stage["carried_over"] = co_carried
    lr_stage = {"name": "learning_review", "status": "done"}
    if rolled_over is not None:
        lr_stage["rolled_over"] = rolled_over
    ve_stage = {"name": "verdicts", "status": "done"}
    if pending_verdicts is not None:
        ve_stage["pending_verdicts"] = pending_verdicts
    fi_stage = {"name": "filter", "status": "done"}
    if excluded_count is not None:
        fi_stage["filter"] = {"excluded_count": excluded_count, "excluded_reasons": {}}
    state = {
        "run_id": "test",
        "created_at": updated_at or datetime.now().isoformat(timespec="seconds"),
        "updated_at": updated_at or datetime.now().isoformat(timespec="seconds"),
        "stages": [lr_stage, fi_stage, co_stage, vac_stage, ve_stage],
    }
    if counts is not None:
        state["counts"] = counts
    if no_progress:
        state["no_progress"] = True
    Path(path).write_text(json.dumps(state), encoding="utf-8")


def _sent_texts(calls):
    return [p["text"] for m, p in calls if m == "sendMessage"]


def test_dropped_line_renders_reason_and_link(denv):
    """AE2: one line — title, company, "dropped: US-only location", a link."""
    _seed(
        denv.db,
        "GiveWell",
        "Program Manager",
        reason="US-only location",
        url="https://example.test/givewell/pm",
    )
    td.cmd_send(_args())
    body = "\n".join(_sent_texts(denv.calls))
    assert "Program Manager — GiveWell — dropped: US-only location" in body
    assert 'href="https://example.test/givewell/pm"' in body


def test_no_progress_header(denv):
    """AE7: a normal-exit session that saved nothing never reads as quiet."""
    _write_run_state(denv.run_state, no_progress=True, target_ids=[f"id{i}" for i in range(8)])
    td.cmd_send(_args())
    body = _sent_texts(denv.calls)[0]
    assert "scored 0 of 8 — the session made no progress" in body


def test_tier_order_top_mid_dropped_carried(denv):
    top = _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org B", "Mid Role", score=45)
    _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    _write_run_state(denv.run_state, vac_carried=3)
    td.cmd_send(_args())
    body = "\n".join(_sent_texts(denv.calls))
    i_top = body.index("Top Role")
    i_mid = body.index("Mid Role")
    i_drop = body.index("Dropped Role")
    i_carried = body.index("carried over")
    assert i_top < i_mid < i_drop < i_carried
    # The top tier carries the like/pass buttons on the first message.
    first_payload = [p for m, p in denv.calls if m == "sendMessage"][0]
    cbs = [
        b["callback_data"]
        for row in first_payload["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert f"v:{top}:l" in cbs and f"v:{top}:p" in cbs


def test_header_counts_from_run_state(denv):
    _write_run_state(
        denv.run_state,
        counts={"new_vacancies": 12, "scored": 5},
        excluded_count=4,
        vac_carried=3,
    )
    td.cmd_send(_args())
    assert "12 fetched, 5 scored, 4 dropped, 3 not scored yet" in _sent_texts(denv.calls)[0]


def test_header_counts_from_db_without_run_state(denv):
    _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org B", "Mid Role", score=45)
    _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    _seed(denv.db, "Org D", "Waiting Role")  # no score, no reason
    td.cmd_send(_args())
    assert "4 fetched, 2 scored, 1 dropped, 1 not scored yet" in _sent_texts(denv.calls)[0]


def test_deadline_header_line_and_candidate_hot_in_tier1(denv):
    e1 = _seed(denv.db, "Org A", "Expiring One", score=70, status="expiring")
    e2 = _seed(denv.db, "Org B", "Expiring Two", score=60, status="expiring")
    hot = _seed(denv.db, "Stranger Co", "Strong Role", score=75, company_status="candidate")
    td.cmd_send(_args())
    body = _sent_texts(denv.calls)[0]
    assert "2 deadlines this week" in body
    # No separate loud alert messages — the fold replaces them (KTD5)…
    assert not any("About to disappear" in t for t in _sent_texts(denv.calls))
    # …but the roles are stamped so the old alert path won't re-fire.
    assert _col(denv.db, e1, "expiring_alerted_at") is not None
    assert _col(denv.db, e2, "expiring_alerted_at") is not None
    # The candidate-hot row joins tier 1 with buttons.
    assert "Strong Role" in body
    first_payload = [p for m, p in denv.calls if m == "sendMessage"][0]
    cbs = [
        b["callback_data"]
        for row in first_payload["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert f"v:{hot}:l" in cbs


def test_many_dropped_split_preserves_order_and_caps(denv, monkeypatch):
    for i in range(60):
        _seed(denv.db, "Org X", f"Dropped {i:02d}", reason="US-only location")
    # Default cap: 25 lines + a "+35 more" tail.
    td.cmd_send(_args(dry_run=True))
    monkeypatch.setattr(td, "DROPPED_MAX_LINES", 100)
    monkeypatch.setattr(td, "MESSAGE_MAX_CHARS", 1500)
    td.cmd_send(_args())
    texts = _sent_texts(denv.calls)
    assert len(texts) >= 2
    for t in texts:
        assert len(t) <= 1500
    joined = "\n".join(texts)
    positions = [joined.index(f"Dropped {i:02d}") for i in range(60)]
    assert positions == sorted(positions)


def test_dropped_cap_renders_more_tail(denv, monkeypatch):
    monkeypatch.setattr(td, "DROPPED_MAX_LINES", 5)
    for i in range(8):
        _seed(denv.db, "Org X", f"Dropped {i}", reason="US-only location")
    td.cmd_send(_args())
    body = "\n".join(_sent_texts(denv.calls))
    assert "+3 more" in body


def test_empty_night_sends_header_and_nothing_new(denv):
    td.cmd_send(_args())
    texts = _sent_texts(denv.calls)
    assert len(texts) == 1
    assert "0 fetched" in texts[0]
    assert "Quiet night" in texts[0]


def test_top_rows_stamped_dropped_rows_not(denv):
    top = _seed(denv.db, "Org A", "Top Role", score=80)
    mid = _seed(denv.db, "Org B", "Mid Role", score=45)
    drop = _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    td.cmd_send(_args())
    assert _col(denv.db, top, "digest_sent_at") is not None
    assert _col(denv.db, mid, "digest_sent_at") is not None
    assert _col(denv.db, drop, "digest_sent_at") is None


def test_second_send_same_morning_repeats_nothing(denv):
    _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    _write_run_state(denv.run_state, vac_carried=2, excluded_count=1)
    td.cmd_send(_args())
    first = "\n".join(_sent_texts(denv.calls))
    assert "Top Role" in first and "Dropped Role" in first and "carried over" in first
    denv.calls.clear()
    td.cmd_send(_args())
    second = _sent_texts(denv.calls)
    assert len(second) == 1
    assert "Top Role" not in second[0]
    assert "Dropped Role" not in second[0]
    assert "carried over" not in second[0]


def test_telegram_error_stops_and_exits_nonzero(denv, monkeypatch):
    top = _seed(denv.db, "Org A", "Top Role", score=80)

    def boom(token, method, payload, timeout=15, retries=2):
        raise RuntimeError("sendMessage failed: chat not found")

    monkeypatch.setattr(td, "tg_call", boom)
    with pytest.raises(SystemExit) as exc:
        td.cmd_send(_args())
    assert exc.value.code != 0
    # The claim was released, so tomorrow's run re-sends it.
    assert _col(denv.db, top, "digest_sent_at") is None


def test_tier_labels_in_russian(denv, monkeypatch):
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org B", "Mid Role", score=45)
    _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    td.cmd_send(_args())
    body = "\n".join(_sent_texts(denv.calls))
    assert "Лучшие совпадения" in body
    assert "Средние оценки" in body
    assert "Отсеяно" in body
    assert "Ночной прогон" in body


def test_dry_run_prints_in_order_and_sends_nothing(denv, capsys):
    _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    td.cmd_send(_args(dry_run=True))
    out = capsys.readouterr().out
    assert out.index("Top Role") < out.index("Dropped Role")
    assert "nothing sent" in out
    assert denv.calls == []
    # Dry run claims nothing.
    cur = denv.db.get_conn().cursor()
    cur.execute("SELECT count(*) FROM vacancy WHERE digest_sent_at IS NOT NULL")
    assert cur.fetchone()[0] == 0
    cur.close()
