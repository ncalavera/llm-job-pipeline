"""Characterization tests for ``save_board_vacancies`` (database_supabase.py).

These pin the CURRENT behaviour of the job-board merge path so a later refactor
can lean on them as a safety net. They assert what the code does TODAY, not what
it ideally should do — where the documented expectation diverged from reality the
docstring records the real behaviour (see scenario 3 + 6).

Harness mirrors tests/test_e2e_pipeline.py: conftest clears SUPABASE_DB_URL, each
test points JOBSEARCH_DB_PATH at its own temp SQLite file and reloads the
backend/registry/DAL chain so it runs entirely on local SQLite (never Postgres).

Order of gates inside save_board_vacancies (load-bearing for these tests):
    _gate_description → filters.title_words_blacklisted(title)
    → filters.has_enough_content → filters.is_content_junk
    → external_id batch-dedup → resolve/ensure company → inactive-company skip
    → filters.is_recently_archived (vs get_archived_hashes(include_gone=True))
    → locations merge / insert
"""

import importlib
import json
import sys

import pytest


# ---------------------------------------------------------------------------
# Fixtures — isolated temp SQLite DB + clean module chain (copied from e2e)
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
    assert db_backend.IS_SQLITE, "characterization tests must run on SQLite"
    import database_supabase as db

    return db


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    """DAL bound to an isolated temp SQLite database."""
    db = _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    yield db
    db.close_conn()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _board(name):
    return {"name": name, "url": "https://board.test/feed", "tier": "B"}


def _job(
    title,
    *,
    org=None,
    city="Berlin, Germany",
    url=None,
    snippet="A genuine snippet long enough to clear the content gate here.",
    desc="Real long job description body. " * 5,
    **extra,
):
    """Build one fetcher-shaped board job dict.

    Defaults clear _has_enough_content (snippet + desc both >= 50 chars) and
    _is_content_junk (no recaptcha / donation / error markers).
    """
    job = {"title": title, "location": city, "snippet": snippet, "full_description": desc}
    if org is not None:
        job["org_override"] = org
    if url is not None:
        job["url"] = url
    job.update(extra)
    return job


def _seed_company(dal, name, status="active"):
    dal.ensure_company(name, status=status)
    dal.get_conn().commit()


def _row_count(dal, dedup_hash):
    cur = dal.get_conn().cursor()
    cur.execute("SELECT COUNT(*) FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
    n = cur.fetchone()[0]
    cur.close()
    return n


def _vacancy_row(dal, dedup_hash):
    cur = dal.get_conn().cursor(cursor_factory=dal.RealDictCursor)
    cur.execute("SELECT * FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
    row = cur.fetchone()
    cur.close()
    return row


def _locations(row):
    locs = row["locations"]
    return json.loads(locs) if isinstance(locs, str) else (locs or [])


# ===========================================================================
# 1. Batch dedup by external_id within one merge call
# ===========================================================================


def test_batch_dedup_by_external_id(dal):
    """Two jobs sharing (board_name, external_id) in ONE batch → the second is
    dropped via seen_ext_ids; only the first inserts. Returns 1 new.
    """
    _seed_company(dal, "DupCo")
    jobs = [
        _job("Dup Role A", org="DupCo", external_id="EXT-1"),
        _job("Dup Role B", org="DupCo", external_id="EXT-1"),
    ]
    new = dal.save_board_vacancies(_board("DupCo"), jobs)
    dal.get_conn().commit()

    assert new == 1
    assert _row_count(dal, dal.make_vacancy_id("DupCo", "Dup Role A")) == 1
    # The second job with the duplicate external_id never reached insert.
    assert _row_count(dal, dal.make_vacancy_id("DupCo", "Dup Role B")) == 0


def test_title_geo_suffix_dedups_to_one_row(dal):
    """Two jobs for the same role, one titled "... (Remote)", dedup to a single
    row: the trailing work-mode suffix is stripped before hashing. Different
    external_ids, so this is the title normaliser, not external_id dedup.
    """
    _seed_company(dal, "SuffixCo")
    jobs = [
        _job("Ops Lead", org="SuffixCo", external_id="EXT-A"),
        _job("Ops Lead (Remote)", org="SuffixCo", external_id="EXT-B"),
    ]
    dal.save_board_vacancies(_board("SuffixCo"), jobs)
    dal.get_conn().commit()

    assert _row_count(dal, dal.make_vacancy_id("SuffixCo", "Ops Lead")) == 1


def test_normalize_title_for_dedup_keeps_non_geo_parens():
    import database_supabase as db

    # stripped: work-mode / location qualifiers and stray whitespace
    assert db._normalize_title_for_dedup("Ops Lead (Remote)") == "Ops Lead"
    assert db._normalize_title_for_dedup("Analyst (Remote, United States)") == "Analyst"
    assert db._normalize_title_for_dedup("Manager (Hybrid)") == "Manager"
    assert db._normalize_title_for_dedup("Consultant -  Grants") == "Consultant - Grants"
    # preserved: parentheticals that distinguish genuinely different roles
    assert db._normalize_title_for_dedup("Teacher (Spanish)") == "Teacher (Spanish)"
    assert db._normalize_title_for_dedup("Lead (Maternity Cover)") == "Lead (Maternity Cover)"


# ===========================================================================
# 2. Inactive company → skipped, never inserted
# ===========================================================================


def test_inactive_company_skipped(dal):
    """A job whose resolved company has status='inactive' is counted in
    skipped_inactive and never inserted; new_count stays 0.
    """
    dal.ensure_company("InactiveCo", status="active")
    cur = dal.get_conn().cursor()
    cur.execute("UPDATE company SET status='inactive' WHERE canonical_name=%s", ("InactiveCo",))
    dal.get_conn().commit()
    cur.close()

    new = dal.save_board_vacancies(_board("InactiveCo"), [_job("Inactive Role", org="InactiveCo")])
    dal.get_conn().commit()

    assert new == 0
    assert _row_count(dal, dal.make_vacancy_id("InactiveCo", "Inactive Role")) == 0


# ===========================================================================
# 3. Auto-created company status -- SAME rule regardless of backend or
#    whether the name happens to be "known" (STRATEGY guardrail 2: no
#    IS_SQLITE branching, no "known name" fast lane around the review gate).
# ===========================================================================


def test_unknown_company_created_candidate_in_sqlite(dal):
    """An unknown org is created with status=_auto_discovery_status(), which
    defaults to 'candidate' -- identical to the Postgres/full-mode rule.
    """
    assert dal._auto_discovery_status() == "candidate"  # default, both backends
    new = dal.save_board_vacancies(_board("BrandNewCo"), [_job("New Role")])
    dal.get_conn().commit()
    assert new == 1
    cur = dal.get_conn().cursor()
    cur.execute("SELECT status FROM company WHERE canonical_name=%s", ("BrandNewCo",))
    assert cur.fetchone()[0] == "candidate"
    cur.close()


def test_via_prefixed_company_created_candidate_not_fast_tracked(dal):
    """An org_override starting with '[via ' (an aggregator placeholder the
    board scraper emits when it can't extract a real org name) is created
    'candidate' like any other new name -- it is NOT special-cased to
    'active'. filter_companies.py's aggregator check prunes it later, from the
    candidate pool, exactly like any other structural junk company.
    """
    via_org = "[via SomeBoard] Imaginary Org"
    new = dal.save_board_vacancies(_board("ViaBoard"), [_job("Via Role", org=via_org)])
    dal.get_conn().commit()
    assert new == 1
    canonical = dal.resolve_canonical_name(via_org)
    cur = dal.get_conn().cursor()
    cur.execute("SELECT status FROM company WHERE canonical_name=%s", (canonical,))
    assert cur.fetchone()[0] == "candidate"
    cur.close()


def test_known_registry_name_does_not_skip_the_gate(dal, monkeypatch):
    """A brand-new org whose name happens to appear in
    company_registry._ALL_KNOWN_NAMES must NOT be fast-tracked to 'active'.
    Regression for the old save_board_vacancies branch that special-cased a
    "known" name straight to status='active' on ANY backend, bypassing the
    candidate gate. The fixed code doesn't consult the registry for this
    decision at all; patching the registry set here proves that.
    """
    import company_registry

    monkeypatch.setattr(company_registry, "_ALL_KNOWN_NAMES", {"RecognisedCo"})
    new = dal.save_board_vacancies(_board("RecognisedCo"), [_job("Recognised Role")])
    dal.get_conn().commit()
    assert new == 1
    cur = dal.get_conn().cursor()
    cur.execute("SELECT status FROM company WHERE canonical_name=%s", ("RecognisedCo",))
    assert cur.fetchone()[0] == "candidate"
    cur.close()


# ===========================================================================
# 4. _is_recently_archived runs with include_gone=True (board lag protection)
# ===========================================================================


def test_gone_from_source_hash_skips_board_merge(dal):
    """Board merge calls _is_recently_archived WITHOUT include_gone=False, so a
    recent 'gone_from_source' archived_hash DOES suppress the row.

    Contrast: the ATS path (save_vacancies) passes include_gone=False and would
    let such a posting through. This test pins the board-path asymmetry.
    """
    _seed_company(dal, "GoneCo")
    dedup_hash = dal.make_vacancy_id("GoneCo", "Gone Role")
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason) VALUES (%s, %s)",
        (dedup_hash, "gone_from_source"),
    )
    dal.get_conn().commit()
    cur.close()

    # Sanity-check the two readings of the same hash via the archived-hash sets.
    board_set = dal.get_archived_hashes(include_gone=True)  # board lag protection
    ats_set = dal.get_archived_hashes(include_gone=False)  # direct-ATS resurrection
    assert dedup_hash in board_set  # board path suppresses
    assert dedup_hash not in ats_set  # ATS path lets it through

    new = dal.save_board_vacancies(_board("GoneCo"), [_job("Gone Role", org="GoneCo")])
    dal.get_conn().commit()

    assert new == 0
    assert _row_count(dal, dedup_hash) == 0


# ===========================================================================
# 5. Resurrection: re-merging an archived row flips it back to 'unseen'
# ===========================================================================


def test_resurrect_archived_vacancy(dal):
    """An existing vacancy with status='archived' becomes 'unseen' when the same
    org+title is merged again (resurrected counter).
    """
    _seed_company(dal, "ResCo")
    dal.save_board_vacancies(_board("ResCo"), [_job("Res Role", org="ResCo")])
    dal.get_conn().commit()
    dedup_hash = dal.make_vacancy_id("ResCo", "Res Role")

    cur = dal.get_conn().cursor()
    cur.execute("UPDATE vacancy SET status='archived' WHERE dedup_hash=%s", (dedup_hash,))
    dal.get_conn().commit()
    cur.close()

    dal.save_board_vacancies(_board("ResCo"), [_job("Res Role", org="ResCo")])
    dal.get_conn().commit()

    assert _vacancy_row(dal, dedup_hash)["status"] == "unseen"


# ===========================================================================
# 6. Location merge — adds new loc_keys, but never refreshes an existing url
# ===========================================================================


def test_location_merge_adds_new_key_but_no_url_refresh(dal):
    """Re-merging the same org+title:
    - a NEW location key is appended to locations[],
    - a repeat of an EXISTING loc_key with a changed url is a no-op for that
      entry's url (board merge has no url-refresh branch, unlike save_vacancies).
    """
    _seed_company(dal, "LocCo")
    dedup_hash = dal.make_vacancy_id("LocCo", "Loc Role")

    dal.save_board_vacancies(
        _board("LocCo"),
        [_job("Loc Role", org="LocCo", city="Berlin, Germany", url="https://orig.test")],
    )
    dal.get_conn().commit()
    assert len(_locations(_vacancy_row(dal, dedup_hash))) == 1

    # New city → second location appended.
    dal.save_board_vacancies(
        _board("LocCo"),
        [_job("Loc Role", org="LocCo", city="London, United Kingdom", url="https://second.test")],
    )
    dal.get_conn().commit()
    assert len(_locations(_vacancy_row(dal, dedup_hash))) == 2

    # Same loc_key (Berlin) with a CHANGED url → no new entry, url NOT updated.
    dal.save_board_vacancies(
        _board("LocCo"),
        [_job("Loc Role", org="LocCo", city="Berlin, Germany", url="https://CHANGED.test")],
    )
    dal.get_conn().commit()
    locs = _locations(_vacancy_row(dal, dedup_hash))
    assert len(locs) == 2  # still two — same key not re-added
    berlin = [l for l in locs if (l.get("city") or "").lower().startswith("berlin")]
    assert berlin and berlin[0]["url"] == "https://orig.test"  # original url kept


# ===========================================================================
# 7. Boilerplate gate blanks the description but still inserts the row
# ===========================================================================


def test_cookie_wall_blanks_description_but_inserts_row(dal):
    """A full_description that is a pure cookie wall is reset to '' by
    _gate_description (skipped_boilerplate++), yet the vacancy STILL inserts
    because the snippet clears _has_enough_content. The gate cleans, it does not
    drop the row.
    """
    from quality import clean_description

    cookie = (
        "We use cookies to enhance your experience. By clicking Accept all "
        "cookies you agree to the storing of cookies. " * 4
    ) + " Apply now."
    # Confirm the empirical verdict the test depends on.
    assert clean_description(cookie)[1] == "cookie_wall"

    _seed_company(dal, "CookieCo")
    new = dal.save_board_vacancies(
        _board("CookieCo"),
        [
            _job(
                "Cookie Role",
                org="CookieCo",
                snippet="A genuine snippet long enough to clear the content gate.",
                desc=cookie,
            )
        ],
    )
    dal.get_conn().commit()

    assert new == 1
    row = _vacancy_row(dal, dal.make_vacancy_id("CookieCo", "Cookie Role"))
    assert row is not None
    assert (row["full_description"] or "") == ""  # gate blanked it


# ===========================================================================
# 8. Junk content (recaptcha) → skipped before insert
# ===========================================================================


def test_recaptcha_junk_skipped(dal):
    """A short (<300 chars) recaptcha-only description is flagged
    'recaptcha_only' by filters.is_content_junk and the row is skipped.

    The description is 50–299 chars so filters.has_enough_content (which runs first)
    passes on the description alone; with no snippet/url the junk gate is what
    drops it.
    """
    import filters

    desc = "Please complete the recaptcha to continue with your application now ok"
    assert 50 <= len(desc) < 300
    assert filters.is_content_junk(desc) == "recaptcha_only"

    _seed_company(dal, "JunkCo")
    job = _job("Junk Role", org="JunkCo", snippet="", url=None, desc=desc)
    assert filters.has_enough_content(job) is True  # desc alone clears the length gate

    new = dal.save_board_vacancies(_board("JunkCo"), [job])
    dal.get_conn().commit()

    assert new == 0
    assert _row_count(dal, dal.make_vacancy_id("JunkCo", "Junk Role")) == 0


# ===========================================================================
# 9. Blacklisted title → skipped (continue, no counter)
# ===========================================================================


def test_blacklisted_title_skipped(dal):
    """A title containing a GLOBAL_BLACKLIST word is dropped (continue) before
    company resolution; nothing is inserted.
    """
    import filters
    from config import GLOBAL_BLACKLIST

    assert GLOBAL_BLACKLIST, "blacklist must be non-empty for this test"
    word = GLOBAL_BLACKLIST[0]  # e.g. 'expression of interest'
    title = f"Senior {word.title()} Specialist"
    assert filters.title_words_blacklisted(title) is True

    _seed_company(dal, "BlackCo")
    new = dal.save_board_vacancies(_board("BlackCo"), [_job(title, org="BlackCo")])
    dal.get_conn().commit()

    assert new == 0
    assert _row_count(dal, dal.make_vacancy_id("BlackCo", title)) == 0
