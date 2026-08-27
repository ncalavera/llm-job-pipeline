"""One exclusion pass that records its reason (migration 0020, R8).

filter_vacancies.persist_scoring_exclusions() is the single decider of "not
scored" for new vacancies: it writes vacancy.scoring_excluded_reason in ONE
UPDATE per run (set and clear in the same statement), and every reader of
"unscored" — load_vacancies(unscored_only=True), the candidate rescue, the
dashboard count, run_daily._unscored_unseen — excludes reasoned rows, so the
reason shown downstream is the reason that applied.

Each test runs on its own fresh temp SQLite DB with migration 0020 applied
from the real migration file (which doubles as the "migration applies on
SQLite" proof).
"""

import importlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

MIGRATION_SQLITE = (
    Path(__file__).resolve().parent.parent
    / "sql"
    / "migrations"
    / "0020_add_vacancy_scoring_excluded_reason.sqlite.sql"
)


# ---------------------------------------------------------------------------
# Harness — isolated temp SQLite (mirrors tests/test_filters_snapshot.py)
# ---------------------------------------------------------------------------


def _force_sqlite(monkeypatch, db_file):
    """Point the whole chain at a fresh temp SQLite file and reload it."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "exclusion tests must run on the SQLite backend"
    import database_supabase as db

    return db


def _apply_0020(db):
    """Apply the REAL 0020 SQLite migration file to the fresh baseline DB."""
    sql = MIGRATION_SQLITE.read_text(encoding="utf-8")
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    cur.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """(db, fv) — fresh migrated SQLite + reloaded filter_vacancies with a
    'united states' geo ban active (the fixture profile bans nothing)."""
    db = _force_sqlite(monkeypatch, tmp_path / "exclusion.db")
    _apply_0020(db)
    sys.modules.pop("filter_vacancies", None)
    import filter_vacancies as fv

    importlib.reload(fv)
    monkeypatch.setattr(fv, "_BANNED_COUNTRIES", frozenset({"united states"}))
    monkeypatch.setattr(fv, "_GEO_ACTIVE", True)
    yield db, fv
    db.close_conn()


def _seed(
    db,
    org,
    title,
    *,
    dedup_hash=None,
    desc="A real job description long enough to pass every content gate. " * 3,
    location="Berlin, Germany",
    url="https://example.test/job",
    status="unseen",
    llm_score=None,
    first_seen=None,
    reason=None,
):
    """Insert one vacancy row directly (bypassing merge gates) and return its id."""
    db.ensure_company(org, status="active")
    canonical = db.resolve_canonical_name(org)
    company_id = db.resolve_company_id(org)
    dedup_hash = dedup_hash or db.make_vacancy_id(canonical, title)
    today = (first_seen or date.today()).isoformat()
    loc = {"location": location, "url": url}
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO vacancy (dedup_hash, company_id, title, full_description, "
        "first_seen, last_seen, locations, status, llm_score, scoring_excluded_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            dedup_hash,
            company_id,
            title,
            desc,
            today,
            date.today().isoformat(),
            json.dumps([loc]),
            status,
            llm_score,
            reason,
        ),
    )
    cur.execute("SELECT id FROM vacancy WHERE dedup_hash = ?", (dedup_hash,))
    vid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return str(vid)


def _reason(db, vid):
    cur = db.get_conn().cursor()
    cur.execute("SELECT scoring_excluded_reason FROM vacancy WHERE id = ?", (vid,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _run_pass(fv):
    categories = fv.classify_vacancies()
    return fv.persist_scoring_exclusions(categories)


# ---------------------------------------------------------------------------
# Reason mapping (KTD4) — each excluded category writes its reason to the row
# ---------------------------------------------------------------------------


def test_us_only_location_gets_reason_and_loader_hides_it(env):
    """AE2: a US-only vacancy carries 'US-only location' and is not offered."""
    db, fv = env
    vid = _seed(db, "GiveWell", "Program Manager", location="New York, US only")

    result = _run_pass(fv)

    assert _reason(db, vid) == "US-only location"
    assert result["count"] == 1
    assert result["reasons"] == {"US-only location": 1}
    # The scorer's loader no longer returns the reasoned row ...
    assert vid not in db.load_vacancies(unscored_only=True)
    # ... but the filter pass itself still sees it (to re-decide next run).
    assert vid in db.load_vacancies(unscored_only=True, include_scoring_excluded=True)


def test_junk_title_gets_reason_with_matched_phrase(env):
    db, fv = env
    vid = _seed(db, "Acme Foundation", "Talent Pool — General Application")

    _run_pass(fv)

    assert _reason(db, vid) == "junk title: talent pool"


def test_junk_content_reason_names_the_junk(env):
    db, fv = env
    vid = _seed(db, "Gamma Trust", "Program Officer", desc="404 not found — page gone")

    _run_pass(fv)

    assert _reason(db, vid) == "junk content: error page"


def test_rearchived_row_gets_archived_before(env):
    db, fv = env
    vid = _seed(db, "Umbrella Corp", "Operations Lead")
    dedup = db.make_vacancy_id(db.resolve_canonical_name("Umbrella Corp"), "Operations Lead")
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO archived_hash (dedup_hash, reason) VALUES (?, ?)", (dedup, "manual"))
    conn.commit()
    cur.close()

    _run_pass(fv)

    assert _reason(db, vid) == "archived before"


def test_stale_blind_gets_no_description_reason(env):
    db, fv = env
    vid = _seed(
        db,
        "Zeta Group",
        "Stale Blind Role",
        desc="",
        first_seen=date.today() - timedelta(days=10),
    )

    _run_pass(fv)

    assert _reason(db, vid) == "no description after enrichment"


def test_fresh_blind_row_keeps_null_reason(env):
    """A reenrich_blind row is not excluded — it waits for enrichment."""
    db, fv = env
    vid = _seed(db, "Eta Labs", "Blind Role", desc="")

    _run_pass(fv)

    assert _reason(db, vid) is None


def test_company_title_filter_reason_is_the_filters_own(env, monkeypatch):
    db, fv = env
    import filters

    compiled = filters._build_company_title_include({"Initech": ["designer"]})
    monkeypatch.setattr(filters, "_COMPANY_TITLE_INCLUDE", compiled)
    vid = _seed(db, "Initech", "Data Analyst")

    _run_pass(fv)

    assert _reason(db, vid) == "company_title_filter — not in Initech include list"


# ---------------------------------------------------------------------------
# Group semantics — reasons follow the scorer's role group
# ---------------------------------------------------------------------------


def test_mixed_geo_group_keeps_null_and_is_scored(env):
    """A role with a US-only sibling AND a Berlin sibling stays scoreable."""
    db, fv = env
    vid_us = _seed(
        db, "MixedOrg", "Program Manager", dedup_hash="mixed-us", location="New York, US only"
    )
    vid_de = _seed(
        db, "MixedOrg", "Program Manager", dedup_hash="mixed-de", location="Berlin, Germany"
    )

    _run_pass(fv)

    assert _reason(db, vid_us) is None
    assert _reason(db, vid_de) is None
    loaded = db.load_vacancies(unscored_only=True)
    assert vid_us in loaded and vid_de in loaded


def test_all_us_group_gets_location_reason_on_every_member(env):
    db, fv = env
    vid_a = _seed(
        db, "AllUSOrg", "Program Manager", dedup_hash="us-a", location="New York, US only"
    )
    vid_b = _seed(db, "AllUSOrg", "Program Manager", dedup_hash="us-b", location="Boston, US")

    _run_pass(fv)

    assert _reason(db, vid_a) == "US-only location"
    assert _reason(db, vid_b) == "US-only location"


def test_group_level_reason_reaches_every_member(env):
    """A junk-titled role group writes the same reason to all its rows."""
    db, fv = env
    vid_a = _seed(db, "PoolOrg", "Talent Pool — General Application", dedup_hash="pool-a")
    vid_b = _seed(db, "PoolOrg", "Talent Pool — General Application", dedup_hash="pool-b")

    _run_pass(fv)

    assert _reason(db, vid_a) == "junk title: talent pool"
    assert _reason(db, vid_b) == "junk title: talent pool"


def test_mixed_category_group_decides_each_member_on_its_own(env):
    """A junk sibling gets its reason; the ready sibling stays scoreable —
    even when the junk row is the group's representative (longest desc)."""
    db, fv = env
    vid_junk = _seed(
        db,
        "SplitOrg",
        "Program Manager",
        dedup_hash="split-junk",
        desc="404 not found — this page is gone. " * 10,
    )
    vid_ready = _seed(db, "SplitOrg", "Program Manager", dedup_hash="split-ready")

    _run_pass(fv)

    assert _reason(db, vid_junk) == "junk content: error page"
    assert _reason(db, vid_ready) is None
    assert vid_ready in db.load_vacancies(unscored_only=True)


def test_excluded_sibling_of_a_ready_representative_keeps_its_reason(env):
    """When the representative is ready, an individually excluded sibling is
    still reasoned from its own row (not cleared by the ELSE NULL)."""
    db, fv = env
    vid_ready = _seed(db, "RepReadyOrg", "Program Manager", dedup_hash="rep-ready")
    vid_junk = _seed(
        db, "RepReadyOrg", "Program Manager", dedup_hash="rep-junk", desc="404 not found"
    )

    _run_pass(fv)

    assert _reason(db, vid_ready) is None
    assert _reason(db, vid_junk) == "junk content: error page"


# ---------------------------------------------------------------------------
# The -1 sentinel, clearing, and rows the pass must never touch
# ---------------------------------------------------------------------------


def test_sentinel_row_can_receive_a_reason(env):
    db, fv = env
    vid = _seed(db, "Acme Foundation", "Talent Pool — General Application", llm_score=-1)

    _run_pass(fv)

    assert _reason(db, vid) == "junk title: talent pool"


def test_sentinel_row_without_reason_is_offered(env):
    db, fv = env
    vid = _seed(db, "CleanOrg", "Backend Engineer", llm_score=-1)

    _run_pass(fv)

    assert _reason(db, vid) is None
    assert vid in db.load_vacancies(unscored_only=True)


def test_stale_reason_is_cleared_in_the_same_statement(env):
    """A row excluded by a rule that no longer matches is cleared next run."""
    db, fv = env
    vid = _seed(db, "CleanOrg", "Backend Engineer", reason="junk title: talent pool")

    _run_pass(fv)

    assert _reason(db, vid) is None
    assert vid in db.load_vacancies(unscored_only=True)


def test_row_at_demoted_company_keeps_its_reason(env):
    """The clear reaches only rows the pass re-decided: a reasoned row whose
    company left 'active' (invisible to classify_vacancies) is NOT cleared,
    while a stale reason at an active company still is."""
    db, fv = env
    vid_cand = _seed(db, "DemotedOrg", "Talent Pool — General Application", reason="junk title: talent pool")
    vid_active = _seed(db, "CleanOrg", "Backend Engineer", reason="stale reason")
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE company SET status = 'candidate' WHERE canonical_name = ?", ("DemotedOrg",))
    conn.commit()
    cur.close()

    _run_pass(fv)

    assert _reason(db, vid_cand) == "junk title: talent pool"
    assert _reason(db, vid_active) is None
    assert vid_cand not in db.load_candidate_vacancies_for_scoring()


def test_scored_and_non_unseen_rows_are_never_touched(env):
    db, fv = env
    vid_scored = _seed(db, "DoneOrg", "Scored Role", llm_score=80, reason="stale reason")
    vid_liked = _seed(db, "LikedOrg", "Liked Role", status="liked", reason="stale reason")

    _run_pass(fv)

    assert _reason(db, vid_scored) == "stale reason"
    assert _reason(db, vid_liked) == "stale reason"


# ---------------------------------------------------------------------------
# Every reader of "unscored" agrees
# ---------------------------------------------------------------------------


def test_candidate_loader_excludes_reasoned_rows(env):
    db, fv = env
    vid = _seed(db, "CandOrg", "Talent Pool — General Application")
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE company SET status = 'candidate' WHERE canonical_name = ?", ("CandOrg",))
    cur.execute(
        "UPDATE vacancy SET scoring_excluded_reason = ? WHERE id = ?",
        ("junk title: talent pool", vid),
    )
    conn.commit()
    cur.close()

    assert vid not in db.load_candidate_vacancies_for_scoring()


def test_unscored_unseen_count_excludes_reasoned_rows(env):
    db, fv = env
    _seed(db, "GiveWell", "Program Manager", location="New York, US only")
    vid_clean = _seed(db, "CleanOrg", "Backend Engineer")
    _run_pass(fv)

    sys.modules.pop("run_daily", None)
    import run_daily

    importlib.reload(run_daily)
    assert run_daily._unscored_unseen() == 1
    assert vid_clean in db.load_vacancies(unscored_only=True)


def test_dashboard_count_unscored_excludes_reasoned_rows(env):
    db, fv = env
    from report import data_prep

    all_vacs = {
        "a": {"llm_score": None, "status": "unseen"},
        "b": {"llm_score": None, "status": "unseen", "scoring_excluded_reason": "US-only location"},
        "c": {"llm_score": -1, "status": "unseen", "scoring_excluded_reason": None},
    }
    assert data_prep._count_unscored(all_vacs) == 2


# ---------------------------------------------------------------------------
# Wiring — prod-write guard, main() output, run_daily filter stage
# ---------------------------------------------------------------------------


def test_filter_vacancies_is_a_trusted_pipeline_entrypoint(env, monkeypatch):
    """The filter stage now WRITES (the reason column), so python3
    scripts/filter_vacancies.py must pass the prod-write guard without
    JOBSEARCH_ALLOW_PROD_WRITE."""
    import db_backend

    assert "filter_vacancies.py" in db_backend._PIPELINE_ENTRYPOINTS
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(db_backend.JOBSEARCH_ALLOW_PROD_WRITE_ENV, raising=False)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(
        "sys.argv", [str(db_backend.PROJECT_ROOT / "scripts" / "filter_vacancies.py")]
    )
    assert db_backend._prod_write_context_ok() is True


def test_h_filter_records_excluded_count_and_histogram(tmp_path, monkeypatch):
    """run_daily's filter stage keeps the exclusion tally in the run state
    (the digest header reads it later, R10)."""
    sys.modules.pop("run_daily", None)
    import run_daily as rd

    importlib.reload(rd)

    payload = {
        "ready": 4,
        "delete_ids": {"delete_geo": ["x"]},
        "scoring_excluded": {"count": 3, "reasons": {"US-only location": 2, "archived before": 1}},
    }

    class _Res:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""

    monkeypatch.setattr(rd, "_run_capture", lambda *a, **k: _Res())
    state = rd._new_state(rd.Opts())
    entry = rd._stage(state, "filter")
    kind, note = rd._h_filter(state, entry, rd.Opts())

    assert kind == "advance"
    assert entry["filter"]["excluded_count"] == 3
    assert entry["filter"]["excluded_reasons"] == {
        "US-only location": 2,
        "archived before": 1,
    }


def test_migration_applies_and_loader_query_runs_on_sqlite(env):
    """The real 0020 file applied cleanly (fixture) and the reasoned-row
    condition is live in the loader SQL."""
    db, fv = env
    assert db._vacancy_has_column("scoring_excluded_reason")
    assert db.load_vacancies(unscored_only=True) == {}
