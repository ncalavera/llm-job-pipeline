"""Writes to the DB are predictable — report-only never mutates.

Three guarantees, all exercised on a throwaway temp SQLite (no Supabase, no
network):

1. ``fetch_vacancies.py --report-only`` is report-ONLY: it re-renders the
   dashboard but never mutates source data. An expired 'unseen' row that a real
   fetch run would auto-pass is left byte-for-byte untouched, even though
   ``generate_dashboard()`` runs (and, in full mode, commits the snapshot).

2. One commit rule with documented exceptions. ``auto_review_candidates()``
   stages its writes and leaves the commit to the caller (a rollback drops an
   uncommitted approve/reject) — the DAL contract, no silent internal commit.

3. Archive atomicity. ``archive_vacancies(force=True)`` OWNS its transaction:
   it commits the DELETE itself and only THEN writes the on-disk JSON, so the
   two can't diverge and nothing relies on an "accidental" caller/snapshot
   commit. The score→archive→dashboard path loses nothing.
"""

import importlib
import json
import sys


# ---------------------------------------------------------------------------
# Backend reset: bind the whole module chain to a fresh temp SQLite file.
# ---------------------------------------------------------------------------

_CHAIN_PREFIXES = {
    "database_supabase",
    "config",
    "company_registry",
    "db_conn",
    "db_backend",
    "report",  # package + submodules (report.data_prep, report.packs, ...)
    "fetchers",
    "fetch_vacancies",
    "score_vacancies",
    "run_status",
}


def _reset_backend(monkeypatch, db_file):
    """Point the backend at a fresh temp SQLite DB and drop stale module state."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    for name in list(sys.modules):
        if name.split(".")[0] in _CHAIN_PREFIXES:
            sys.modules.pop(name, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "these tests must run on the SQLite backend"

    import database_supabase as db

    return db


def _job(title, city="Berlin, Germany"):
    return {
        "title": title,
        "snippet": f"{title} blurb.",
        "full_description": f"We are hiring a {title}. " * 12,
        "location": city,
        "url": f"https://acme.example/job/{title.lower().replace(' ', '-')}",
    }


def _id_by_title(db, title):
    for vid, v in db.load_vacancies(include_inactive_companies=True).items():
        if v["title"] == title:
            return vid
    raise AssertionError(f"vacancy {title!r} not found")


def _set(db, vid, **cols):
    cur = db.get_conn().cursor()
    sets = ", ".join(f"{k} = %s" for k in cols)
    cur.execute(f"UPDATE vacancy SET {sets} WHERE id = %s", list(cols.values()) + [vid])
    cur.close()
    db.get_conn().commit()


def _dump_vacancies(db):
    """Full, JSON-comparable snapshot of the vacancy table (the source data)."""
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT * FROM vacancy ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    return json.loads(json.dumps(rows, default=str, sort_keys=True))


# ---------------------------------------------------------------------------
# 1. --report-only never mutates source data
# ---------------------------------------------------------------------------


def test_report_only_gives_zero_data_diff(tmp_path, monkeypatch):
    db = _reset_backend(monkeypatch, tmp_path / "jobsearch.db")
    try:
        # Seed an expired, unseen, low-fit role — exactly what a real fetch run
        # would auto-pass (status → 'passed') at the end of main().
        db.ensure_company("Acme Robotics", status="active")
        db.save_vacancies("Acme Robotics", "A", [_job("Filing Assistant")])
        db.get_conn().commit()
        vid = _id_by_title(db, "Filing Assistant")
        _set(db, vid, llm_score=10, deadline="2020-01-01")

        before = _dump_vacancies(db)
        assert before and before[0]["status"] == "unseen", "fixture must start 'unseen'"

        # Redirect the dashboard sink so the real public/data.js is never touched.
        import config
        import report
        import fetch_vacancies

        out_dir = tmp_path / "public_out"
        out_dir.mkdir()
        monkeypatch.setattr(config, "PUBLIC_DIR", out_dir, raising=False)
        monkeypatch.setattr(report, "PUBLIC_DIR", out_dir, raising=False)
        monkeypatch.setattr(fetch_vacancies, "PUBLIC_DIR", out_dir, raising=False)

        # Run the real report-only path end-to-end.
        monkeypatch.setattr(sys, "argv", ["fetch_vacancies.py", "--report-only"])
        fetch_vacancies.main()

        after = _dump_vacancies(db)
        assert after == before, "--report-only mutated source data"

        # report-only still did its ONE job: it (re)generated the dashboard sink.
        assert (out_dir / "data.js").exists(), "--report-only must regenerate data.js"

        # Control — prove the fixture was genuinely expirable, so the zero diff
        # above is meaningful and not vacuous: a real pass sweep DOES flip it.
        db.pass_expired_vacancies()
        db.get_conn().commit()
        control = _dump_vacancies(db)
        assert control != before, "fixture was not expirable — the test is vacuous"
        assert control[0]["status"] == "passed"
    finally:
        db.close_conn()


# ---------------------------------------------------------------------------
# 2. auto_review_candidates stages, caller commits (one rule, no silent commit)
# ---------------------------------------------------------------------------


def test_auto_review_requires_caller_commit(tmp_path, monkeypatch):
    db = _reset_backend(monkeypatch, tmp_path / "jobsearch.db")
    try:
        db.ensure_company("Highfit Inc", status="candidate")
        db.save_company_enrichment("Highfit Inc", alignment_score=80)
        db.get_conn().commit()

        result = db.auto_review_candidates(approve_threshold=60, reject_threshold=25, enabled=True)
        assert "Highfit Inc" in result["approved"]

        # The DAL did NOT commit — a rollback must drop the staged approval.
        db.get_conn().rollback()
        assert db.get_company_fitness_map()["Highfit Inc"]["status"] == "candidate"

        # With the caller's commit, the approval persists across a rollback.
        db.auto_review_candidates(approve_threshold=60, reject_threshold=25, enabled=True)
        db.get_conn().commit()
        db.get_conn().rollback()
        assert db.get_company_fitness_map()["Highfit Inc"]["status"] == "active"
    finally:
        db.close_conn()


# ---------------------------------------------------------------------------
# 3. score → archive → dashboard loses nothing without an outside commit
# ---------------------------------------------------------------------------


def test_archive_self_commits_then_writes_json(tmp_path, monkeypatch):
    db = _reset_backend(monkeypatch, tmp_path / "jobsearch.db")
    try:
        # Keep the archive JSON off the real repo tree.
        monkeypatch.setattr(db, "VACANCIES_DIR", tmp_path, raising=False)

        db.ensure_company("Acme Robotics", status="active")
        db.save_vacancies("Acme Robotics", "A", [_job("Low Fit Role"), _job("Strong Fit Role")])
        db.get_conn().commit()
        low = _id_by_title(db, "Low Fit Role")
        keep = _id_by_title(db, "Strong Fit Role")
        _set(db, low, llm_score=10)  # below LLM_SCORE_THRESHOLD (20) → archivable
        _set(db, keep, llm_score=90)  # above threshold → survives

        # Archive WITHOUT any surrounding caller/snapshot commit.
        archived = db.archive_vacancies(force=True)
        assert archived == [low]

        # The DELETE was committed by archive itself: a rollback can't resurrect
        # the row (no reliance on an "accidental" side-commit).
        db.get_conn().rollback()
        live = db.load_vacancies(include_inactive_companies=True)
        assert low not in live, "archived row must be gone for good"
        assert keep in live, "above-threshold row must survive"

        # The on-disk JSON was written AFTER the commit and matches the delete.
        archive_files = list((tmp_path / "archive").glob("archived_*.json"))
        assert len(archive_files) == 1
        payload = json.loads(archive_files[0].read_text(encoding="utf-8"))
        assert low in payload["vacancies"]
        assert keep not in payload["vacancies"]

        # The dashboard regenerates cleanly and shows only the surviving role.
        import config
        import report

        out_dir = tmp_path / "public_out"
        out_dir.mkdir()
        monkeypatch.setattr(config, "PUBLIC_DIR", out_dir, raising=False)
        monkeypatch.setattr(report, "PUBLIC_DIR", out_dir, raising=False)
        report.generate_dashboard()

        data_js = (out_dir / "data.js").read_text(encoding="utf-8")
        assert "Strong Fit Role" in data_js
        assert "Low Fit Role" not in data_js
    finally:
        db.close_conn()
