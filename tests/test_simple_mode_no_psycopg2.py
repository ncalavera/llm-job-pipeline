"""Simple-mode smoke test: every DB-touching entry script imports and performs
a basic operation on a clean SQLite backend with **no psycopg2 available**.

This locks the easy-install contract: the SQLite path must never import the
compiled Postgres driver. We simulate its absence by pointing
``sys.modules['psycopg2']`` (and ``psycopg2.extras``) at ``None`` so any stray
``import psycopg2`` raises ``ImportError`` — the package stays physically
installed, only this view removes it. The scripts must therefore source their
``Json`` / ``RealDictCursor`` helpers from ``db_backend`` (whose SQLite branch
never touches psycopg2), not from ``psycopg2.extras`` directly.

Everything runs offline against a fresh temp SQLite DB (tmp_path).
"""

import importlib
import io
import json
import sys
import types

import pytest

# Import graph rebuilt per test so IS_SQLITE recomputes for the temp DB and the
# entry scripts re-import their db_backend-sourced helpers under the block.
_RESET = [
    "score_companies",
    "fetch_companies",
    "enrich_blind_vacancies",
    "triage",
    "database_supabase",
    "company_registry",
    "config",
    "db_conn",
    "db_backend",
    "filter_vacancies",
    "filters",
    "fetchers",
    "quality",
    "run_status",
    "prompts",
]


@pytest.fixture()
def simple(tmp_path, monkeypatch):
    """Clean SQLite backend with psycopg2 rendered un-importable."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    # Make psycopg2 un-importable for the duration of the test.
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", None)

    for mod in _RESET:
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "simple-mode tests must run on SQLite"

    import database_supabase as dal

    ns = type("SimpleEnv", (), {})()
    ns.dal = dal
    yield ns
    dal.close_conn()


def test_psycopg2_is_unimportable(simple):
    """The simulation genuinely removes psycopg2 from view."""
    with pytest.raises(ImportError):
        import psycopg2  # noqa: F401


def test_db_backend_reexports_shims_without_psycopg2(simple):
    """db_backend exposes Json / RealDictCursor from its SQLite branch."""
    import db_backend

    assert db_backend.Json({"a": 1}).dumps() == '{"a": 1}'
    assert db_backend.RealDictCursor is not None
    assert db_backend.get_conn() is not None


def test_score_companies_cmd_save_persists(simple):
    """score_companies imports and cmd_save writes enrichment to SQLite."""
    import score_companies

    dal = simple.dal
    cid = dal.ensure_company("Acme Robotics", status="candidate")
    dal.get_conn().commit()

    payload = [
        {
            "payload_kind": "company",
            "id": str(cid),
            "canonical_name": "Acme Robotics",
            "enrichment": {
                "about": {"description": "x", "sector": "Robotics"},
                "mission_fit": {"alignment_score": 70, "alignment_label": "ok"},
                "alignment_score": 70,
            },
        }
    ]
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        score_companies.cmd_save(types.SimpleNamespace(no_auto_review=True))
    finally:
        sys.stdin = old_stdin

    assert dal.load_company_enrichment("Acme Robotics")["alignment_score"] == 70


def test_fetch_companies_social_reaches_json_import(simple, monkeypatch):
    """fetch_companies imports and cmd_social runs the (formerly psycopg2) Json
    import path without psycopg2. _enrich_social_signals is stubbed to yield no
    data, so the loop `continue`s and no row is written — the point is that the
    `from db_backend import Json` line is reached and resolves under the block."""
    import fetch_companies

    dal = simple.dal
    cid = dal.ensure_company("Social Co", status="candidate")
    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE company SET website = %s WHERE id = %s", ("https://social.example", cid))
    cur.close()
    conn.commit()

    monkeypatch.setattr(fetch_companies, "_get_firecrawl_client", lambda: object())
    monkeypatch.setattr(fetch_companies, "_enrich_social_signals", lambda client, name: None)

    # Reaches `from db_backend import Json` (line before the loop) and returns
    # cleanly; no psycopg2 needed.
    fetch_companies.cmd_social(types.SimpleNamespace(company=None, limit=None))


def test_enrich_blind_vacancies_main_on_empty_db(simple, monkeypatch):
    """enrich_blind_vacancies imports and main() runs a DB read on an empty DB,
    returning before any Firecrawl call."""
    import enrich_blind_vacancies

    monkeypatch.setattr(sys, "argv", ["enrich_blind_vacancies.py"])
    enrich_blind_vacancies.main()  # no blind vacancies → clean return


def test_triage_update_vacancies_in_db(simple):
    """triage imports and update_vacancies_in_db writes via the RealDictCursor +
    Json shims on SQLite (its former psycopg2.extras import)."""
    import triage

    dal = simple.dal
    dal.ensure_company("Triage Co", status="active")
    dal.save_vacancies(
        "Triage Co",
        "A",
        [
            {
                "title": "Data Lead",
                "snippet": "Data Lead blurb.",
                "full_description": "We are hiring a Data Lead. " * 12,
                "location": "Berlin, Germany",
                # deliberately no "url" — update_vacancies_in_db fills it
            }
        ],
    )
    dal.get_conn().commit()

    vid = next(
        v_id
        for v_id, v in dal.load_vacancies(include_inactive_companies=True).items()
        if v["title"] == "Data Lead"
    )

    changed = triage.update_vacancies_in_db({vid: {"url": "https://triage.example/job"}})
    assert changed == 1

    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT locations FROM vacancy WHERE id = %s", (vid,))
    locs = cur.fetchone()[0]  # decoded from JSON TEXT by the shim
    cur.close()
    assert locs[0]["url"] == "https://triage.example/job"
