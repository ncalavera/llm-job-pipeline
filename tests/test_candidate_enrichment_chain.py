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


def test_company_scoring_advances_when_no_candidates(rd, monkeypatch):
    run_daily, dal = rd
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    # No candidates seeded; the junk-filter + backfill run, then n == 0 -> advance.
    monkeypatch.setattr(run_daily, "_run", lambda cmd, opts: 0)
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
