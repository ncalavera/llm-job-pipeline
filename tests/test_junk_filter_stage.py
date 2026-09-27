"""Junk-filter stage (U5): shadow and live modes, the one-check-per-role cache,
the refill loop, fail-open, restore and the guarded write. SQLite fixture;
every engine is a fake, so nikita_tools is never imported."""

import importlib
import json
import sys

import pytest

LONG = "Operations lead for a global health charity, running grants and programmes. " * 10
CLEAN = {"english": {"noul": 0.99}}
RTW = {"english": {"noul": 0.99}, "right_to_work": {"noul": 0.98}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobsearch.db"))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend", "migrate",
                "prepare_discovery", "judge_roles", "junk_filter_stage", "filter_vacancies"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import migrate

    assert migrate.cmd_migrate(allow_destructive=True, do_backup=False) == 0
    import junk_filter_stage as st

    monkeypatch.setattr(st.prepare_discovery, "_cap", lambda limit=None: 3)
    conn = db_backend.get_conn()
    # Postgres has company.visa_sponsor (read by judge_roles._ROLE_SELECT); no SQLite migration adds it.
    conn.cursor().execute("ALTER TABLE company ADD COLUMN visa_sponsor TEXT")
    conn.cursor().execute("INSERT INTO company (id, canonical_name, status) VALUES ('c1', 'Org', 'active')")
    conn.commit()
    yield st, conn
    db_backend.close_conn() if hasattr(db_backend, "close_conn") else None


def _add(conn, vid, *, reason=None, score=None, company="c1", day=1):
    conn.cursor().execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, full_description, first_seen, last_seen, "
        "status, llm_score, scoring_excluded_reason) VALUES (%s, %s, %s, %s, %s, %s, %s, 'unseen', %s, %s)",
        (vid, vid, company, vid, LONG, f"2026-09-0{day}", f"2026-09-0{day}", score, reason),
    )
    conn.commit()


def _row(conn, vid):
    cur = conn.cursor()
    cur.execute("SELECT scoring_excluded_reason, screening FROM vacancy WHERE id = %s", (vid,))
    reason, screening = cur.fetchone()
    cur.close()
    if isinstance(screening, str):
        screening = json.loads(screening)
    return reason, (screening or {}).get("junk")


class Engine:
    """Answers RTW for titles in ``skip``; raises for titles in ``fail``."""

    def __init__(self, skip=(), fail=()):
        self.skip, self.fail, self.calls = set(skip), set(fail), []

    def __call__(self, state, questions):
        title = state["role"]["title"]
        self.calls.append(title)
        if title in self.fail:
            err = RuntimeError("jev http 500")
            err.status, err.body, err.headers = 500, '{"error":"boom"}', {"x-request-id": "rq1"}
            raise err
        return (RTW if title in self.skip else CLEAN), {"tokens": 10}


def cfg(mode, **over):
    base = {"mode": mode, "engine": "jev", "cut": 0.95, "max_per_run": 200, "scratch_dir": "",
            "baseline_model": "", "sample_pct": 10, "live_since": ""}
    base.update(over)
    return base


def _three(conn):
    for i, v in enumerate(("a", "b", "c"), start=1):
        _add(conn, v, day=i)


def test_shadow_marks_would_skip_and_never_touches_the_reason(env):
    st, conn = env
    _three(conn)
    out = st.run_stage(cfg("shadow"), engine=Engine(skip={"b"}))
    assert out["counts"]["checked"] == 3 and out["counts"]["would_skip"] == 1
    assert out["seconds_saved"] == 55
    assert [_row(conn, v)[0] for v in "abc"] == [None, None, None]
    assert _row(conn, "b")[1]["would_skip"] is True
    assert _row(conn, "a")[1]["would_skip"] is False


def test_live_stamps_the_reason_and_the_scorer_pool_drops_the_role(env):
    st, conn = env
    _three(conn)
    out = st.run_stage(cfg("live"), engine=Engine(skip={"b"}))
    assert _row(conn, "b")[0] == "junk_filter: right_to_work 0.98"
    assert _row(conn, "b")[1]["skipped"] is True
    assert out["counts"]["skipped"] == 1 and out["junk_reasons_after"] == out["junk_reasons_before"] + 1
    assert "b" not in [p["id"] for p in st.prepare_discovery.select_payloads()]


def test_rule_filter_keeps_the_junk_reason_after_a_live_skip(env, monkeypatch):
    st, conn = env
    _three(conn)
    _add(conn, "d", reason="junk title: talent pool", day=4)
    st.run_stage(cfg("live"), engine=Engine(skip={"b"}))
    import filter_vacancies as fv

    monkeypatch.setattr(fv, "_GEO_ACTIVE", False)
    fv.persist_scoring_exclusions(fv.classify_vacancies())
    assert _row(conn, "b")[0] == "junk_filter: right_to_work 0.98"
    assert _row(conn, "d")[0] is None


def test_second_run_makes_no_engine_calls(env):
    st, conn = env
    _three(conn)
    st.run_stage(cfg("shadow"), engine=Engine(skip={"b"}))
    again = Engine(skip={"b"})
    out = st.run_stage(cfg("shadow"), engine=again)
    assert again.calls == [] and out["counts"].get("checked", 0) == 0


def test_refill_loop_checks_every_role_that_enters_the_capped_pool(env):
    st, conn = env
    for i, v in enumerate("abcde", start=1):
        _add(conn, v, day=i)
    eng = Engine(skip={"a", "b"})
    out = st.run_stage(cfg("live"), engine=eng)
    assert sorted(eng.calls) == list("abcde")
    assert out["counts"]["checked"] == 5 and out["counts"]["skipped"] == 2
    pool = [p["id"] for p in st.prepare_discovery.select_payloads()]
    assert pool == ["c", "d", "e"]
    assert all(_row(conn, v)[1] for v in pool)


def test_refill_loop_is_bounded_by_max_per_run(env):
    st, conn = env
    for i, v in enumerate("abcde", start=1):
        _add(conn, v, day=i)
    eng = Engine(skip={"a", "b", "c", "d", "e"})
    out = st.run_stage(cfg("live", max_per_run=4), engine=eng)
    assert len(eng.calls) == 4 and out["counts"]["checked"] == 4


def test_shadow_decision_is_stamped_when_live_starts_without_a_new_call(env):
    st, conn = env
    _three(conn)
    st.run_stage(cfg("shadow"), engine=Engine(skip={"b"}))
    eng = Engine(skip={"b"})
    out = st.run_stage(cfg("live"), engine=eng)
    assert eng.calls == []
    assert _row(conn, "b")[0] == "junk_filter: right_to_work 0.98"
    assert out["counts"]["skipped"] == 1


def test_new_task_version_checks_the_roles_again(env):
    st, conn = env
    _three(conn)
    base = cfg("shadow", engine="baseline", baseline_model="m.pkl")
    st.run_stage(base, engine=lambda s, q: {"p_junk": 0.1, "cut": 0.95})
    calls = []
    st.run_stage({**base, "cut": 0.9}, engine=lambda s, q: calls.append(1) or {"p_junk": 0.1, "cut": 0.9})
    assert len(calls) == 3


def test_engine_error_fails_open_with_diagnostics(env, tmp_path):
    st, conn = env
    _three(conn)
    out = st.run_stage(cfg("live", scratch_dir=str(tmp_path / "scratch")), engine=Engine(skip={"a", "c"}, fail={"b"}))
    assert _row(conn, "a")[0].startswith("junk_filter:") and _row(conn, "c")[0].startswith("junk_filter:")
    assert _row(conn, "b") == (None, None)
    assert out["counts"]["errors"] == 1
    err = out["errors"][0]
    assert err["id"] == "b" and err["status"] == 500 and err["headers"] == {"x-request-id": "rq1"}
    files = sorted(p.name for p in (tmp_path / "scratch").rglob("*.json"))
    assert files == ["a.json", "b.json", "c.json"]


def test_no_key_advances_and_writes_nothing(env):
    st, conn = env
    _three(conn)
    out = st.run_stage(cfg("live"))
    assert out["skipped"].startswith("no key")
    assert [_row(conn, v) for v in "abc"] == [(None, None)] * 3


def test_no_profile_advances_and_writes_nothing(env, tmp_path, monkeypatch):
    st, conn = env
    _three(conn)
    monkeypatch.setenv("JUNK_PROFILE_PATH", str(tmp_path / "absent.json"))
    eng = Engine(skip={"a"})
    out = st.run_stage(cfg("live"), engine=eng)
    assert out["skipped"].startswith("no profile")
    assert eng.calls == [] and [_row(conn, v) for v in "abc"] == [(None, None)] * 3


def test_engine_load_failure_advances_and_writes_nothing(env, monkeypatch):
    st, conn = env
    _three(conn)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    def boom(cfg):
        raise ImportError("no module named nikita_tools")

    monkeypatch.setattr(st, "load_engine", boom)
    out = st.run_stage(cfg("live"))
    assert out == {"skipped": "engine unavailable (ImportError: no module named nikita_tools)"}
    assert [_row(conn, v) for v in "abc"] == [(None, None)] * 3


def test_no_baseline_model_advances_and_writes_nothing(env):
    st, conn = env
    _three(conn)
    out = st.run_stage(cfg("live", engine="baseline", baseline_model=""))
    assert out["skipped"].startswith("no baseline model")
    assert [_row(conn, v) for v in "abc"] == [(None, None)] * 3


def test_cached_shadow_skip_is_not_stamped_when_an_override_appears(env):
    st, conn = env
    _three(conn)
    st.run_stage(cfg("shadow"), engine=Engine(skip={"b"}))
    conn.cursor().execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, first_seen, last_seen, status) "
        "VALUES ('liked1', 'liked1', 'c1', 'x', '2026-09-01', '2026-09-01', 'liked')"
    )
    conn.commit()
    out = st.run_stage(cfg("live"), engine=Engine(skip={"b"}))
    assert _row(conn, "b")[0] is None
    assert out["counts"].get("skipped", 0) == 0


def test_off_does_nothing(env):
    st, conn = env
    _three(conn)
    assert "off" in st.run_stage(cfg("off"), engine=Engine(skip={"a"}))["skipped"]


def test_restore_clears_only_junk_reasons_and_is_left_alone_after(env):
    st, conn = env
    _three(conn)
    _add(conn, "d", reason="US-only location", day=4)
    st.run_stage(cfg("live"), engine=Engine(skip={"b"}))
    out = st.restore(ids=["b", "d"])
    assert out["restored"] == ["b"]
    reason, junk = _row(conn, "b")
    assert reason is None and junk["restored"] is True
    assert _row(conn, "d")[0] == "US-only location"
    eng = Engine(skip={"b"})
    st.run_stage(cfg("live"), engine=eng)
    assert "b" not in eng.calls and _row(conn, "b")[0] is None


def test_restore_all_since(env):
    st, conn = env
    _three(conn)
    st.run_stage(cfg("live"), engine=Engine(skip={"a", "b"}))
    assert st.restore(since="2999-01-01")["count"] == 0
    assert st.restore(since="2000-01-01")["count"] == 2


def test_guarded_write_skips_a_role_scored_meanwhile(env):
    st, conn = env
    _add(conn, "a")
    conn.cursor().execute("UPDATE vacancy SET llm_score = 30 WHERE id = 'a'")
    conn.commit()
    assert st._stamp(conn, "a", "junk_filter: place 0.99") is False
    assert _row(conn, "a")[0] is None


def test_override_sends_the_role_to_scoring_without_a_call(env):
    st, conn = env
    _add(conn, "a")
    conn.cursor().execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, first_seen, last_seen, status) "
        "VALUES ('liked1', 'liked1', 'c1', 'x', '2026-09-01', '2026-09-01', 'liked')"
    )
    conn.commit()
    eng = Engine(skip={"a"})
    out = st.run_stage(cfg("live"), engine=eng)
    assert eng.calls == [] and out["counts"]["overridden"] == 1
    assert _row(conn, "a")[0] is None and _row(conn, "a")[1]["question"] == "override:status:liked"


def test_run_daily_places_the_stage_between_filter_and_screening_prep():
    import run_daily

    order = run_daily.STAGE_ORDER
    assert order.index("junk_filter") == order.index("filter") + 1
    assert order.index("screening_prep") > order.index("junk_filter")
    assert "junk_filter" in run_daily.HANDLERS and "junk_filter" in run_daily.STAGE_ABOUT


def test_handler_no_key_advances_with_note(monkeypatch):
    import subprocess

    import run_daily

    out = json.dumps({"skipped": "no key: TYPESAFE_API_KEY is unset, every role goes to scoring"})
    monkeypatch.setattr(run_daily, "_run_capture",
                        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, out, ""))
    kind, note = run_daily._h_junk_filter({}, {}, None)
    assert kind == "advance" and note.startswith("no key")


def test_handler_off_is_a_skip_not_an_advance(monkeypatch):
    import subprocess

    import run_daily

    out = json.dumps({"skipped": 'junk filter is off ([junk_filter] mode = "off")'})
    monkeypatch.setattr(run_daily, "_run_capture",
                        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, out, ""))
    kind, note = run_daily._h_junk_filter({}, {}, None)
    assert kind == "skip" and note.startswith("junk filter is off")


def test_handler_crash_does_not_stop_the_night(monkeypatch):
    import subprocess

    import run_daily

    monkeypatch.setattr(run_daily, "_run_capture",
                        lambda cmd, opts: subprocess.CompletedProcess(cmd, 1, "", "Traceback"))
    kind, _ = run_daily._h_junk_filter({}, {}, None)
    assert kind == "error_continue"


def test_settings_default_off_and_unknown_values_fall_back(tmp_path, monkeypatch):
    import settings

    toml = tmp_path / "defaults.toml"
    toml.write_text('[junk_filter]\nmode = "loud"\nengine = "gpt"\ncut = 7\n')
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(toml))
    settings.clear_cache()
    got = settings.junk_filter()
    assert (got["mode"], got["engine"], got["cut"], got["max_per_run"]) == ("off", "jev", 0.95, 200)
    assert (got["sample_pct"], got["live_since"]) == (10, "")
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(tmp_path / "missing.toml"))
    settings.clear_cache()
    assert settings.junk_filter()["mode"] == "off"
    settings.clear_cache()


def test_stage_is_a_trusted_pipeline_entrypoint():
    """run_daily runs it as a subprocess; without this every live write is ProdWriteBlocked."""
    import db_backend

    assert "junk_filter_stage.py" in db_backend._PIPELINE_ENTRYPOINTS


# --- U6: the Review-tab sample and the stop (R17, KTD10) ---------------------


def test_sample_draw_is_ten_percent_and_seeded():
    import junk_filter_stage as st

    ids = [f"v{i}" for i in range(20)]
    first = st.sample_skips(ids, 10, "2026-10-02")
    assert len(first) == 2 and set(first) <= set(ids)
    assert st.sample_skips(ids, 10, "2026-10-02") == first
    assert st.sample_skips([], 10, "x") == [] and st.sample_skips(ids, 0, "x") == []


def test_live_run_flags_ten_percent_of_its_skips_as_sampled(env):
    st, conn = env
    ids = [f"r{i:02d}" for i in range(20)]
    for v in ids:
        _add(conn, v)
    out = st.run_stage(cfg("live"), engine=Engine(skip=set(ids)))
    assert out["counts"]["skipped"] == 20
    sampled = [v for v in ids if _row(conn, v)[1].get("sampled")]
    assert len(sampled) == 2 and out["sampled"] == sorted(sampled)
    assert all(_row(conn, v)[1]["sampled_at"] for v in sampled)
    assert all(_row(conn, v)[0].startswith("junk_filter:") for v in ids)


def test_shadow_never_samples(env):
    st, conn = env
    _three(conn)
    st.run_stage(cfg("shadow", sample_pct=100), engine=Engine(skip={"b"}))
    assert not _row(conn, "b")[1].get("sampled")


def _verdict(conn, vid, verdict, at):
    conn.cursor().execute(
        "INSERT INTO judge_review (vacancy_id, source, verdict, draw, created_at, updated_at) "
        "VALUES (%s, 'review', %s, 'hard', %s, %s)", (vid, verdict, at, at))
    conn.commit()


def test_a_keep_on_a_sampled_role_after_live_since_forces_shadow(env):
    st, conn = env
    _three(conn)
    live = cfg("live", sample_pct=100, live_since="2026-10-01")
    st.run_stage(live, engine=Engine(skip={"b"}))
    assert _row(conn, "b")[1]["sampled"] is True
    _verdict(conn, "b", "keep", "2026-10-02 09:00:00")
    _add(conn, "d", day=4)
    out = st.run_stage(live, engine=Engine(skip={"d"}))
    assert out["mode"] == "shadow" and "b" in out["stopped"] and "keep" in out["stopped"]
    assert _row(conn, "d")[0] is None
    assert _row(conn, "d")[1]["would_skip"] is True and _row(conn, "d")[1]["mode"] == "shadow"
    assert out["junk_reasons_after"] == out["junk_reasons_before"]


def test_old_or_remove_verdicts_do_not_stop_the_stage(env):
    st, conn = env
    _three(conn)
    live = cfg("live", sample_pct=100, live_since="2026-10-01")
    st.run_stage(live, engine=Engine(skip={"a", "b"}))
    _verdict(conn, "a", "keep", "2026-09-30 23:00:00")   # before go-live
    _verdict(conn, "b", "remove", "2026-10-02 09:00:00")  # the filter was right
    assert st.stop_verdict(conn, "2026-10-01") is None
    _add(conn, "d", day=4)
    out = st.run_stage(live, engine=Engine(skip={"d"}))
    assert out["mode"] == "live" and "stopped" not in out
    assert _row(conn, "d")[0].startswith("junk_filter:")


def test_a_keep_on_an_unsampled_role_does_not_stop_the_stage(env):
    st, conn = env
    _three(conn)
    _verdict(conn, "a", "keep", "2026-10-02 09:00:00")
    assert st.stop_verdict(conn, "2026-10-01") is None


def test_handler_note_says_why_the_stage_ran_as_shadow(monkeypatch):
    import subprocess

    import run_daily

    out = json.dumps({"mode": "shadow", "engine": "jev", "counts": {"checked": 1},
                      "stopped": "wanted role in the Review sample: b (keep)"})
    monkeypatch.setattr(run_daily, "_run_capture",
                        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, out, ""))
    kind, note = run_daily._h_junk_filter({}, {}, None)
    assert kind == "advance" and "wanted role in the Review sample: b (keep)" in note
