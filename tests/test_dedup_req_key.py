"""Cross-source dedup anchored on the ATS requisition id (req key).

Production regression (2026-08-19): the SAME Ashby requisition at CEA reached
the DB twice — Probably Good stored the apply link with its utm decoration
GLUED onto the ashby_jid value with no separator
(".../careers?ashby_jid=<uuid>utm_source=PG_board"), so the normalized-URL
merge read the two boards' links as two different reqs. Nikita had already
applied through one row while the other sat in the browse queue. A second
same-day pair ("Director of Community Growth" vs "Director, Community Growth")
slipped through the same crack. Earlier same-family escapes: J-PAL / WFP / FHI
rows forked a body-salted sibling even though the existing row already carried
the SAME apply URL (the fork branch never consulted URLs).

Every observed dup shares one root: identity was derived from strings the
boards mangle (org spelling, title punctuation, URL decoration, body chrome).
The requisition id inside the apply URL survives every observed mangle.

Contract under test:

  * extract_req_key() reads the requisition id out of known ATS URL shapes,
    including Probably Good's corrupted glued form;
  * normalize_apply_url() salvages a glued utm tail;
  * the save path folds a same-req candidate onto the existing row even when
    title wording, URL decoration, and body chrome all differ;
  * the exact-hash fork branch folds (never forks a sibling) when the
    candidate's req key is already on the existing row;
  * a source that stamps ONE url onto several different roles never collapses
    them (title-overlap / body guard);
  * dedup_sweep clusters a same-req pair and auto-collapses it even when both
    rows are live.

Same SQLite harness as tests/test_dedup_url_normalization.py. Orgs invented.
"""

import importlib
import sys

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "dedup_sweep",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"
    import database_supabase as db

    yield db
    db.close_conn()


_UUID = "97c6c334-bd3d-4fc3-a3f9-3d3928c343d1"
_CLEAN = f"https://www.acme-institute.org/careers?ashby_jid={_UUID}"
_DECORATED = _CLEAN + "&utm_source=80000hours&utm_medium=job-board"
_GLUED = f"https://www.acme-institute.org/careers?ashby_jid={_UUID}utm_source=PG_board"


def _job(title, *, url, desc):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": desc,
        "location": "Remote",
        "url": url,
    }


def _full_desc(seed):
    return (f"We are hiring: {seed}. You will own strategy and execution. " * 12) + (
        "Salary band published. Apply via the careers page."
    )


def _raw_rows(db):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT id, dedup_hash, title, status, locations FROM vacancy")
    rows = cur.fetchall()
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Pure extraction contract
# ---------------------------------------------------------------------------


def test_extracts_ashby_req_from_clean_decorated_and_glued(dal):
    k = dal.extract_req_key
    assert k(_CLEAN) == f"ashby:{_UUID}"
    assert k(_DECORATED) == f"ashby:{_UUID}"
    assert k(_GLUED) == f"ashby:{_UUID}"
    assert k(f"https://jobs.ashbyhq.com/acme/{_UUID}") == f"ashby:{_UUID}"


def test_extracts_known_ats_families(dal):
    k = dal.extract_req_key
    assert k("https://job-boards.eu.greenhouse.io/acme/jobs/4757233101") == "greenhouse:4757233101"
    assert k("https://acme.com/careers?gh_jid=5302779008") == "greenhouse:5302779008"
    assert (
        k(
            "https://wfp.wd3.myworkdayjobs.com/job_openings/job/Cairo-Egypt/"
            "Supply-Chain-Expert--SSA--L10-_JR123456"
        )
        == "workday:jr123456"
    )
    assert k(f"https://jobs.lever.co/acme/{_UUID}") == f"lever:{_UUID}"
    assert (
        k("https://jobs.smartrecruiters.com/Acme/744000123471150-product-lead")
        == "smartrecruiters:744000123471150"
    )
    assert (
        k("https://uk.linkedin.com/jobs/view/head-of-change-at-acme-4436301661")
        == "linkedin:4436301661"
    )
    assert k(f"https://acme.pinpointhq.com/en/postings/{_UUID}") == f"pinpoint:{_UUID}"
    assert (
        k("https://www.idealist.org/en/nonprofit-job/a67db9e8e72e4547a97247e00fbe6feb-x")
        == "idealist:a67db9e8e72e4547a97247e00fbe6feb"
    )


def test_generic_uuid_fallback_and_no_key(dal):
    k = dal.extract_req_key
    assert k(f"https://careers.acme.org/postings/{_UUID}") == f"uuid:{_UUID}"
    assert k("https://acme.org/careers") is None
    assert k("") is None
    assert k(None) is None


def test_normalize_apply_url_salvages_glued_utm(dal):
    assert dal.normalize_apply_url(_GLUED) == _CLEAN.replace("https://www.", "https://www.")
    assert dal.normalize_apply_url(_GLUED) == dal.normalize_apply_url(_DECORATED)


# ---------------------------------------------------------------------------
# Save path: same req = one row
# ---------------------------------------------------------------------------


def test_same_req_folds_across_retitle_decoration_and_chrome(dal):
    """The CEA Director pair: existing PG row with a corrupted URL and board
    chrome in the body; the 80k copy arrives retitled and decorated."""
    body = _full_desc("community growth leadership")
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Director of Community Growth", url=_GLUED, desc=body + "\n\nSkills: x | y")],
    )
    dal.get_conn().commit()
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Director, Community Growth", url=_DECORATED, desc=body + " Board summary.")],
    )
    dal.get_conn().commit()
    rows = _raw_rows(dal)
    assert len(rows) == 1


def test_fork_branch_folds_on_shared_req_key(dal):
    """The J-PAL / WFP escape: same org+title, comparably sized but DIFFERENT
    bodies, same requisition URL — must fold, not fork a salted sibling."""
    dal.save_vacancies(
        "Acme Institute", "A", [_job("Product Lead", url=_GLUED, desc=_full_desc("v1 chrome"))]
    )
    dal.get_conn().commit()
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Product Lead", url=_DECORATED, desc=_full_desc("v2 entirely different wording"))],
    )
    dal.get_conn().commit()
    rows = _raw_rows(dal)
    assert len(rows) == 1


def test_stamped_shared_url_keeps_distinct_roles_apart(dal):
    """The Taptap Send case: a board stamps ONE apply URL onto several of a
    company's genuinely different roles — they must stay separate rows."""
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("General Manager", url=_CLEAN, desc=_full_desc("run the company"))],
    )
    dal.get_conn().commit()
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Head of East Africa", url=_CLEAN, desc=_full_desc("regional expansion work"))],
    )
    dal.get_conn().commit()
    rows = _raw_rows(dal)
    assert len(rows) == 2


def test_req_fold_inherits_user_decision(dal):
    """The applied-job regression: the surviving row keeps the decision."""
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Product Lead, Opportunities Board", url=_GLUED, desc=_full_desc("the board"))],
    )
    dal.get_conn().commit()
    row = _raw_rows(dal)[0]
    dal.update_vacancy_status(row["id"], "applied")
    dal.get_conn().commit()
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [
            _job(
                "Product Lead, Opportunities Board",
                url=_DECORATED,
                desc=_full_desc("board copy, other chrome"),
            )
        ],
    )
    dal.get_conn().commit()
    rows = _raw_rows(dal)
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"


# ---------------------------------------------------------------------------
# Sweep: same req clusters and auto-collapses even when both rows are live
# ---------------------------------------------------------------------------


def test_sweep_clusters_and_collapses_live_same_req_pair(dal):
    import dedup_sweep as ds

    body = _full_desc("one requisition, two boards")
    dal.save_vacancies(
        "Acme Institute", "A", [_job("Director of Community Growth", url=_GLUED, desc=body)]
    )
    dal.get_conn().commit()
    # Force the second variant in as its own row (bypassing the save-path fix)
    # to model the pre-fix prod state.
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, full_description, "
        "locations, status, first_seen, last_seen) "
        "SELECT 'dup-row-1', 'ffffffffffffffff', company_id, "
        "'Director, Community Growth', ?, ?, 'unseen', first_seen, last_seen "
        "FROM vacancy LIMIT 1",
        (body + " Other chrome.", '[{"url": "' + _DECORATED.replace("&", "&") + '"}]'),
    )
    dal.get_conn().commit()
    clusters = ds._cluster(ds._load_rows())
    assert len(clusters) == 1
    assert not ds._needs_manual_review(clusters[0])
