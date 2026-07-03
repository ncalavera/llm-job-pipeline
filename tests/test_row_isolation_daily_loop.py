"""One bad row must never abort the daily loop.

STRATEGY guardrail 1: reliability of the daily loop beats everything. A single
malformed row/entry is logged and skipped; the run continues with the good rows.

Each test feeds a hostile row/entry alongside a good one and asserts the save /
fetch SURVIVES and the good rows PERSIST. Every test here FAILS on origin/main
(the row aborts the run, or a bad value is stored) and PASSES after the fix.

Harness mirrors the rest of the suite: conftest clears SUPABASE_DB_URL, each
test points JOBSEARCH_DB_PATH at its own temp SQLite file and reloads the
backend/registry/DAL chain so it runs entirely on local SQLite (never Postgres).
"""

import importlib
import io
import json
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
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


def _good_ats_job(title, url):
    """A fetcher-shaped ATS row that clears the content gate."""
    return {
        "title": title,
        "snippet": "A genuine snippet long enough to clear the content gate.",
        "full_description": "Real long job description body here. " * 8,
        "location": "Berlin, Germany",
        "url": url,
    }


def _vacancy_row(db, dedup_hash):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT * FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
    row = cur.fetchone()
    cur.close()
    return row


def _total_vacancies(db):
    cur = db.get_conn().cursor()
    cur.execute("SELECT COUNT(*) FROM vacancy")
    n = cur.fetchone()[0]
    cur.close()
    return n


# ===========================================================================
# #8 — a null-title ATS row must not abort save_vacancies (save runs outside
#      the fetch error boundary; on origin/main _sanitize_title(None) raises a
#      TypeError in the pre-loop batch_hashes precompute and kills the run).
# ===========================================================================


def test_null_title_row_is_skipped_not_fatal(sqlite_dal):
    db = sqlite_dal
    db.ensure_company("Acme Robotics", status="active")
    db.get_conn().commit()

    good = _good_ats_job("Head of Community", "https://acme.example/job/hoc")
    bad = _good_ats_job(None, "https://acme.example/job/null")  # null title from the source

    # The malformed row is listed FIRST so the precompute crashes before any
    # good row is reached on origin/main.
    new = db.save_vacancies("Acme Robotics", "A", [bad, good])
    db.get_conn().commit()

    # The run survived, the good row persisted, the null-title row was skipped
    # (no blank-title vacancy inserted).
    assert new == 1
    assert _total_vacancies(db) == 1
    assert _vacancy_row(db, db.make_vacancy_id("Acme Robotics", "Head of Community")) is not None


# ===========================================================================
# #20 — a calendar-invalid ISO date must be dropped before persistence, not
#       stored verbatim (SQLite tolerates it; the canonical Postgres DATE column
#       rejects it and aborts). Backend-agnostic: no reliance on IS_SQLITE.
# ===========================================================================


def test_calendar_invalid_deadline_is_dropped_not_stored(sqlite_dal):
    db = sqlite_dal

    # Unit: an impossible calendar date is rejected; a real date still passes.
    assert db._safe_deadline("2026-02-30") is None  # Feb has no 30th
    assert db._safe_deadline("2026-13-01") is None  # no month 13
    assert db._safe_deadline("2026-06-15") == "2026-06-15"  # a genuine date survives

    db.ensure_company("DeadlineCo", status="active")
    db.get_conn().commit()

    job = _good_ats_job("Grants Officer", "https://x/grants")
    job["deadline"] = "2026-02-30"  # ISO-shaped, but not a real calendar date

    new = db.save_vacancies("DeadlineCo", "B", [job])
    db.get_conn().commit()

    assert new == 1
    row = _vacancy_row(db, db.make_vacancy_id("DeadlineCo", "Grants Officer"))
    # On origin/main SQLite stored "2026-02-30" verbatim (Postgres would have
    # rejected the DATE and aborted). After the fix the impossible date lands as
    # NULL on BOTH backends — the good row still persists.
    assert row["deadline"] is None


# ===========================================================================
# #19 — a null department + a company's department_exclude config must not
#       abort the run (on origin/main None.lower() in the exclude filter raises
#       and kills the whole daily run before any save).
# ===========================================================================

_CHAIN_PREFIXES = {
    "database_supabase",
    "config",
    "company_registry",
    "db_conn",
    "db_backend",
    "report",
    "fetchers",
    "fetch_vacancies",
    "run_status",
}


def test_null_department_with_exclude_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobsearch.db"))

    saved = {n: m for n, m in sys.modules.items() if n.split(".")[0] in _CHAIN_PREFIXES}
    for name in saved:
        sys.modules.pop(name, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    import database_supabase as db

    try:
        db.ensure_company("DeptCo", status="active")
        db.get_conn().commit()

        import fetch_vacancies as fv

        # Keep the run off disk (the fetch-log writer is the only file side effect
        # _fetch_one_company has of its own).
        monkeypatch.setattr(fv, "_save_fetch_log", lambda *a, **k: None)

        # A fetcher returning one excluded row AND one row whose department is
        # explicitly None — the crash trigger paired with a company-configured
        # department_exclude.
        jobs = [
            {
                "title": "Programme Lead",
                "department": "Programmes",
                "full_description": "Real long body. " * 12,
                "url": "https://x/1",
            },
            {
                "title": "Ops Analyst",
                "department": None,
                "full_description": "Real long body. " * 12,
                "url": "https://x/2",
            },
        ]
        monkeypatch.setattr(fv, "fetch_firecrawl_scrape", lambda *a, **k: list(jobs))

        config = {
            "strategy": "firecrawl_scrape",
            "url": "https://x",
            "department_exclude": ["programmes"],
            "tier": "B",
        }
        fetch_stats = {"orgs": {}, "errors": {}, "total_new": 0}

        new = fv._fetch_one_company("DeptCo", config, "B", "firecrawl_scrape", fetch_stats)
        db.get_conn().commit()

        # Excluded "Programmes" row filtered out; the null-department row kept
        # (department-less → matches no exclude term) — and nothing crashed.
        assert new == 1
        titles = {v["title"] for v in db.load_vacancies(include_inactive_companies=True).values()}
        assert "Ops Analyst" in titles
        assert "Programme Lead" not in titles
    finally:
        db.close_conn()
        for name in list(sys.modules):
            if name.split(".")[0] in _CHAIN_PREFIXES:
                sys.modules.pop(name, None)
        sys.modules.update(saved)


# ===========================================================================
# #7 — a scored entry missing member_ids must be skipped, never allowed to raise
#      a KeyError out of the --save loop: that aborts before the single batch
#      commit and rolls back EVERY good score already staged in the batch.
# ===========================================================================


def _seed_one_vacancy(db):
    db.ensure_company("Acme Robotics", status="active")
    db.save_vacancies("Acme Robotics", "A", [_good_ats_job("Head of Community", "https://x/hoc")])
    db.get_conn().commit()
    return next(iter(db.load_vacancies()))


def test_missing_member_ids_does_not_discard_the_batch(sqlite_dal, monkeypatch):
    db = sqlite_dal
    vid = _seed_one_vacancy(db)

    monkeypatch.setitem(
        sys.modules,
        "report",
        types.SimpleNamespace(generate_dashboard=lambda *a, **k: None),
    )

    payload = [
        {  # GOOD entry — staged first; must still be committed at the end.
            "member_ids": [vid],
            "org": "Acme Robotics",
            "title": "Head of Community",
            "score": 71,
            "reasoning": "Solid fit.",
            "short_summary": "A " * 120,
        },
        {  # MALFORMED entry — no member_ids. Must be skipped, not fatal.
            "org": "Ghost Co",
            "title": "Phantom Role",
            "score": 80,
            "reasoning": "Should not sink the batch.",
            "short_summary": "B " * 120,
        },
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    import score_vacancies

    importlib.reload(score_vacancies)
    # On origin/main the second entry raises KeyError('member_ids') before the
    # loop's single conn.commit() → the first entry's staged score rolls back.
    score_vacancies.cmd_save(types.SimpleNamespace(archive=False))

    assert db.load_vacancies()[vid]["llm_score"] == 71
