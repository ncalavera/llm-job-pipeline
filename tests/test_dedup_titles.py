"""Cross-variant dedup on title wording, plus the dedup_sweep end-to-end contract.

Covers title normalization (renamed roles, punctuation, geo/plural suffixes,
board-prefix retitles, scrape-depth stubs, word-order) and the dedup_sweep
utility's dry-run/apply/archive behavior over these title shapes. Absorbs:

  * tests/test_dedup_renamed_roles.py
  * tests/test_dedup_geo_titles_and_scrape_depth.py
  * tests/test_dedup_board_prefix_retitle.py

Same SQLite harness throughout. Orgs invented (except title shapes mirroring
production regressions, called out in each section).
"""

import hashlib
import importlib
import json
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


def _commit(db):
    db.get_conn().commit()


def _set_status(db, dedup_hash, status):
    cur = db.get_conn().cursor()
    cur.execute("UPDATE vacancy SET status = %s WHERE dedup_hash = %s", (status, dedup_hash))
    db.get_conn().commit()
    cur.close()


def _job(title, *, city="Berlin, Germany", desc=None, url=None):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": desc
        or (f"We are hiring a {title}. " * 12 + "Own the work end to end."),
        "location": city,
        # url=None -> a title-derived URL (distinct roles differ); url="" -> no
        # apply URL at all (isolates the description-fingerprint path).
        "url": url
        if url is not None
        else "https://example.test/job/" + title.lower().replace(" ", "-"),
    }


# ===========================================================================
# --- from test_dedup_renamed_roles.py ---
# ===========================================================================
#
# Cross-variant vacancy dedup: renamed roles, re-punctuation, language copies.
#
# The save path must treat these as ONE role at a company, not a new vacancy —
# but ONLY when the story is a rename OVER TIME (the old title vanished from the
# source, a new one appeared). Two variants that are both live in the same fetch
# are two open roles and stay two rows.
#
#   * a seniority word added/removed in the title, when the old title is gone
#     ("Product Manager, Geo Expansion" -> "Senior Product Manager, Geo
#     Expansion");
#   * punctuation / whitespace / case differences ("Innovation - Generative" vs
#     "Innovation, Generative");
#   * a same-company duplicate in another language whose description body matches.
#
# Guards that keep a genuinely open role from being merged away:
#   * batch-alive — a variant of a title that is itself present in the current
#     fetch is a same-time pair, not a rename;
#   * URL — two rows with different non-empty apply URLs are distinct reqs.
#
# A rename keeps the user's decision (applied / passed) on the surviving row and
# repoints its title, and a repeat collection never forks a copy. The exact
# dedup_hash formula stays backward-compatible, so this is an ADDITIVE layer.

# A single requisition keeps ONE apply URL across a rename; two variants sharing
# this URL are the same req, so a merge is allowed (the URL guard does not fire).
_REQ_URL = "https://example.test/req/42"


def _rows(db):
    return db.load_vacancies(include_inactive_companies=True)


def _titles(db):
    return sorted(v["title"] for v in _rows(db).values())


def _raw_rows(db):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT dedup_hash, title, status FROM vacancy")
    rows = cur.fetchall()
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Pure normalization contract
# ---------------------------------------------------------------------------


def test_dedup_hash_formula_is_unchanged(dal):
    """make_vacancy_id() must still be md5(org|geo-normalized-title lower)[:16].

    Backward-compat guard: changing this silently would un-match every stored
    row and every tombstone (mass duplication / resurrection).
    """
    org, title = "Acme Foundation", "Ops Lead (Remote)"
    key = f"{org}|{dal._normalize_title_for_dedup(title)}".lower()
    assert dal.make_vacancy_id(org, title) == hashlib.md5(key.encode()).hexdigest()[:16]


def test_normalize_strips_level_words(dal):
    assert dal._normalize_title_strong("Senior Product Manager, Geo Expansion") == (
        dal._normalize_title_strong("Product Manager, Geo Expansion")
    )
    for lvl in ("Senior", "Sr.", "Lead", "Principal", "Junior"):
        assert dal.make_normalized_id("Acme", f"{lvl} Data Engineer") == dal.make_normalized_id(
            "Acme", "Data Engineer"
        )


def test_role_naming_words_are_not_stripped(dal):
    """Words that NAME a role -- 'staff', 'head of', 'chief' -- must keep a
    distinguishing key instead of collapsing into the bare noun (regression:
    'Staff Engineer'->'Engineer', 'Head of Product'->'Product', 'Chief of
    Staff'->'of')."""
    assert dal.make_normalized_id("Acme", "Staff Engineer") != dal.make_normalized_id(
        "Acme", "Engineer"
    )
    assert dal.make_normalized_id("Acme", "Head of Product") != dal.make_normalized_id(
        "Acme", "Product"
    )
    assert dal.make_normalized_id("Acme", "Chief of Staff") != dal.make_normalized_id(
        "Acme", "Staff"
    )
    # "Chief of Staff" must not degenerate into a punctuation-only stub.
    assert dal._normalize_title_strong("Chief of Staff") not in ("", "of")


def test_normalize_ignores_punctuation_and_spacing(dal):
    assert dal.make_normalized_id("Acme", "Innovation - Generative") == dal.make_normalized_id(
        "Acme", "Innovation,  Generative"
    )


def test_normalized_key_is_company_scoped(dal):
    assert dal.make_normalized_id("Acme", "Ops Lead") != dal.make_normalized_id(
        "Globex", "Ops Lead"
    )


def test_description_fingerprint_short_body_is_none(dal):
    assert dal.description_fingerprint("too short to trust") is None
    long_body = "We build safe and useful systems for everyone, everywhere. " * 20
    assert len(long_body) > 1000
    fp = dal.description_fingerprint(long_body)
    assert fp is not None and fp == dal.description_fingerprint(long_body)


# ---------------------------------------------------------------------------
# 1. Renamed role over time (old title gone) -> one vacancy
# ---------------------------------------------------------------------------


def test_level_rename_is_one_vacancy(dal):
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies(
        "Acme Foundation", "A", [_job("Product Manager, Geo Expansion", url=_REQ_URL)]
    )
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Foundation", "A", [_job("Senior Product Manager, Geo Expansion", url=_REQ_URL)]
    )
    _commit(dal)

    rows = _rows(dal)
    assert new == 0  # merged onto the existing role, not a new row
    assert len(rows) == 1
    # The survivor is repointed at the new title (fix 4).
    assert next(iter(rows.values()))["title"] == "Senior Product Manager, Geo Expansion"


def test_rescrape_rename_repoints_title_and_hash(dal):
    """The surviving row's title AND dedup_hash both move to the new name, so a
    later exact fetch of the new title matches directly."""
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [_job("Data Analyst", url=_REQ_URL)])
    _commit(dal)
    new = dal.save_vacancies("Acme Foundation", "A", [_job("Senior Data Analyst", url=_REQ_URL)])
    _commit(dal)

    raw = _raw_rows(dal)
    assert new == 0
    assert len(raw) == 1
    assert raw[0]["title"] == "Senior Data Analyst"
    assert raw[0]["dedup_hash"] == dal.make_vacancy_id("Acme Foundation", "Senior Data Analyst")


# ---------------------------------------------------------------------------
# 2. Punctuation / whitespace / case -> one vacancy
# ---------------------------------------------------------------------------


def test_punctuation_variant_is_one_vacancy(dal):
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [_job("Innovation - Generative", url=_REQ_URL)])
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Foundation", "A", [_job("Innovation,  Generative", url=_REQ_URL)]
    )
    _commit(dal)

    assert new == 0
    assert len(_rows(dal)) == 1


# ---------------------------------------------------------------------------
# 3. Language duplicate by description body -> one vacancy
# ---------------------------------------------------------------------------


def test_language_duplicate_by_description_is_one_vacancy(dal):
    dal.ensure_company("Acme Foundation", status="active")
    # A realistic full job description: long AND role-specific, so the fingerprint
    # is a trustworthy same-role signal (short shared blurbs are ignored, see
    # _MIN_DESC_FP_CHARS).
    shared = (
        "We are building tools to advance safe and trustworthy AI for the public "
        "interest. You will lead applied research on evaluation and alignment, "
        "publish findings openly, and mentor a small team of engineers across "
        "several time zones. Responsibilities include designing experiments, "
        "shipping reproducible pipelines, and collaborating with policy staff. " * 4
    )
    assert len(shared) > 1000
    # No apply URL on either -> the URL guard is silent and the description
    # fingerprint alone decides (a language pair often has no shared req URL).
    dal.save_vacancies("Acme Foundation", "A", [_job("Research Engineer", desc=shared, url="")])
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Foundation", "A", [_job("Ingenieur de recherche", desc=shared, url="")]
    )
    _commit(dal)

    assert new == 0
    assert len(_rows(dal)) == 1


def test_distinct_roles_are_not_merged(dal):
    """Two genuinely different roles (different title, different body) stay two."""
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies(
        "Acme Foundation",
        "A",
        [_job("Data Engineer"), _job("Communications Officer")],
    )
    _commit(dal)
    assert _titles(dal) == ["Communications Officer", "Data Engineer"]


# ---------------------------------------------------------------------------
# 4. Status inheritance (applied / passed survive a rename)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decided", ["applied"])
def test_rename_inherits_decided_status(dal, decided):
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [_job("Growth Marketing Manager", url=_REQ_URL)])
    _commit(dal)
    h = dal.make_vacancy_id("Acme Foundation", "Growth Marketing Manager")
    _set_status(dal, h, decided)

    new = dal.save_vacancies(
        "Acme Foundation", "A", [_job("Senior Growth Marketing Manager", url=_REQ_URL)]
    )
    _commit(dal)

    rows = _rows(dal)
    assert new == 0
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["status"] == decided  # NOT reset to unseen
    assert row["title"] == "Senior Growth Marketing Manager"  # repointed on rename


# ---------------------------------------------------------------------------
# 5. Same-time pairs stay two rows (the blocker fix)
# ---------------------------------------------------------------------------


def test_same_time_level_pair_stays_two_rows(dal):
    """Both levels present in ONE fetch are two open roles -- losing one would
    drop a genuinely live vacancy. They share an apply URL, so ONLY the
    batch-alive guard (not the URL guard) keeps them apart."""
    dal.ensure_company("Acme Foundation", status="active")
    new = dal.save_vacancies(
        "Acme Foundation",
        "A",
        [_job("Program Officer", url=_REQ_URL), _job("Senior Program Officer", url=_REQ_URL)],
    )
    _commit(dal)

    assert new == 2
    assert _titles(dal) == ["Program Officer", "Senior Program Officer"]


def test_variant_does_not_merge_onto_still_listed_existing(dal):
    """An existing role whose exact title is ALSO in the current fetch is live,
    so a level variant forks a new row instead of merging onto it."""
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [_job("Program Officer", url=_REQ_URL)])
    _commit(dal)

    new = dal.save_vacancies(
        "Acme Foundation",
        "A",
        [_job("Program Officer", url=_REQ_URL), _job("Senior Program Officer", url=_REQ_URL)],
    )
    _commit(dal)

    assert new == 1  # Program Officer merges onto existing; Senior Program Officer is new
    assert _titles(dal) == ["Program Officer", "Senior Program Officer"]


def test_different_apply_urls_block_rename_merge(dal):
    """Two variants with different non-empty apply URLs are distinct reqs: even
    when the old title is gone from the fetch, they do NOT merge."""
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies(
        "Acme Foundation", "A", [_job("Partnerships Manager", url="https://example.test/req/A")]
    )
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Foundation",
        "A",
        [_job("Senior Partnerships Manager", url="https://example.test/req/B")],
    )
    _commit(dal)

    assert new == 1
    assert _titles(dal) == ["Partnerships Manager", "Senior Partnerships Manager"]


# ---------------------------------------------------------------------------
# 6. Idempotent repeat collection
# ---------------------------------------------------------------------------


def test_repeat_collection_is_idempotent(dal):
    """Re-running the same fetch changes nothing. Two level variants that are
    BOTH present in the fetch are two live roles (kept), not a rename."""
    dal.ensure_company("Acme Foundation", status="active")
    batch = [
        _job("Program Officer", url=_REQ_URL),
        _job("Senior Program Officer", url=_REQ_URL),  # both live -> two rows, not merged
        _job("Software Engineer"),
    ]
    first = dal.save_vacancies("Acme Foundation", "A", batch)
    _commit(dal)
    second = dal.save_vacancies("Acme Foundation", "A", batch)
    _commit(dal)

    assert first == 3
    assert second == 0
    assert len(_rows(dal)) == 3


# ---------------------------------------------------------------------------
# 7. archive_gone keeps a renamed-but-still-live role
# ---------------------------------------------------------------------------


def test_archive_gone_keeps_renamed_live_role(dal):
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [_job("Data Analyst", url=_REQ_URL)])
    _commit(dal)

    # The ATS now lists the SAME role under a renamed title (same req URL).
    listing = [_job("Senior Data Analyst", url=_REQ_URL)]
    dal.save_vacancies("Acme Foundation", "A", listing)  # merges onto the row
    _commit(dal)
    archived = dal.archive_gone_vacancies("Acme Foundation", listing)
    _commit(dal)

    rows = _rows(dal)
    assert int(archived) == 0
    assert len(rows) == 1
    assert next(iter(rows.values()))["status"] == "unseen"  # kept alive, not archived


# ---------------------------------------------------------------------------
# 8. A tombstone suppresses a renamed variant (no resurrection loop)
# ---------------------------------------------------------------------------


def test_tombstone_suppresses_renamed_variant(dal):
    dal.ensure_company("Acme Foundation", status="active")
    dedup = dal.make_vacancy_id("Acme Foundation", "Growth Product Manager")
    norm = dal.make_normalized_id("Acme Foundation", "Growth Product Manager")
    dal.record_archived_hashes([(dedup, "score_below_threshold", norm)])
    _commit(dal)

    # A renamed variant of the buried role must not be re-imported.
    new = dal.save_vacancies("Acme Foundation", "A", [_job("Senior Growth Product Manager")])
    _commit(dal)

    assert new == 0
    assert len(_rows(dal)) == 0


# ---------------------------------------------------------------------------
# 9. dedup_sweep utility: dry-run reports, --apply collapses + archives losers
# ---------------------------------------------------------------------------

_RECENT = "2026-07-02"  # seen in the latest fetch -> live
_OLD = "2026-05-01"  # not seen since -> the renamed-away prior title


def _seed_row(
    dal,
    company_id,
    title,
    *,
    status="unseen",
    score=None,
    desc=None,
    first_seen="2026-01-01",
    last_seen=_RECENT,
):
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO vacancy (dedup_hash, company_id, title, full_description, status, "
        "llm_score, first_seen, last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            dal.make_vacancy_id("Acme Foundation", title),
            company_id,
            title,
            desc or "",
            status,
            score,
            first_seen,
            last_seen,
        ),
    )
    dal.get_conn().commit()
    cur.close()


def _seed_preexisting_dupes(dal):
    """Four collapsing dup classes (each a live survivor + a stale renamed-away
    loser) + one control pair that is both-live (must NOT collapse) + a
    standalone role."""
    cid = dal.ensure_company("Acme Foundation", status="active")
    dal.get_conn().commit()
    # (1) level rename -- the decided (applied) row must survive.
    _seed_row(
        dal, cid, "Senior Growth Product Manager", status="applied", score=55, last_seen=_RECENT
    )
    _seed_row(dal, cid, "Growth Product Manager", status="unseen", score=40, last_seen=_OLD)
    # (2) punctuation.
    _seed_row(dal, cid, "Innovation - Generative", status="unseen", last_seen=_RECENT)
    _seed_row(dal, cid, "Innovation, Generative", status="unseen", last_seen=_OLD)
    # (3) spacing around punctuation (different exact hash, same normalized key).
    _seed_row(dal, cid, "Data - Platform Engineer", status="unseen", last_seen=_RECENT)
    _seed_row(dal, cid, "Data-Platform Engineer", status="unseen", last_seen=_OLD)
    # (4) language pair sharing a long, role-specific description body.
    body = "We advance safe AI in the public interest and publish our research openly. " * 20
    _seed_row(dal, cid, "Research Scientist", status="liked", desc=body, last_seen=_RECENT)
    _seed_row(dal, cid, "Chercheur scientifique", status="unseen", desc=body, last_seen=_OLD)
    # Control: two levels BOTH live in the latest fetch -> manual review, not merged.
    _seed_row(dal, cid, "Program Officer", status="unseen", last_seen=_RECENT)
    _seed_row(dal, cid, "Senior Program Officer", status="unseen", last_seen=_RECENT)
    # A standalone role that must be left untouched.
    _seed_row(dal, cid, "Office Coordinator", status="unseen", last_seen=_RECENT)
    return cid


def test_dedup_sweep_dry_run_reports_but_does_not_write(dal, capsys, monkeypatch):
    _seed_preexisting_dupes(dal)
    before = len(_rows(dal))
    assert before == 11

    import dedup_sweep

    importlib.reload(dedup_sweep)
    monkeypatch.setattr(sys, "argv", ["dedup_sweep.py"])
    rc = dedup_sweep.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "DRY-RUN" in out
    assert "4 duplicate cluster(s)" in out
    assert "1 cluster(s) flagged for manual review" in out
    assert "MANUAL REVIEW" in out
    assert "inherits 'applied'" in out
    assert "ARCHIVE" in out  # names the losers that would be archived
    assert "Program Officer" in out  # the both-live control pair is surfaced
    # Nothing was written.
    assert len(_rows(dal)) == before


def test_dedup_sweep_apply_collapses_and_leaves_live_pair(dal, tmp_path, monkeypatch):
    _seed_preexisting_dupes(dal)

    import dedup_sweep

    importlib.reload(dedup_sweep)
    monkeypatch.setattr(dedup_sweep, "VACANCIES_DIR", tmp_path / "vacancies")
    monkeypatch.setattr(sys, "argv", ["dedup_sweep.py", "--apply"])
    rc = dedup_sweep.main()

    assert rc == 0
    rows = _rows(dal)
    # 11 rows -> 7: four clusters collapse (one loser each), the both-live pair
    # and the standalone are untouched.
    assert len(rows) == 7
    statuses = {v["title"]: v["status"] for v in rows.values()}
    assert "applied" in statuses.values()  # Growth Product Manager cluster survivor
    assert "liked" in statuses.values()  # Research Scientist cluster survivor
    # The both-live control pair is NOT collapsed.
    assert {"Program Officer", "Senior Program Officer"} <= set(statuses)
    assert "Office Coordinator" in statuses


def test_dedup_sweep_apply_archives_losers_to_json(dal, tmp_path, monkeypatch):
    _seed_preexisting_dupes(dal)

    import dedup_sweep

    importlib.reload(dedup_sweep)
    archive_root = tmp_path / "vacancies"
    monkeypatch.setattr(dedup_sweep, "VACANCIES_DIR", archive_root)
    monkeypatch.setattr(sys, "argv", ["dedup_sweep.py", "--apply"])
    rc = dedup_sweep.main()
    assert rc == 0

    files = list((archive_root / "archive").glob("dedup_sweep_*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["count"] == 4
    assert len(payload["vacancies"]) == 4
    archived_titles = {v["title"] for v in payload["vacancies"].values()}
    # Every archived row is a stale, renamed-away prior title, never a survivor.
    assert "Growth Product Manager" in archived_titles
    assert "Senior Growth Product Manager" not in archived_titles
    # No temp file left behind after the atomic publish.
    assert not list((archive_root / "archive").glob("*.tmp"))


# ===========================================================================
# --- from test_dedup_geo_titles_and_scrape_depth.py ---
# ===========================================================================
#
# Cross-variant dedup: geo title suffixes, plural titles, scrape-depth stubs.
#
# Three real-world duplication stories the save path must fold to ONE row
# (regression: a GWWC role lived as three rows — "Head of Community Engagement,
# London" via a board, "Heads of Community Engagement" and "Head of Community
# Engagement" via direct fetches; a second role forked a sibling because one
# board shipped a 1.1k-char summary stub of an 11.5k-char JD):
#
#   * a trailing comma/dash segment naming a KNOWN city/country/work mode is not
#     part of the role identity ("Head of X, London" == "Head of X");
#   * a plural retitle is not a new role ("Heads of X" == "Head of X");
#   * two sources scraping the SAME posting with wildly different body lengths
#     (full JD vs summary stub) must not fork a description-salted sibling —
#     the fingerprint comparison is only trusted for comparably-sized bodies.
#
# Guards that must survive:
#   * a distinguishing comma qualifier stays ("Program Officer, Climate");
#   * two distinct same-title roles with comparably-sized different bodies still
#     fork a sibling.
#
# All orgs invented, except title shapes mirroring the production regression.

_REQ_URL_GEO = "https://example.test/req/77"


def _raw_rows_geo(db):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT dedup_hash, title, status, full_description FROM vacancy")
    rows = cur.fetchall()
    cur.close()
    return rows


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
    dal.save_vacancies("Acme Fund", "A", [_job("Heads of Community Engagement", url=_REQ_URL_GEO)])
    _commit(dal)
    old_hash = dal.make_vacancy_id("Acme Fund", "Heads of Community Engagement")
    _set_status(dal, old_hash, "passed")

    new = dal.save_vacancies(
        "Acme Fund", "A", [_job("Head of Community Engagement, London", url=_REQ_URL_GEO)]
    )
    _commit(dal)

    raw = _raw_rows_geo(dal)
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
        "Acme Fund", "A", [_job("Grants Operations Associate", desc=_FULL_JD, url=_REQ_URL_GEO)]
    )
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Fund",
        "A",
        [_job("Grants Operations Associate", desc=_STUB_JD, url="https://board.test/j/1")],
    )
    _commit(dal)

    raw = _raw_rows_geo(dal)
    assert new == 0
    assert len(raw) == 1
    # The richer body is kept on the merged row (save path may trim whitespace).
    assert raw[0]["full_description"].strip() == _FULL_JD.strip()


def test_comparable_distinct_bodies_still_fork_sibling(dal):
    """Two genuinely different same-title reqs (both full JDs) stay two rows."""
    dal.ensure_company("Acme Fund", status="active")
    dal.save_vacancies("Acme Fund", "A", [_job("Program Officer", desc=_FULL_JD, url=_REQ_URL_GEO)])
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Fund",
        "A",
        [_job("Program Officer", desc=_OTHER_FULL_JD, url="https://example.test/req/78")],
    )
    _commit(dal)

    assert new == 1
    assert len(_raw_rows_geo(dal)) == 2


# ---------------------------------------------------------------------------
# Continent suffixes and same-URL retitles (board re-listing patterns)
# ---------------------------------------------------------------------------


def test_continent_suffix_folds_to_one_key(dal):
    k = lambda t: dal.make_normalized_id("Acme Fund", t)
    assert k("Director of MEAL, Africa") == k("Director of MEAL")
    assert k("Short Term Analyst, Academy, Latin America") == k("Short Term Analyst, Academy")


def test_same_url_retitle_merges_over_time(dal):
    """A re-listing under the SAME apply URL with a containment retitle is one
    role even when normalized titles no longer match ("- Deal Operations")."""
    url = "https://example.test/req/900"
    dal.ensure_company("Acme Fund", status="active")
    dal.save_vacancies("Acme Fund", "A", [_job("Program Manager, Business Practices", url=url)])
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Fund",
        "A",
        [_job("Program Manager, Business Practices - Deal Operations", url=url)],
    )
    _commit(dal)

    raw = _raw_rows_geo(dal)
    assert new == 0
    assert len(raw) == 1
    assert raw[0]["title"] == "Program Manager, Business Practices - Deal Operations"


# Note: a companion guard, "a generic shared URL without title containment
# never merges different roles", used to live here as
# test_generic_shared_url_without_containment_stays_two_rows. It was dropped
# as an exact duplicate of test_generic_careers_url_never_merges_different_roles
# in tests/test_dedup_identity.py (same guard, same code path, only the
# literal title strings differed).


# ===========================================================================
# --- from test_dedup_board_prefix_retitle.py ---
# ===========================================================================
#
# Cross-board dedup on board-prefixed retitles and reordered titles.
#
# Production regression (run 2026-08-04): the SAME program reached the DB twice
# because two boards list it under different titles AND different URLs —
#
#   * 80,000 Hours: "Charity Entrepreneurship Incubation Program", linking the
#     org's own page, with a short summary stub (912 chars);
#   * Idealist: "Non-profit Entrepreneur — Charity Entrepreneurship Incubation
#     Program", linking Idealist's OWN posting page, with the full JD (13k chars).
#
# No existing key matched: the title keys differ (board-added label segment),
# the description fingerprints differ (stub vs full JD), and the URL key differs
# (each board links its own page). A third, February copy of the same program
# ("Incubation Program, Charity Entrepreneurship") shared the normalized URL but
# missed the same-URL merge because token ORDER broke both containment and the
# stopword fold.
#
# Contract under test:
#
#   * _title_segment_keys() keys every dash/comma segment of a title that is
#     substantial on its own (>= 3 significant words), org-scoped;
#   * the save path folds a board copy whose full title equals a segment of a
#     stored title (and vice versa) — the body guard, not the apply URL, decides
#     distinctness; segment-vs-segment never matches;
#   * _titles_equal_sans_stopwords() is word-order insensitive, so the same-URL
#     merge folds reordered retitles of one req;
#   * dedup_sweep clusters a board-prefix pair and auto-collapses it even when
#     both rows are live, the survivor keeping the most-decided status.

_BARE = "Charity Kitchen Fellowship Program"
_PREFIXED = "Non-profit Entrepreneur — Charity Kitchen Fellowship Program"
_ORG_URL = "https://charitykitchen.test/fellowship-program"
_BOARD_URL = "https://board.test/nonprofit-job/abc123-charity-kitchen-fellowship"

_STUB = "This is a two-month training program to launch a high-impact project. " * 4
_BP_FULL_JD = (
    "TLDR: Serious about impact? We help you start a high-impact project from "
    "scratch: strategy, fundraising, operations and evaluation, with seed "
    "funding and mentorship from experienced advisors across the whole cycle. "
) * 8


def _job_bp(title, *, url, desc):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": desc,
        "location": "Remote",
        "url": url,
    }


def _board_job(title, *, url, desc, org):
    return {**_job_bp(title, url=url, desc=desc), "org_override": org}


def _raw_rows_bp(db):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT id, dedup_hash, title, status FROM vacancy")
    rows = cur.fetchall()
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Pure contracts: segment keys and order-insensitive stopword fold
# ---------------------------------------------------------------------------


def test_segment_keys_for_substantial_segments_only(dal):
    keys = dal._title_segment_keys("Acme Org", _PREFIXED)
    assert dal.make_normalized_id("Acme Org", _BARE) in keys

    # Short segments ("Climate", "Ops Lead") never produce keys.
    assert dal._title_segment_keys("Acme Org", "Program Officer, Climate") == []
    # An unsegmented title produces no keys (that is the ordinary norm key).
    assert dal._title_segment_keys("Acme Org", "Director of Finance") == []


def test_segment_keys_are_org_scoped(dal):
    a = dal._title_segment_keys("Acme Org", _PREFIXED)
    b = dal._title_segment_keys("Other Org", _PREFIXED)
    assert a and b and set(a).isdisjoint(b)


def test_titles_equal_sans_stopwords_ignores_word_order(dal):
    strong = dal._normalize_title_strong
    eq = dal._titles_equal_sans_stopwords
    assert eq(
        strong("Fellowship Program, Charity Kitchen"),
        strong("Charity Kitchen Fellowship Program"),
    )
    assert not eq(strong("Director of Finance"), strong("Director of Programs"))


# ---------------------------------------------------------------------------
# Save path: a board-prefixed copy folds onto the stored row (either order)
# ---------------------------------------------------------------------------


def test_prefixed_board_copy_folds_onto_bare_row(dal):
    dal.ensure_company("Charity Kitchen", status="active")
    dal.save_vacancies("Charity Kitchen", "A", [_job_bp(_BARE, url=_ORG_URL, desc=_STUB)])
    _commit(dal)

    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [_board_job(_PREFIXED, url=_BOARD_URL, desc=_BP_FULL_JD, org="Charity Kitchen")],
    )
    _commit(dal)

    rows = _raw_rows_bp(dal)
    assert len(rows) == 1, [r["title"] for r in rows]
    assert rows[0]["title"] == _BARE


def test_bare_copy_folds_onto_prefixed_row(dal):
    dal.ensure_company("Charity Kitchen", status="active")
    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [_board_job(_PREFIXED, url=_BOARD_URL, desc=_BP_FULL_JD, org="Charity Kitchen")],
    )
    _commit(dal)

    dal.save_vacancies("Charity Kitchen", "A", [_job_bp(_BARE, url=_ORG_URL, desc=_STUB)])
    _commit(dal)

    rows = _raw_rows_bp(dal)
    assert len(rows) == 1, [r["title"] for r in rows]
    assert rows[0]["title"] == _PREFIXED


def test_two_full_different_bodies_stay_two_roles(dal):
    """The body guard: a segment match backed by two full, comparably sized but
    DIFFERENT bodies is a second genuine role, not a board copy."""
    dal.ensure_company("Charity Kitchen", status="active")
    other_full = (
        "As the coordinator you will own logistics, partner comms and the "
        "weekly cohort rhythm; you will run selection interviews and demo days "
        "and keep the alumni community engaged after each cohort graduates. "
    ) * 8
    dal.save_vacancies("Charity Kitchen", "A", [_job_bp(_BARE, url=_ORG_URL, desc=_BP_FULL_JD)])
    _commit(dal)

    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [
            _board_job(
                "Coordinator — Charity Kitchen Fellowship Program",
                url=_BOARD_URL,
                desc=other_full,
                org="Charity Kitchen",
            )
        ],
    )
    _commit(dal)

    rows = _raw_rows_bp(dal)
    assert len(rows) == 2, [r["title"] for r in rows]


def test_segment_vs_segment_never_merges(dal):
    """Two roles decorated with the same program name are two roles."""
    dal.ensure_company("Charity Kitchen", status="active")
    dal.save_vacancies(
        "Charity Kitchen",
        "A",
        [_job_bp("Ops Lead — Charity Kitchen Fellowship Program", url=_ORG_URL, desc=_STUB)],
    )
    _commit(dal)

    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [
            _board_job(
                "Research Lead — Charity Kitchen Fellowship Program",
                url=_BOARD_URL,
                desc=_STUB + " Research flavor.",
                org="Charity Kitchen",
            )
        ],
    )
    _commit(dal)

    rows = _raw_rows_bp(dal)
    assert len(rows) == 2, [r["title"] for r in rows]


# ---------------------------------------------------------------------------
# Sweep: the production trio collapses onto the decided row
# ---------------------------------------------------------------------------


def _sweep(monkeypatch, apply=False):
    import dedup_sweep

    importlib.reload(dedup_sweep)
    argv = ["dedup_sweep.py"] + (["--apply"] if apply else [])
    monkeypatch.setattr(sys, "argv", argv)
    dedup_sweep.main()


def test_sweep_collapses_live_board_prefix_pair(dal, monkeypatch):
    """Reproduces run 2026-08-04: a bare stub copy (passed), a board-prefixed
    full copy (to_apply) — both live — plus an old archived reordered copy
    sharing the bare row's normalized URL. All three must collapse; the
    to_apply row survives."""
    dal.ensure_company("Charity Kitchen", status="active")
    # Insert as distinct reqs (distinct URLs + titles the save path won't fold
    # by itself once statuses are rewritten below simulate the legacy state).
    dal.save_vacancies(
        "Charity Kitchen",
        "A",
        [_job_bp("Fellowship Program, Charity Kitchen", url="https://old.test/req", desc=_STUB)],
    )
    _commit(dal)
    dal.save_vacancies(
        "Charity Kitchen",
        "A",
        [_job_bp(_BARE, url="https://mid.test/req", desc=_STUB + " Bare copy.")],
    )
    _commit(dal)
    # The save path now folds the prefixed board copy, so inject it BEHIND the
    # save path (simulates a row stored before this fix existed): save it under
    # an unrelated title, then repoint the title at the prefixed form.
    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [
            _board_job(
                "Placeholder Independent Role",
                url=_BOARD_URL,
                desc=_BP_FULL_JD,
                org="Charity Kitchen",
            )
        ],
    )
    _commit(dal)
    assert len(_raw_rows_bp(dal)) == 3

    cur = dal.get_conn().cursor()
    reordered_hash = dal.make_vacancy_id("Charity Kitchen", "Fellowship Program, Charity Kitchen")
    bare_hash = dal.make_vacancy_id("Charity Kitchen", _BARE)
    placeholder_hash = dal.make_vacancy_id("Charity Kitchen", "Placeholder Independent Role")
    prefixed_hash = dal.make_vacancy_id("Charity Kitchen", _PREFIXED)
    cur.execute(
        "UPDATE vacancy SET title = %s, dedup_hash = %s WHERE dedup_hash = %s",
        (_PREFIXED, prefixed_hash, placeholder_hash),
    )
    cur.execute(
        "UPDATE vacancy SET status = 'archived', last_seen = '2026-03-26', "
        "locations = %s WHERE dedup_hash = %s",
        (
            dal.Json([{"url": _ORG_URL + "?utm_source=80k&utm_medium=job-board"}]),
            reordered_hash,
        ),
    )
    cur.execute(
        "UPDATE vacancy SET status = 'passed', locations = %s WHERE dedup_hash = %s",
        (dal.Json([{"url": _ORG_URL + "?utm_source=80000hours"}]), bare_hash),
    )
    cur.execute("UPDATE vacancy SET status = 'to_apply' WHERE dedup_hash = %s", (prefixed_hash,))
    _commit(dal)

    _sweep(monkeypatch, apply=True)

    rows = _raw_rows_bp(dal)
    live = [r for r in rows if r["status"] != "archived"]
    assert len(live) == 1, [(r["title"], r["status"]) for r in rows]
    assert live[0]["status"] == "to_apply"
    assert live[0]["title"] == _PREFIXED
