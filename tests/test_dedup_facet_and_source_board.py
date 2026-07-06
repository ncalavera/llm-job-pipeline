"""BUG-6 (per-facet vacancy dedup + scored-row protection) and DHA-423
(source_board provenance on board imports).

BUG-6: a role posted once per country in a SINGLE fetch (FundraiseUp lists one
remote role as up to 8 Greenhouse listings, one per country, each with a
slightly different body) used to fork into a parallel row per facet because the
description fingerprints differed. The save layer now folds same-company +
same-title + already-claimed-in-this-batch rows onto one vacancy, and never
spawns a re-scoreable copy of a role that is already scored/decided. The title
normaliser also expands abbreviations (CEO -> Chief Executive Officer), strips
count parentheticals ("(3 Openings)") and req-id noise ("#12345").

DHA-423: board-sourced saves stamp vacancy.source_board with the board name;
direct-ATS saves leave it empty.

Harness mirrors tests/test_save_board_vacancies_characterization.py: conftest
clears SUPABASE_DB_URL, each test points JOBSEARCH_DB_PATH at its own temp
SQLite file and reloads the DAL chain, so everything runs on local SQLite. All
orgs/roles are invented.
"""

import importlib
import json
import sys

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _force_sqlite(monkeypatch, db_file):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"
    import database_supabase as db

    return db


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db = _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    yield db
    db.close_conn()


def _board(name):
    return {"name": name, "url": "https://board.test/feed", "tier": "B"}


def _long_body(seed):
    # >1000 chars, role-specific → a trustworthy, distinct description fingerprint.
    return (
        f"We are hiring for {seed}. You will own the {seed} charter end to "
        f"end, ship reliably, and collaborate across the org on {seed}. "
    ) * 20


def _job(title, *, org=None, city="Berlin, Germany", url=None, desc=None, snippet=None):
    job = {
        "title": title,
        "location": city,
        "snippet": snippet or f"{title} — a genuine open role with real duties here.",
        "full_description": desc or _long_body(title),
    }
    if org is not None:
        job["org_override"] = org
    if url is not None:
        job["url"] = url
    return job


def _seed_company(dal, name, status="active"):
    dal.ensure_company(name, status=status)
    dal.get_conn().commit()


def _rows(dal):
    return dal.load_vacancies(include_inactive_companies=True)


def _locations(row):
    locs = row["locations"]
    return json.loads(locs) if isinstance(locs, str) else (locs or [])


def _row_by_hash(dal, dedup_hash):
    cur = dal.get_conn().cursor(cursor_factory=dal.RealDictCursor)
    cur.execute("SELECT * FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
    r = cur.fetchone()
    cur.close()
    return r


def _add_source_board_column(dal):
    """Simulate prod / a migrated DB where vacancy.source_board exists."""
    cur = dal.get_conn().cursor()
    cur.execute("ALTER TABLE vacancy ADD COLUMN source_board TEXT")
    dal.get_conn().commit()
    cur.close()


# ===========================================================================
# 1. Title normaliser: the two real misses from the 2026-07-06 run
# ===========================================================================


def test_normalizer_expands_ceo_abbreviation(dal):
    """ "…Office of the CEO" and "…Office of the Chief Executive Officer" are one
    role (the passed@24 vs today miss from BUG-6)."""
    assert dal.make_normalized_id(
        "Org", "Senior Operations Associate, Office of the CEO"
    ) == dal.make_normalized_id(
        "Org", "Senior Operations Associate, Office of the Chief Executive Officer"
    )


def test_normalizer_strips_count_parenthetical(dal):
    """ "Principal, Project Development (3 Openings)" == "Principal, Project
    Development" (the applied@66 vs today miss). A distinguishing parenthetical
    like "(Spanish)" is NOT stripped."""
    assert dal.make_normalized_id(
        "Org", "Principal, Project Development (3 Openings)"
    ) == dal.make_normalized_id("Org", "Principal, Project Development")
    assert dal.make_normalized_id("Org", "Teacher (Spanish)") != dal.make_normalized_id(
        "Org", "Teacher"
    )


def test_normalizer_strips_req_id_noise(dal):
    assert dal.make_normalized_id("Org", "Data Analyst #12345") == dal.make_normalized_id(
        "Org", "Data Analyst"
    )


# ===========================================================================
# 2. Per-facet collapse: one remote role listed per country in ONE fetch
# ===========================================================================


def test_multi_country_facets_collapse_to_one_row(dal):
    """Same company + same title + remote, three country facets with distinct
    per-country bodies and URLs in ONE board fetch → ONE vacancy whose
    locations[] carries all three facets (BUG-6 FundraiseUp case)."""
    _seed_company(dal, "FacetCo")
    jobs = [
        _job(
            "Product Manager, Billing",
            org="FacetCo",
            city="Remote - Berlin, Germany",
            url="https://gh.test/1",
            desc=_long_body("billing PM cyprus"),
        ),
        _job(
            "Product Manager, Billing",
            org="FacetCo",
            city="Remote - London, United Kingdom",
            url="https://gh.test/2",
            desc=_long_body("billing PM armenia"),
        ),
        _job(
            "Product Manager, Billing",
            org="FacetCo",
            city="Remote - Paris, France",
            url="https://gh.test/3",
            desc=_long_body("billing PM georgia"),
        ),
    ]
    new = dal.save_board_vacancies(_board("FacetCo"), jobs)
    dal.get_conn().commit()

    assert new == 1, "three country facets of one role must be ONE new row"
    rows = _rows(dal)
    assert len(rows) == 1
    row = _row_by_hash(dal, dal.make_vacancy_id("FacetCo", "Product Manager, Billing"))
    assert len({(l.get("city") or "") for l in _locations(row)}) == 3  # all folded


def test_multi_country_facets_are_idempotent_across_reruns(dal):
    """Re-fetching the same multi-country batch does not spawn extra rows."""
    _seed_company(dal, "FacetCo")

    def batch():
        return [
            _job(
                "Group PM",
                org="FacetCo",
                city="Remote - Berlin, Germany",
                url="https://gh.test/a",
                desc=_long_body("group pm cyprus"),
            ),
            _job(
                "Group PM",
                org="FacetCo",
                city="Remote - London, United Kingdom",
                url="https://gh.test/b",
                desc=_long_body("group pm armenia"),
            ),
        ]

    dal.save_board_vacancies(_board("FacetCo"), batch())
    dal.get_conn().commit()
    dal.save_board_vacancies(_board("FacetCo"), batch())
    dal.get_conn().commit()

    assert len(_rows(dal)) == 1


def test_distinct_titles_same_company_stay_two_rows(dal):
    """No false positive: two genuinely different roles at one company keep two
    rows — the facet collapse only folds same-title facets."""
    _seed_company(dal, "DistinctCo")
    jobs = [
        _job("Data Engineer", org="DistinctCo", desc=_long_body("data engineer")),
        _job("Communications Officer", org="DistinctCo", desc=_long_body("comms officer")),
    ]
    new = dal.save_board_vacancies(_board("DistinctCo"), jobs)
    dal.get_conn().commit()

    assert new == 2
    assert sorted(v["title"] for v in _rows(dal).values()) == [
        "Communications Officer",
        "Data Engineer",
    ]


# ===========================================================================
# 3. Scored-row protection: never insert a re-scoreable copy of a settled role
# ===========================================================================


def test_scored_decided_row_is_not_reinserted_for_normalized_variant(dal):
    """A role already PASSED (and scored) as "…Office of the CEO" must absorb a
    later "…Office of the Chief Executive Officer" (different apply URL) rather
    than spawn a fresh, unscored, re-scoreable row (BUG-6)."""
    _seed_company(dal, "SettledCo")
    dal.save_vacancies(
        "SettledCo",
        "B",
        [
            _job(
                "Senior Operations Associate, Office of the CEO",
                org="SettledCo",
                url="https://ats.test/reqA",
            )
        ],
    )
    dal.get_conn().commit()
    h = dal.make_vacancy_id("SettledCo", "Senior Operations Associate, Office of the CEO")
    cur = dal.get_conn().cursor()
    cur.execute("UPDATE vacancy SET status='passed', llm_score=24 WHERE dedup_hash=%s", (h,))
    dal.get_conn().commit()
    cur.close()

    new = dal.save_vacancies(
        "SettledCo",
        "B",
        [
            _job(
                "Senior Operations Associate, Office of the Chief Executive Officer",
                org="SettledCo",
                url="https://ats.test/reqB",
            )
        ],
    )
    dal.get_conn().commit()

    rows = _rows(dal)
    assert new == 0, "the normalized variant must fold onto the settled row"
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["status"] == "passed"  # decision kept
    assert row["llm_score"] == 24  # score kept, not reset / re-scored


def test_scored_row_absorbs_same_title_new_facet_without_forking(dal):
    """An already-scored row that gains a NEW location facet (distinct body)
    from a later fetch folds the facet in — the settled row is never forked into
    a parallel re-scoreable sibling."""
    _seed_company(dal, "ScoreCo")
    dal.save_board_vacancies(
        _board("ScoreCo"),
        [
            _job(
                "Staff Engineer",
                org="ScoreCo",
                city="Remote - Berlin, Germany",
                url="https://b/1",
                desc=_long_body("staff eng one"),
            )
        ],
    )
    dal.get_conn().commit()
    h = dal.make_vacancy_id("ScoreCo", "Staff Engineer")
    cur = dal.get_conn().cursor()
    cur.execute("UPDATE vacancy SET status='liked', llm_score=71 WHERE dedup_hash=%s", (h,))
    dal.get_conn().commit()
    cur.close()

    new = dal.save_board_vacancies(
        _board("ScoreCo"),
        [
            _job(
                "Staff Engineer",
                org="ScoreCo",
                city="Remote - London, United Kingdom",
                url="https://b/2",
                desc=_long_body("staff eng two"),
            )
        ],
    )
    dal.get_conn().commit()

    assert new == 0
    row = _row_by_hash(dal, h)
    assert row["status"] == "liked" and row["llm_score"] == 71
    assert len(_locations(row)) == 2  # new facet folded in


# ===========================================================================
# 4. DHA-423 — source_board written for board imports, empty for direct ATS
# ===========================================================================


def test_board_import_stamps_source_board(dal):
    _add_source_board_column(dal)
    _seed_company(dal, "BoardCo")
    dal.save_board_vacancies(_board("EA Jobs Board"), [_job("Analyst", org="BoardCo")])
    dal.get_conn().commit()

    row = _row_by_hash(dal, dal.make_vacancy_id("BoardCo", "Analyst"))
    assert row["source_board"] == "EA Jobs Board"


def test_direct_ats_leaves_source_board_empty(dal):
    _add_source_board_column(dal)
    _seed_company(dal, "AtsCo")
    dal.save_vacancies("AtsCo", "B", [_job("Engineer", org="AtsCo")])
    dal.get_conn().commit()

    row = _row_by_hash(dal, dal.make_vacancy_id("AtsCo", "Engineer"))
    assert not (row["source_board"] or "")  # NULL / empty for ATS-sourced rows


def test_board_import_without_column_does_not_crash(dal):
    """A pre-migration install (no source_board column) still saves the row,
    just without provenance — the write is guarded."""
    _seed_company(dal, "NoColCo")
    new = dal.save_board_vacancies(_board("Some Board"), [_job("Officer", org="NoColCo")])
    dal.get_conn().commit()
    assert new == 1
    assert dal.make_vacancy_id("NoColCo", "Officer") in {
        v["dedup_hash"] for v in _rows(dal).values()
    }
