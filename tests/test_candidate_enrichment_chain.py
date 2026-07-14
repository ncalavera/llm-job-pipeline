"""The company_scoring stage runs the full candidate chain (find site + evidence).

Before WANT-scoring, run_daily's company_scoring stage must:
  1. drop structural junk       (filter_companies.py --apply)
  2. find missing websites      (find_company_urls.py)         <- pt 2
  3. scrape about-pages         (fetch_companies.py --limit N)
  4. collect primary evidence   (collect_company_evidence.py)  <- pt 1
  5. build WANT payloads        (score_companies.py --local)

These tests stub the subprocess boundary (_run / _run_capture) and assert the
deterministic ORDER of the shelled commands, plus the FIRECRAWL-unset short
circuit. DB helpers run for real on an isolated temp SQLite DB.
"""

import importlib
import subprocess
import sys

import pytest


def _force_sqlite_and_run_daily(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobsearch.db"))
    for mod in (
        "run_daily",
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
    import run_daily

    importlib.reload(run_daily)
    monkeypatch.setattr(run_daily, "STATE_PATH", tmp_path / "run_state.json")
    monkeypatch.setattr(run_daily, "FETCH_STATS_PATH", tmp_path / "fetch_stats.json")
    monkeypatch.setattr(run_daily, "CO_PAYLOAD_PATH", tmp_path / "co_payload.json")
    return run_daily, dal


@pytest.fixture()
def rd(monkeypatch, tmp_path):
    run_daily, dal = _force_sqlite_and_run_daily(monkeypatch, tmp_path)
    yield run_daily, dal
    dal.close_conn()


# A benign successful screen record (llm_ran, nothing pending): the stubbed _run
# in these chain tests never writes the real screen summary file, so the driver's
# money valve would otherwise (correctly) treat the screen as crashed. These tests
# assert the PAID chain order, not the screen, so we stub a clean screen result.
_OK_SCREEN = {
    "total": 0,
    "screened": 0,
    "llm_ran": True,
    "drops": 0,
    "dup_drops": 0,
    "llm_drops": 0,
    "fail_safe_keeps": 0,
    "pending_count": 0,
}


def _seed_candidate(dal, name, website):
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO company (canonical_name, status, website) VALUES (%s, 'candidate', %s)",
        (name, website),
    )
    dal.get_conn().commit()
    cur.execute("SELECT id FROM company WHERE canonical_name = %s", (name,))
    cid = cur.fetchone()[0]
    cur.close()
    return cid


def test_ghost_and_names_helpers(rd):
    run_daily, dal = rd
    scored = _seed_candidate(dal, "Nova Harbor", "https://novaharbor.org")
    _seed_candidate(dal, "Ghost Org", "")  # no website -> ghost

    assert run_daily._ghost_candidate_count() == 1
    names = run_daily._candidate_names_to_score(10)
    assert names == ["Nova Harbor"]  # only the website-bearing candidate is scorable
    assert str(scored)  # id exists


def test_company_scoring_runs_full_chain_in_order(rd, monkeypatch):
    run_daily, dal = rd
    cid = _seed_candidate(dal, "Nova Harbor", "https://novaharbor.org")
    _seed_candidate(dal, "Ghost Org", "")  # exercises the backfill branch
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    calls = []

    def fake_run(cmd, opts):
        calls.append(cmd)
        return 0

    def fake_run_capture(cmd, opts):
        calls.append(cmd)
        payloads = [
            {
                "payload_kind": "company",
                "id": str(cid),
                "canonical_name": "Nova Harbor",
                "url": "https://novaharbor.org",
                "system_prompt": "sp",
                "user_msg": "um",
            }
        ]
        import json

        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payloads), stderr="")

    monkeypatch.setattr(run_daily, "_run", fake_run)
    monkeypatch.setattr(run_daily, "_run_capture", fake_run_capture)
    monkeypatch.setattr(run_daily, "_screen_candidates", lambda opts: _OK_SCREEN)

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, payload = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "gate"
    assert payload["action"] == "score_companies"

    joined = [" ".join(str(x) for x in c) for c in calls]

    # The five steps appear in the required order.
    def idx(substr):
        return next(i for i, s in enumerate(joined) if substr in s)

    assert idx("filter_companies.py") < idx("find_company_urls.py")
    assert idx("find_company_urls.py") < idx("fetch_companies.py")
    assert idx("fetch_companies.py") < idx("collect_company_evidence.py")
    assert idx("collect_company_evidence.py") < idx("score_companies.py")
    # evidence collected for exactly the scorable set (the website-bearing candidate)
    assert any("collect_company_evidence.py" in s and "Nova Harbor" in s for s in joined)


def test_company_scoring_skips_when_firecrawl_unset(rd, monkeypatch):
    run_daily, dal = rd
    _seed_candidate(dal, "Nova Harbor", "https://novaharbor.org")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)
    monkeypatch.setattr(
        run_daily,
        "_run_capture",
        lambda cmd, opts: (
            calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        ),
    )

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, note = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "skip"
    assert "FIRECRAWL_API_KEY unset" in note
    # No enrichment subprocess ran without a key.
    assert calls == [], "no find/collect/score subprocess should run without Firecrawl"


def test_backfill_caps_ghost_search_at_max_per_run(rd, monkeypatch):
    """_backfill_candidate_websites must never let find_company_urls.py run
    unbounded: it fires one paid Firecrawl search() per ghost candidate
    (STRATEGY guardrail 3: cost), so it is capped at the same per-run safety
    net already used for scoring (scoring_settings.max_per_run). More ghosts
    than the cap must still call find_company_urls.py once with --limit set
    to the cap, and print how many were deferred (no silent caps)."""
    import scoring_settings

    monkeypatch.setattr(scoring_settings, "max_per_run", lambda: 2)
    run_daily, dal = rd
    for i in range(5):
        _seed_candidate(dal, f"Ghost Org {i}", "")  # no website -> ghost

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)

    run_daily._backfill_candidate_websites(run_daily.Opts())

    assert len(calls) == 1
    cmd = calls[0]
    assert "find_company_urls.py" in " ".join(cmd)
    assert cmd[-2:] == ["--limit", "2"], cmd


def test_backfill_skips_limit_flag_reasoning_when_under_cap(rd, monkeypatch, capsys):
    """Fewer ghosts than the cap still passes the exact ghost count as --limit
    (a no-op cap) and prints the plain (uncapped) message, not the deferred one."""
    import scoring_settings

    monkeypatch.setattr(scoring_settings, "max_per_run", lambda: 50)
    run_daily, dal = rd
    _seed_candidate(dal, "Ghost Org", "")

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)

    run_daily._backfill_candidate_websites(run_daily.Opts())

    assert calls[0][-2:] == ["--limit", "1"]
    out = capsys.readouterr().out
    assert "deferred" not in out


def test_company_scoring_caps_paid_chain_at_max_per_run(rd, monkeypatch, capsys):
    """company_scoring must cap the whole PAID chain — Firecrawl scrape,
    evidence collection, WANT-scoring — at scoring_settings.max_per_run()
    (STRATEGY guardrail 3: cost). The bug: the stage passed the UNCAPPED candidate
    count as an explicit --limit to score_companies, and score_companies only
    applies its own cap when --limit is None, so the cap was always bypassed. All
    three paid steps must receive the capped count/names, and the deferred count
    must be reported (no silent drops)."""
    import scoring_settings

    monkeypatch.setattr(scoring_settings, "max_per_run", lambda: 2)
    run_daily, dal = rd
    ids = [_seed_candidate(dal, f"Cand {i}", f"https://cand{i}.org") for i in range(5)]
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)
    monkeypatch.setattr(run_daily, "_screen_candidates", lambda opts: _OK_SCREEN)

    def fake_run_capture(cmd, opts):
        calls.append(cmd)
        import json

        payloads = [
            {
                "payload_kind": "company",
                "id": str(ids[i]),
                "canonical_name": f"Cand {i}",
                "url": f"https://cand{i}.org",
                "system_prompt": "sp",
                "user_msg": "um",
            }
            for i in range(2)
        ]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payloads), stderr="")

    monkeypatch.setattr(run_daily, "_run_capture", fake_run_capture)

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, payload = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "gate"

    def cmd_with(substr):
        return next(c for c in calls if any(substr in str(x) for x in c))

    # Firecrawl scrape capped at 2, not the 5 available.
    fetch = cmd_with("fetch_companies.py")
    assert fetch[-2:] == ["--limit", "2"], fetch
    # WANT-scoring capped at 2 too — passing 5 here suppressed score_companies'
    # own internal cap (the actual bug this test guards against).
    score = cmd_with("score_companies.py")
    assert score[-2:] == ["--limit", "2"], score
    # Evidence collected for EXACTLY the capped set (2 names), not all 5.
    evidence = cmd_with("collect_company_evidence.py")
    names_arg = evidence[evidence.index("--company") + 1]
    assert names_arg.split(",") == ["Cand 0", "Cand 1"], names_arg
    # Deferral reported to the run output (3 = 5 available − 2 cap).
    assert "3 deferred to a later run" in capsys.readouterr().out
    # And surfaced at the scoring gate note.
    assert "3 candidate(s) deferred" in payload["instructions"]


def test_company_scoring_closes_valve_when_screen_crashes(rd, monkeypatch):
    """Money valve (R2): a crashed cheap screen (returns None) withholds ALL paid
    enrichment this cycle — no website search, scrape, or evidence subprocess runs
    — records a BLOCKING warning, and returns a skip with the withheld-count note."""
    run_daily, dal = rd
    _seed_candidate(dal, "Nova Harbor", "https://novaharbor.org")
    _seed_candidate(dal, "Ghost Org", "")  # a ghost too
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    calls = []
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: calls.append(cmd) or 0)
    monkeypatch.setattr(
        run_daily,
        "_run_capture",
        lambda cmd, opts: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "[]", ""),
    )
    # Screen crashed: no run marker written → _screen_candidates returns None.
    monkeypatch.setattr(run_daily, "_screen_candidates", lambda opts: None)

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, note = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "skip"
    assert "paid enrichment withheld" in note
    # No PAID enrichment subprocess ran (only the free junk prefilter may have).
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert not any(
        s
        for s in joined
        if "find_company_urls" in s
        or "fetch_companies" in s
        or "collect_company_evidence" in s
        or "score_companies" in s
    )
    # A blocking warning was recorded → publish gate will treat the run as dirty.
    blocking = [w for w in state["warnings"] if w["blocking"]]
    assert blocking and "withheld" in blocking[0]["message"]


def test_company_scoring_advances_when_no_candidates(rd, monkeypatch):
    run_daily, dal = rd
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    # No candidates seeded; the junk-filter + backfill run, then n == 0 -> advance.
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: 0)
    monkeypatch.setattr(run_daily, "_screen_candidates", lambda opts: _OK_SCREEN)
    monkeypatch.setattr(
        run_daily,
        "_run_capture",
        lambda cmd, opts: subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr=""),
    )

    state = run_daily._new_state(run_daily.Opts())
    entry = run_daily._stage(state, "company_scoring")
    kind, note = run_daily._h_company_scoring(state, entry, run_daily.Opts())

    assert kind == "advance"
    assert "no new candidate companies" in note
