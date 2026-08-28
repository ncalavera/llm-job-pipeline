"""Tests for telegram_digest.py — pure builders plus the tiered send.

The tiered-send tests run against a fresh temp SQLite DB (migrations 0025 and
0026 applied from the real files) with ``tg_call`` faked — no network, no
Postgres.
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

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "sql" / "migrations"
MIGRATION_0020 = _MIGRATIONS_DIR / "0025_add_vacancy_scoring_excluded_reason.sqlite.sql"
MIGRATION_0021 = _MIGRATIONS_DIR / "0026_add_vacancy_digest_dropped_at.sqlite.sql"


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
    assert "Top matches" in td._t("digest_tier_top")


def test_digest_switches_to_russian(monkeypatch):
    """PRODUCT_LANGUAGE=ru flips every user-facing string to Russian."""
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    line = td.build_top_line(dict(ROW), 1)
    assert "открыть →" in line

    expiring = td.build_expiring_message(dict(EXPIRING_ROW))
    assert "Вот-вот пропадёт" in expiring
    assert "последний раз виден 2026-06-20" in expiring


# ---------------------------------------------------------------------------
# No buttons. Nikita asked for the 👍/👎 feature to be removed (2026-08-28);
# nothing listens for a tap, so a button anywhere would be a dead control.
# ---------------------------------------------------------------------------


def test_the_bot_offers_no_buttons_at_all(denv):
    _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org B", "Mid Role", score=45)
    _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    td.cmd_send(_args())
    payloads = [p for m, p in denv.calls if m == "sendMessage"]
    assert payloads
    for p in payloads:
        assert "reply_markup" not in p


def test_an_expiring_alert_carries_no_buttons(denv):
    _seed(denv.db, "Org A", "Expiring Role", score=70, status="expiring")
    td.cmd_alert(_args(dry_run=False))
    for _, p in denv.calls:
        assert "reply_markup" not in p


def test_the_tap_handling_code_is_gone(denv):
    """Not hidden behind a flag — removed. A dead code path that could be
    re-enabled is how a stopped poller comes back."""
    for name in (
        "build_digest_keyboard",
        "build_keyboard",
        "build_expiring_keyboard",
        "rebuild_markup",
        "parse_callback",
        "handle_callback",
        "cmd_poll",
        "set_status",
        "_status_label",
        "CALLBACK_PREFIX",
        "ACTION_TO_STATUS",
    ):
        assert not hasattr(td, name), name


def test_there_is_no_poll_subcommand():
    import subprocess

    script = Path(__file__).resolve().parent.parent / "scripts" / "telegram_digest.py"
    res = subprocess.run(
        [sys.executable, str(script), "poll"], capture_output=True, text=True
    )
    assert res.returncode != 0
    assert "invalid choice: 'poll'" in res.stderr


def test_the_tier_one_header_no_longer_asks_for_a_tap():
    from i18n import STRINGS

    for lang in ("en", "ru"):
        header = STRINGS[lang]["digest_tier_top"]
        assert "👍" not in header and "👎" not in header
        assert "tap" not in header.lower() and "жми" not in header.lower()


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
    cur.execute(MIGRATION_0021.read_text(encoding="utf-8"))
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
    degraded=None,
):
    """Fixture mirroring the run_daily.py state shape the digest reads.

    Field contract (U4 must write exactly this):
      * top-level ``no_progress: true`` when the scoring session exited without
        saving a single score; the digest then renders the AE7 header line with
        N = len(stages[vacancy_scoring].target_ids).
      * optional top-level ``counts`` {"new_vacancies": F, "scored": S}
        (run_daily._run_counts persisted into the state).
      * stages[filter].filter.excluded_count → recorded, but NOT the header's
        D: the header counts dropped rows in the database instead.
      * stages[vacancy_scoring].carried_over / stages[company_scoring].carried_over
        → the tier-4 carried-over line (the header's U is the live pool).
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
    if degraded is not None:
        state["degraded"] = list(degraded)
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
    assert "Program Manager — GiveWell — skipped: only in US" in body
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
    i_carried = body.index("going first in the next run")
    assert i_top < i_mid < i_drop < i_carried
    # The top tier leads, on its own message.
    first_text = _sent_texts(denv.calls)[0]
    assert "Top Role" in first_text
    assert "Mid Role" not in first_text and "Dropped Role" not in first_text


def test_header_counts_from_run_state(denv):
    """fetched/scored come from the run state (THIS run); dropped and
    still-to-score are counted in the database (the backlog NOW), so both can
    be checked against what the message itself lists."""
    _seed(denv.db, "Org C", "Dropped One", reason="US-only location")
    _seed(denv.db, "Org C", "Dropped Two", reason="junk title: talent pool")
    _seed(denv.db, "Org D", "Waiting Role")  # no score, no reason
    _write_run_state(
        denv.run_state,
        counts={"new_vacancies": 12, "scored": 5},
        excluded_count=4,  # the in-memory filter tally — must NOT reach the header
        vac_carried=3,
    )
    td.cmd_send(_args())
    header = _sent_texts(denv.calls)[0]
    assert "found 12 new roles, scored 5" in header
    assert "2 skipped (listed below), 1 still to score" in header
    # The carried-over batch keeps its own tier-4 line; it is not the pool.
    assert "3 roles" in "\n".join(_sent_texts(denv.calls))


def test_header_names_the_backlog_parked_behind_unapproved_companies(denv):
    """B: 357 unscored roles at candidate companies were invisible in every
    report. The header now labels them next to the waiting figure."""
    _seed(denv.db, "Org A", "Waiting Role")
    for i in range(4):
        _seed(denv.db, "Stranger Co", f"Parked {i}", company_status="candidate")
    td.cmd_send(_args())
    header = _sent_texts(denv.calls)[0]
    assert "1 still to score" in header
    assert "4 more roles are waiting behind companies you have not approved yet" in header


def test_header_omits_the_parked_line_when_nothing_is_parked(denv):
    _seed(denv.db, "Org A", "Waiting Role")
    td.cmd_send(_args())
    header = _sent_texts(denv.calls)[0]
    assert "1 still to score" in header
    assert "waiting behind" not in header


def test_waiting_figure_counts_only_roles_the_scorer_would_take(denv):
    """passed / liked / already-scored / dropped rows are not waiting."""
    _seed(denv.db, "Org A", "Waiting Role")
    _seed(denv.db, "Org A", "Passed Role", status="passed")
    _seed(denv.db, "Org A", "Liked Role", status="liked")
    _seed(denv.db, "Org A", "Scored Role", score=70)
    _seed(denv.db, "Org A", "Dropped Role", reason="US-only location")
    td.cmd_send(_args())
    assert "1 still to score" in _sent_texts(denv.calls)[0]


def test_header_dropped_equals_the_dropped_lines_the_digest_lists(denv):
    """The header's "dropped" is the number of tier-3 rows this message claims,
    not the filter's in-memory tally (the 2026-08-27 mismatch: 128 vs 74)."""
    for i in range(3):
        _seed(denv.db, "Org X", f"Dropped {i}", reason="US-only location")
    _write_run_state(denv.run_state, counts={"new_vacancies": 9, "scored": 1}, excluded_count=99)
    td.cmd_send(_args())
    texts = _sent_texts(denv.calls)
    assert "3 skipped (listed below)" in texts[0]
    assert "99" not in texts[0]
    body = "\n".join(texts)
    assert len([ln for ln in body.splitlines() if "skipped:" in ln]) == 3


def test_header_counts_from_db_without_run_state(denv):
    _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org B", "Mid Role", score=45)
    _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    _seed(denv.db, "Org D", "Waiting Role")  # no score, no reason
    td.cmd_send(_args())
    header = _sent_texts(denv.calls)[0]
    assert "found 4 new roles, scored 2" in header
    assert "1 skipped (listed below), 1 still to score" in header


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
    # The candidate-hot row joins tier 1.
    assert "Strong Role" in body
    assert _col(denv.db, hot, "digest_sent_at") is not None


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


def test_dropped_cap_renders_more_tail_and_rest_surface_next_morning(denv, monkeypatch):
    monkeypatch.setattr(td, "DROPPED_MAX_LINES", 5)
    for i in range(8):
        _seed(denv.db, "Org X", f"Dropped {i}", reason="US-only location")
    td.cmd_send(_args())
    body = "\n".join(_sent_texts(denv.calls))
    assert "+3 more" in body
    # Only the shown rows are stamped — the 3 beyond the cap stay unclaimed
    # and arrive with the next digest instead of vanishing.
    denv.calls.clear()
    td.cmd_send(_args())
    second = "\n".join(_sent_texts(denv.calls))
    shown_second = [i for i in range(8) if f"Dropped {i}" in second]
    assert len(shown_second) == 3
    assert "more" not in second


def test_empty_night_sends_header_and_nothing_new(denv):
    td.cmd_send(_args())
    texts = _sent_texts(denv.calls)
    assert len(texts) == 1
    assert "found 0 new roles" in texts[0]
    assert "Quiet night" in texts[0]


def test_top_rows_stamp_sent_at_dropped_rows_stamp_dropped_at(denv):
    top = _seed(denv.db, "Org A", "Top Role", score=80)
    mid = _seed(denv.db, "Org B", "Mid Role", score=45)
    drop = _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    td.cmd_send(_args())
    assert _col(denv.db, top, "digest_sent_at") is not None
    assert _col(denv.db, mid, "digest_sent_at") is not None
    assert _col(denv.db, drop, "digest_dropped_at") is not None
    # digest_sent_at stays free on the dropped row: if its exclusion is later
    # cleared and it gets scored, it must still be able to reach tiers 1–2.
    assert _col(denv.db, drop, "digest_sent_at") is None
    assert _col(denv.db, top, "digest_dropped_at") is None


def test_second_send_same_morning_repeats_nothing(denv):
    _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    _write_run_state(denv.run_state, vac_carried=2, excluded_count=1)
    td.cmd_send(_args())
    first = "\n".join(_sent_texts(denv.calls))
    assert "Top Role" in first and "Dropped Role" in first
    assert "going first in the next run" in first
    denv.calls.clear()
    td.cmd_send(_args())
    second = _sent_texts(denv.calls)
    assert len(second) == 1
    assert "Top Role" not in second[0]
    assert "Dropped Role" not in second[0]
    assert "going first in the next run" not in second[0]


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
    cur.execute(
        "SELECT count(*) FROM vacancy "
        "WHERE digest_sent_at IS NOT NULL OR digest_dropped_at IS NOT NULL"
    )
    assert cur.fetchone()[0] == 0
    cur.close()


def test_dropped_later_the_same_day_appears_in_the_next_send(denv):
    """#9: tier 3 gates on the digest_dropped_at claim, not on a date-granular
    first_seen cutoff — a row dropped after the morning digest (same calendar
    day) arrives with the next send instead of vanishing forever."""
    early = _seed(denv.db, "Org A", "Early Drop", reason="US-only location")
    td.cmd_send(_args())
    assert "Early Drop" in "\n".join(_sent_texts(denv.calls))
    assert _col(denv.db, early, "digest_dropped_at") is not None

    late = _seed(denv.db, "Org B", "Late Drop", reason="junk title: talent pool")
    denv.calls.clear()
    td.cmd_send(_args())
    second = "\n".join(_sent_texts(denv.calls))
    assert "Late Drop" in second
    assert "Early Drop" not in second
    assert _col(denv.db, late, "digest_dropped_at") is not None


# ===========================================================================
# Multi-part sends — per-part keyboards and per-part claims (#20, #21, #19)
# ===========================================================================

BULKY_SUMMARY = "A long enough summary line to make every tier-1 entry bulky. " * 4


def _seed_five_top(db):
    """Five tier-1 rows whose entries overflow a shrunken message limit."""
    return [
        _seed(db, f"Org {i}", f"Top Role {i}", score=90 - i, summary=BULKY_SUMMARY)
        for i in range(5)
    ]


def _entry_numbers(text):
    """Global tier-1 entry numbers rendered in one message text."""
    import re

    return [int(n) for n in re.findall(r"^(\d+)\. <b>", text, re.MULTILINE)]


def _keyboard_numbers(payload):
    kb = payload.get("reply_markup")
    if not kb:
        return []
    return [int(row[0]["text"].split()[-1]) for row in kb["inline_keyboard"]]


def test_skipped_roles_never_share_a_message_with_the_top_matches(denv, monkeypatch):
    """#22: tier 3 always opens its own message. It began as a keyboard fix and
    survives the buttons — the top matches must not scroll away under a long
    list of skipped roles."""
    top = _seed(denv.db, "Org A", "Top Role", score=80)
    for i in range(3):
        _seed(denv.db, "Org X", f"Dropped {i}", reason="US-only location")
    td.cmd_send(_args())
    texts = _sent_texts(denv.calls)
    assert len(texts) == 2
    assert "Top Role" in texts[0] and "skipped:" not in texts[0]
    assert "skipped:" in texts[1] and "Top Role" not in texts[1]
    # The rows of each part are still claimed by that part.
    assert _col(denv.db, top, "digest_sent_at") is not None


# ===========================================================================
# Plain words — the morning message is read on a phone, not in a log viewer
# ===========================================================================

#: Internal vocabulary. It belongs in code identifiers, docstrings and
#: comments — never in a line a person reads.
JARGON = (
    "classified",
    "stamped",
    "cleared",
    "write scope",
    "out of scope",
    "persisted",
    "sentinel",
    "denominator",
    "partition",
    "backlog",
    "unseen",
    "row",
)

DIGEST_KEYS = (
    "digest_run_header",
    "digest_waiting_parked",
    "digest_degraded_firecrawl",
    "digest_degraded_exa",
    "digest_tier_dropped",
    "digest_dropped_prefix",
    "digest_no_progress",
    "digest_tier_top",
    "digest_tier_mid",
)


def test_digest_strings_carry_no_internal_vocabulary():
    from i18n import STRINGS

    for lang in ("en", "ru"):
        for key in DIGEST_KEYS:
            text = STRINGS[lang][key].lower()
            for word in JARGON:
                assert word not in text, f"{lang}.{key} says {word!r}"


#: Proper nouns that stay in Latin script in Russian too — transliterating a
#: product name would make the instruction harder to act on, not easier.
PRODUCT_NAMES = ("Firecrawl", "Exa", "Anthropic", "Telegram")


def test_russian_digest_strings_are_actually_russian():
    """No English loanwords or transliterated jargon: outside HTML tags,
    {placeholders} and product names, the Russian strings carry no Latin
    letters."""
    import re

    from i18n import STRINGS

    for key in DIGEST_KEYS:
        text = STRINGS["ru"][key]
        bare = re.sub(r"<[^>]+>|\{[^}]+\}", "", text)
        for name in PRODUCT_NAMES:
            bare = bare.replace(name, "")
        assert not re.search(r"[A-Za-z]", bare), f"ru.{key} has Latin letters: {bare!r}"


def test_both_languages_define_every_digest_string():
    from i18n import STRINGS

    for key in DIGEST_KEYS:
        assert key in STRINGS["en"] and key in STRINGS["ru"], key
        # Same placeholders on both sides, or one language crashes on format.
        import re

        assert set(re.findall(r"\{(\w+)\}", STRINGS["en"][key])) == set(
            re.findall(r"\{(\w+)\}", STRINGS["ru"][key])
        ), key


def test_a_missing_key_reaches_the_phone(denv):
    """The 2026-08-27 defect: Exa failed every call all night and said so only
    in a log line nobody reads at 02:00."""
    _write_run_state(denv.run_state, counts={"new_vacancies": 3, "scored": 1}, degraded=["exa"])
    td.cmd_send(_args())
    header = _sent_texts(denv.calls)[0]
    assert "Could not search the web about companies all night" in header
    assert "Add the Exa key on the server." in header
    assert "EXA_API_KEY" not in header  # the variable name is not his problem


def test_every_missing_key_gets_its_own_line(denv):
    _write_run_state(denv.run_state, degraded=["firecrawl", "exa"])
    td.cmd_send(_args())
    header = _sent_texts(denv.calls)[0]
    assert header.count("⚠️") == 2
    assert "Firecrawl key" in header and "Exa key" in header


def test_a_missing_anthropic_key_is_never_reported(denv):
    """Nikita said twice on 2026-08-28 that he does not want that key
    anywhere — scoring runs on his subscription through subagents. Its absence
    is the intended state, so it must never read as something to fix."""
    _write_run_state(denv.run_state, degraded=["anthropic"])
    td.cmd_send(_args())
    header = _sent_texts(denv.calls)[0]
    assert "Anthropic" not in header
    assert "⚠️" not in header


def test_a_capability_the_digest_does_not_know_is_passed_over(denv):
    """An id added to run_daily before its message exists must not print a raw
    key name on the phone."""
    _write_run_state(denv.run_state, degraded=["something_new"])
    td.cmd_send(_args())
    header = _sent_texts(denv.calls)[0]
    assert "digest_degraded" not in header and "something_new" not in header


def test_nothing_is_said_when_every_key_is_present(denv):
    _write_run_state(denv.run_state, counts={"new_vacancies": 3, "scored": 1})
    td.cmd_send(_args())
    assert "⚠️" not in _sent_texts(denv.calls)[0]


def test_mid_scores_never_share_a_message_with_the_top_matches(denv):
    """Same rule as tier 3, for the same reason."""
    top = _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org B", "Mid One", score=48)
    _seed(denv.db, "Org C", "Mid Two", score=44)
    td.cmd_send(_args())
    texts = _sent_texts(denv.calls)
    assert len(texts) == 2
    assert "Top Role" in texts[0]
    assert "Mid One" not in texts[0] and "Mid Two" not in texts[0]
    assert _col(denv.db, top, "digest_sent_at") is not None


def test_every_tier_opens_its_own_message(denv):
    """Tier 1 / 2 / 3 never share a message: one subject per message."""
    _seed(denv.db, "Org A", "Top Role", score=80)
    _seed(denv.db, "Org B", "Mid Role", score=45)
    _seed(denv.db, "Org C", "Dropped Role", reason="US-only location")
    td.cmd_send(_args())
    texts = _sent_texts(denv.calls)
    assert len(texts) == 3
    assert "Top Role" in texts[0]
    assert "Mid Role" in texts[1] and "Top Role" not in texts[1]
    assert "Dropped Role" in texts[2] and "Mid Role" not in texts[2]


def test_part_break_does_not_split_a_digest_with_only_one_tier(denv):
    """The break fires per tier: a night with tier 1 alone stays one message."""
    _seed(denv.db, "Org A", "Top Role", score=80)
    td.cmd_send(_args())
    assert len(_sent_texts(denv.calls)) == 1


def test_part_break_renders_nothing_of_its_own(denv):
    """The sentinel is a control block: it must never reach a message body."""
    parts = td.split_message_parts(["header", td.PART_BREAK, "tail"])
    assert [p["text"] for p in parts] == ["header", "tail"]
    assert td.PART_BREAK not in "".join(p["text"] for p in parts)


def test_failure_on_part_two_releases_only_that_part(denv, monkeypatch):
    """#21: delivered parts keep their stamps (no re-send), the failed part's
    claims are released, later parts were never claimed — the next run sends
    exactly the undelivered rows."""
    monkeypatch.setattr(td, "MESSAGE_MAX_CHARS", 600)
    ids = _seed_five_top(denv.db)

    calls = []

    def flaky(token, method, payload, timeout=15, retries=2):
        calls.append((method, payload))
        if len(calls) == 2:
            raise RuntimeError("sendMessage failed: flood control")
        return {}

    monkeypatch.setattr(td, "tg_call", flaky)
    with pytest.raises(SystemExit) as exc:
        td.cmd_send(_args())
    assert exc.value.code != 0

    part1 = calls[0][1]["text"]
    delivered = [vid for i, vid in enumerate(ids) if f"Top Role {i}" in part1]
    assert delivered and len(delivered) < len(ids)
    for vid in ids:
        stamped = _col(denv.db, vid, "digest_sent_at") is not None
        assert stamped == (vid in delivered)
    # The in-process release also cleared the crash journal.
    assert not json.loads(denv.state_file.read_text()).get("pending_claim")

    # Next run: the delivered rows stay silent, the rest go out.
    monkeypatch.setattr(
        td, "tg_call", lambda t, m, p, timeout=15, retries=2: denv.calls.append((m, p)) or {}
    )
    denv.calls.clear()
    td.cmd_send(_args())
    body = "\n".join(_sent_texts(denv.calls))
    for i, vid in enumerate(ids):
        assert (f"Top Role {i}" in body) == (vid not in delivered)
        assert _col(denv.db, vid, "digest_sent_at") is not None


def test_stale_pending_claim_from_a_killed_run_is_released(denv):
    """#19: a SIGKILL between claim and send leaves stamps plus the journalled
    pending_claim; the next send releases those ids first, so the rows appear
    in its message instead of being lost forever."""
    top = _seed(denv.db, "Org A", "Crashed Top", score=80)
    drop = _seed(denv.db, "Org C", "Crashed Drop", reason="US-only location")
    conn = denv.db.get_conn()
    td.mark_sent_many(conn, [top])
    td.mark_dropped_many(conn, [drop])
    denv.state_file.write_text(
        json.dumps({"pending_claim": {"sent": [top], "dropped": [drop], "alerted": []}})
    )
    td.cmd_send(_args())
    body = "\n".join(_sent_texts(denv.calls))
    assert "Crashed Top" in body
    assert "Crashed Drop" in body
    # Re-claimed by the successful send; the journal is cleared.
    assert _col(denv.db, top, "digest_sent_at") is not None
    assert _col(denv.db, drop, "digest_dropped_at") is not None
    assert not json.loads(denv.state_file.read_text()).get("pending_claim")


# ---------------------------------------------------------------------------
# Skip reasons: technical in the database, plain on the phone
# ---------------------------------------------------------------------------


def test_a_rule_name_never_reaches_the_phone(denv):
    """The stored reason names the rule so a debugger can trace it; the digest
    says what it means. The 2026-08-27 digest showed the rule name itself."""
    _seed(
        denv.db,
        "WFP",
        "Conductor GS2",
        reason="company_title_filter — not in WFP - World Food Programme include list",
    )
    td.cmd_send(_args())
    body = "\n".join(_sent_texts(denv.calls))
    assert "skipped: not the kind of role you look for" in body
    assert "company_title_filter" not in body
    assert "include list" not in body
    # The database keeps the technical reason untouched.
    cur = denv.db.get_conn().cursor()
    cur.execute("SELECT scoring_excluded_reason FROM vacancy WHERE title = ?", ("Conductor GS2",))
    stored = cur.fetchone()[0]
    cur.close()
    assert stored.startswith("company_title_filter")


@pytest.mark.parametrize(
    "stored,plain",
    [
        ("junk title: talent pool", "the title is not a real role"),
        ("junk content: error page", "the page is not a job description"),
        ("archived before", "you archived this one before"),
        ("no description after enrichment", "no description could be read"),
    ],
)
def test_every_stored_reason_has_plain_words(stored, plain):
    assert td.plain_skip_reason(stored) == plain


@pytest.mark.parametrize(
    "stored,plain",
    [
        ("US-only location", "only in US"),
        ("excluded locations only (Canada, US)", "only in places you ruled out: Canada, US"),
    ],
)
def test_a_location_reason_keeps_its_place_name(stored, plain):
    """A country is a proper noun: the sentence is translated, the place is not."""
    assert td.plain_skip_reason(stored) == plain


@pytest.mark.parametrize(
    "stored,plain",
    [
        ("a program or grant to apply to, not a job", "a programme or grant to apply to, not a job"),
        ("test posting, not a real job", "a test posting, not a real job"),
    ],
)
def test_the_not_a_vacancy_reasons_are_translatable_too(stored, plain):
    """Stored in English by the filter; the phone gets the digest's language."""
    assert td.plain_skip_reason(stored) == plain


def test_an_unmapped_reason_is_shown_as_stored():
    """A rule added later must stay visible, not vanish behind a wrong phrase."""
    assert td.plain_skip_reason("some brand new rule") == "some brand new rule"
    assert td.plain_skip_reason(None) == ""


def test_skip_reasons_are_russian_in_russian(monkeypatch):
    monkeypatch.setenv("PRODUCT_LANGUAGE", "ru")
    import importlib

    import product_language

    importlib.reload(product_language)
    try:
        assert td.plain_skip_reason("company_title_filter — not in X include list") == (
            "не тот тип роли"
        )
    finally:
        monkeypatch.delenv("PRODUCT_LANGUAGE", raising=False)
        importlib.reload(product_language)
