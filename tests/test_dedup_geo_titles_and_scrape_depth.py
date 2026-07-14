"""Cross-variant dedup: geo title suffixes, plural titles, scrape-depth stubs.

Three real-world duplication stories the save path must fold to ONE row
(regression: a GWWC role lived as three rows — "Head of Community Engagement,
London" via a board, "Heads of Community Engagement" and "Head of Community
Engagement" via direct fetches; a second role forked a sibling because one
board shipped a 1.1k-char summary stub of an 11.5k-char JD):

  * a trailing comma/dash segment naming a KNOWN city/country/work mode is not
    part of the role identity ("Head of X, London" == "Head of X");
  * a plural retitle is not a new role ("Heads of X" == "Head of X");
  * two sources scraping the SAME posting with wildly different body lengths
    (full JD vs summary stub) must not fork a description-salted sibling —
    the fingerprint comparison is only trusted for comparably-sized bodies.

Guards that must survive:
  * a distinguishing comma qualifier stays ("Program Officer, Climate");
  * two distinct same-title roles with comparably-sized different bodies still
    fork a sibling.

Same SQLite harness as tests/test_dedup_renamed_roles.py. All orgs invented,
except title shapes mirroring the production regression.
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


_REQ_URL = "https://example.test/req/77"


def _commit(db):
    db.get_conn().commit()


def _job(title, *, city="Berlin, Germany", desc=None, url=None):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": desc
        or (f"We are hiring a {title}. " * 12 + "Own the work end to end."),
        "location": city,
        "url": url
        if url is not None
        else "https://example.test/job/" + title.lower().replace(" ", "-"),
    }


def _raw_rows(db):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT dedup_hash, title, status, full_description FROM vacancy")
    rows = cur.fetchall()
    cur.close()
    return rows


def _set_status(db, dedup_hash, status):
    cur = db.get_conn().cursor()
    cur.execute("UPDATE vacancy SET status = %s WHERE dedup_hash = %s", (status, dedup_hash))
    db.get_conn().commit()
    cur.close()


# ---------------------------------------------------------------------------
# Pure normalization contract
# ---------------------------------------------------------------------------


def test_geo_suffix_and_plural_fold_to_one_key(dal):
    """The production trio must produce ONE cross-variant key."""
    k = lambda t: dal.make_normalized_id("Acme Fund", t)
    assert (
        k("Head of Community Engagement, London")
        == k("Heads of Community Engagement")
        == k("Head of Community Engagement")
    )


def test_workmode_and_dash_geo_suffixes_fold(dal):
    k = lambda t: dal.make_normalized_id("Acme Fund", t)
    assert k("Operations Manager - Remote") == k("Operations Manager")
    assert k("Operations Manager, United Kingdom") == k("Operations Manager")
    # Stacked segments strip one by one: ", London, United Kingdom".
    assert k("Operations Manager, London, United Kingdom") == k("Operations Manager")


def test_non_geo_comma_qualifier_is_kept(dal):
    """A comma segment that names a FOCUS, not a place, distinguishes roles."""
    k = lambda t: dal.make_normalized_id("Acme Fund", t)
    assert k("Program Officer, Climate") != k("Program Officer, Health")
    assert k("Program Officer, Climate") != k("Program Officer")


def test_plural_fold_never_empties_title(dal):
    assert dal._normalize_title_strong("Heads") != ""
    # -ss / -us / -is endings are left alone, not mangled into other words.
    k = lambda t: dal.make_normalized_id("Acme Fund", t)
    assert k("Business Analyst") != k("Busines Analyst")


# ---------------------------------------------------------------------------
# Rename over time: geo-suffixed / plural variants merge onto the decided row
# ---------------------------------------------------------------------------


def test_geo_suffix_variant_merges_and_inherits_status(dal):
    dal.ensure_company("Acme Fund", status="active")
    dal.save_vacancies("Acme Fund", "A", [_job("Heads of Community Engagement", url=_REQ_URL)])
    _commit(dal)
    old_hash = dal.make_vacancy_id("Acme Fund", "Heads of Community Engagement")
    _set_status(dal, old_hash, "passed")

    new = dal.save_vacancies(
        "Acme Fund", "A", [_job("Head of Community Engagement, London", url=_REQ_URL)]
    )
    _commit(dal)

    raw = _raw_rows(dal)
    assert new == 0
    assert len(raw) == 1
    assert raw[0]["status"] == "passed"  # the user's decision survives the retitle
    assert raw[0]["title"] == "Head of Community Engagement, London"


# ---------------------------------------------------------------------------
# Scrape-depth guard: a summary stub of the same posting must not fork
# ---------------------------------------------------------------------------

# Full role-specific JD (~12k chars) vs a summary stub of the same posting
# (>1000 normalized chars so it still fingerprints, but ~8x shorter).
_FULL_JD = "Design and run the grants operations function end to end. " * 200
_STUB_JD = "Design and run the grants operations function. Apply on our site. " * 20
_OTHER_FULL_JD = "Lead a completely different portfolio with its own remit. " * 200


def test_short_stub_of_same_posting_folds_not_forks(dal):
    assert dal.description_fingerprint(_STUB_JD) is not None
    assert len(_FULL_JD) > 3 * len(_STUB_JD)

    dal.ensure_company("Acme Fund", status="active")
    dal.save_vacancies(
        "Acme Fund", "A", [_job("Grants Operations Associate", desc=_FULL_JD, url=_REQ_URL)]
    )
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Fund",
        "A",
        [_job("Grants Operations Associate", desc=_STUB_JD, url="https://board.test/j/1")],
    )
    _commit(dal)

    raw = _raw_rows(dal)
    assert new == 0
    assert len(raw) == 1
    # The richer body is kept on the merged row (save path may trim whitespace).
    assert raw[0]["full_description"].strip() == _FULL_JD.strip()


def test_comparable_distinct_bodies_still_fork_sibling(dal):
    """Two genuinely different same-title reqs (both full JDs) stay two rows."""
    dal.ensure_company("Acme Fund", status="active")
    dal.save_vacancies(
        "Acme Fund", "A", [_job("Program Officer", desc=_FULL_JD, url=_REQ_URL)]
    )
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Fund",
        "A",
        [_job("Program Officer", desc=_OTHER_FULL_JD, url="https://example.test/req/78")],
    )
    _commit(dal)

    assert new == 1
    assert len(_raw_rows(dal)) == 2
