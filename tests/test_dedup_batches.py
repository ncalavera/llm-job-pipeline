"""Within-batch vacancy dedup: per-facet folding, and the inverse over-merge guard.

Covers what happens when several rows for the SAME role arrive together in one
save call — per-country/location facets fold to one row, scored/decided rows
never spawn a re-scoreable copy, board imports stamp provenance — and the
inverse guard that keeps two genuinely DIFFERENT sibling roles sharing a title
from collapsing into one. Absorbs:

  * tests/test_dedup_facet_and_source_board.py
  * tests/test_dedup_overmerge_siblings.py

Harness mirrors tests/test_save_board_vacancies_characterization.py /
tests/test_dedup_renamed_roles.py: conftest clears SUPABASE_DB_URL, each test
points JOBSEARCH_DB_PATH at its own temp SQLite file and reloads the DAL
chain. All orgs/roles are invented.
"""

import importlib
import json
import sys

import pytest


# ===========================================================================
# --- from test_dedup_facet_and_source_board.py ---
# ===========================================================================
#
# Per-facet vacancy dedup + scored-row protection, and source_board
# provenance on board imports.
#
# Facet dedup: a role posted once per country in a SINGLE fetch (an ATS lists one
# remote role as up to 8 Greenhouse listings, one per country, each with a
# slightly different body) used to fork into a parallel row per facet because the
# description fingerprints differed. The save layer now folds same-company +
# same-title + already-claimed-in-this-batch rows onto one vacancy, and never
# spawns a re-scoreable copy of a role that is already scored/decided. The title
# normaliser also expands abbreviations (CEO -> Chief Executive Officer), strips
# count parentheticals ("(3 Openings)") and req-id noise ("#12345").
#
# Board provenance: board-sourced saves stamp vacancy.source_board with the board name;
# direct-ATS saves leave it empty.


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


# ---------------------------------------------------------------------------
# 1. Title normaliser: the two real misses from the 2026-07-06 run
# ---------------------------------------------------------------------------


def test_normalizer_expands_ceo_abbreviation(dal):
    """ "…Office of the CEO" and "…Office of the Chief Executive Officer" are one
    role (the passed@24 vs today miss from the 2026-07-06 run)."""
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


def test_normalizer_keeps_new_city_parentheticals_distinct(dal):
    """Bare "new" is NOT a noise word: "(New York)" and "(New Delhi)" are
    distinguishing location parentheticals and must keep distinct norm keys
    (a count phrase like "(3 new openings)" still strips via the count /
    "openings" branch)."""
    assert dal.make_normalized_id("Org", "Analyst (New York)") != dal.make_normalized_id(
        "Org", "Analyst (New Delhi)"
    )
    assert dal.make_normalized_id("Org", "Analyst (New York)") != dal.make_normalized_id(
        "Org", "Analyst"
    )
    assert dal.make_normalized_id("Org", "Analyst (3 new openings)") == dal.make_normalized_id(
        "Org", "Analyst"
    )


# ---------------------------------------------------------------------------
# 2. Per-facet collapse: one remote role listed per country in ONE fetch
# ---------------------------------------------------------------------------


def test_multi_country_facets_collapse_to_one_row(dal):
    """Same company + same title + remote, three country facets with distinct
    per-country bodies and URLs in ONE board fetch → ONE vacancy whose
    locations[] carries all three facets (the per-country multi-post case)."""
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


def test_same_batch_same_exact_title_always_collapses_accepted_tradeoff(dal):
    """PINNED TRADEOFF: within ONE save call the batch-fold keys on the exact
    title hash make_vacancy_id(org, title) — NOT title+work_mode/location — so
    even two genuinely distinct same-exact-title roles (same city, different
    bodies and apply URLs) collapse to one row. Accepted deliberately: the save
    layer cannot tell an 8-country facet spray from two same-title same-city
    reqs, and the duplicate spray was the expensive failure. Cross-fetch
    distinct siblings still fork (see the "from test_dedup_overmerge_siblings.py"
    section below in this same file).
    """
    _seed_company(dal, "TradeoffCo")
    jobs = [
        _job(
            "Product Manager",
            org="TradeoffCo",
            city="Berlin, Germany",
            url="https://b.test/req1",
            desc=_long_body("payments charter"),
        ),
        _job(
            "Product Manager",
            org="TradeoffCo",
            city="Berlin, Germany",
            url="https://b.test/req2",
            desc=_long_body("growth charter"),
        ),
    ]
    new = dal.save_board_vacancies(_board("TradeoffCo"), jobs)
    dal.get_conn().commit()

    assert new == 1  # same-batch same-title collapse, by design
    assert len(_rows(dal)) == 1


def test_same_location_fold_keeps_an_apply_url(dal):
    """A same-title same-city facet folded onto the kept row must not lose its
    apply URL: the board merge refreshes the existing location entry's url in
    place (mirrors save_vacancies), so the row always keeps a working link."""
    _seed_company(dal, "UrlCo")
    jobs = [
        _job(
            "Ops Manager",
            org="UrlCo",
            city="Berlin, Germany",
            url="https://b.test/first",
            desc=_long_body("ops one"),
        ),
        _job(
            "Ops Manager",
            org="UrlCo",
            city="Berlin, Germany",
            url="https://b.test/second",
            desc=_long_body("ops two"),
        ),
    ]
    dal.save_board_vacancies(_board("UrlCo"), jobs)
    dal.get_conn().commit()

    row = _row_by_hash(dal, dal.make_vacancy_id("UrlCo", "Ops Manager"))
    locs = _locations(row)
    assert len(locs) == 1  # same loc_key folded, not duplicated
    assert locs[0]["url"] == "https://b.test/second"  # folded facet's url kept


# ---------------------------------------------------------------------------
# 3. Scored-row protection: never insert a re-scoreable copy of a settled role
# ---------------------------------------------------------------------------


def test_scored_decided_row_is_not_reinserted_for_normalized_variant(dal):
    """A role already PASSED (and scored) as "…Office of the CEO" must absorb a
    later "…Office of the Chief Executive Officer" (different apply URL) rather
    than spawn a fresh, unscored, re-scoreable row."""
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


# ---------------------------------------------------------------------------
# 4. source_board written for board imports, empty for direct ATS
# ---------------------------------------------------------------------------


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


# ===========================================================================
# --- from test_dedup_overmerge_siblings.py ---
# ===========================================================================
#
# The dedup key must not OVER-merge two distinct sibling roles.
#
# make_vacancy_id() hashes only org+title and dedup_hash is UNIQUE, so two
# GENUINELY different open roles that share a title+org collide on the canonical
# hash. When one arrives via a direct ATS and the other via a job board in the
# same run, the exact-hash merge used to collapse them into one row — destroying a
# live vacancy (guardrail 1). The disambiguator is the DESCRIPTION body: two
# distinct roles carry different, role-specific job descriptions, so a differing
# description fingerprint forks them into two rows; a shared body (a true
# two-source duplicate) still merges to one.
#
# The apply URL is deliberately NOT the signal: the board save folds one role's
# several location-specific URLs onto one row (multi-location posting), so a
# differing URL is not a distinct-role signal — see
# tests/test_save_board_vacancies_characterization.py::
# test_location_merge_adds_new_key_and_refreshes_url.
#
# This is the INVERSE of the under-merge guard against a whitespace-mangled org
# forking a true duplicate (see the org-whitespace section of
# tests/test_dedup_identity.py); the two goals coexist here — a true duplicate
# (same role, same body, two sources) still merges to ONE row.
#
# This section's fixture differs from the facet/source-board fixture above (it
# also force-reloads dedup_sweep out of sys.modules, per
# tests/test_dedup_renamed_roles.py's harness), so it keeps its own name,
# ``dal_ovm``, rather than silently reusing ``dal``.


@pytest.fixture()
def dal_ovm(tmp_path, monkeypatch):
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


_OVM_ORG = "Acme Foundation"

# Two full, role-specific bodies, each well over _MIN_DESC_FP_CHARS (1000) so
# each yields a trustworthy, DISTINCT description fingerprint.
_BODY_BACKEND = (
    "We are hiring a backend engineer to own our payments platform end to end: "
    "design the ledger, harden the settlement pipeline, and scale the API. " * 20
)
_BODY_FRONTEND = (
    "We are hiring a frontend engineer to craft our design system and dashboards: "
    "own the component library, the charts, and the accessibility work. " * 20
)


def _job_ovm(title, *, desc, url, city="Berlin, Germany", org_override=None):
    job = {
        "title": title,
        "snippet": f"{title} -- a genuine open role with real responsibilities.",
        "full_description": desc,
        "location": city,
        "url": url,
    }
    if org_override is not None:
        job["org_override"] = org_override
    return job


def _board_ovm():
    return {"name": _OVM_ORG, "url": "https://board.example/feed", "tier": "B"}


def _commit_ovm(dal_ovm):
    dal_ovm.get_conn().commit()


def _rows_ovm(dal_ovm):
    return dal_ovm.load_vacancies(include_inactive_companies=True)


def _hashes(dal_ovm):
    cur = dal_ovm.get_conn().cursor()
    cur.execute("SELECT dedup_hash FROM vacancy")
    hs = sorted(r[0] for r in cur.fetchall())
    cur.close()
    return hs


# ---------------------------------------------------------------------------
# Over-merge: two distinct siblings (same title, different body) must NOT collapse
# ---------------------------------------------------------------------------


def test_distinct_siblings_same_title_via_ats_and_board_stay_two_rows(dal_ovm):
    """Two genuinely different roles with the SAME title but DIFFERENT job
    descriptions, one from the direct ATS and one from a job board in one run,
    must survive as two rows. Before the fix the board save merged onto the ATS
    row via the exact hash and one live vacancy was lost."""
    dal_ovm.ensure_company(_OVM_ORG, status="active")

    new_ats = dal_ovm.save_vacancies(
        _OVM_ORG,
        "B",
        [_job_ovm("Product Manager", desc=_BODY_BACKEND, url="https://acme.example/ats/1")],
    )
    _commit_ovm(dal_ovm)
    new_board = dal_ovm.save_board_vacancies(
        _board_ovm(),
        [
            _job_ovm(
                "Product Manager",
                desc=_BODY_FRONTEND,
                url="https://board.example/2",
                org_override=_OVM_ORG,
            )
        ],
    )
    _commit_ovm(dal_ovm)

    rows = _rows_ovm(dal_ovm)
    assert new_ats == 1
    assert new_board == 1, "the distinct-body sibling must be a NEW row, not a merge"
    assert len(rows) == 2, "distinct same-title siblings collapsed into one (data loss)"
    assert sorted(v["title"] for v in rows.values()) == ["Product Manager", "Product Manager"]

    # One row keeps the canonical hash; the other is body-salted so both coexist.
    canonical = dal_ovm.make_vacancy_id(_OVM_ORG, "Product Manager")
    salted = dal_ovm.make_sibling_vacancy_id(
        canonical, dal_ovm.description_fingerprint(_BODY_FRONTEND)
    )
    assert _hashes(dal_ovm) == sorted([canonical, salted])


def test_sibling_fork_is_idempotent_across_reruns(dal_ovm):
    """Re-collecting the same two siblings changes nothing: each re-matches its
    OWN row (the canonical one, and the body-salted one by its fingerprint)."""
    dal_ovm.ensure_company(_OVM_ORG, status="active")

    for _ in range(2):
        dal_ovm.save_vacancies(
            _OVM_ORG,
            "B",
            [_job_ovm("Product Manager", desc=_BODY_BACKEND, url="https://acme.example/ats/1")],
        )
        _commit_ovm(dal_ovm)
        dal_ovm.save_board_vacancies(
            _board_ovm(),
            [
                _job_ovm(
                    "Product Manager",
                    desc=_BODY_FRONTEND,
                    url="https://board.example/2",
                    org_override=_OVM_ORG,
                )
            ],
        )
        _commit_ovm(dal_ovm)

    assert len(_rows_ovm(dal_ovm)) == 2  # no runaway duplication


# ---------------------------------------------------------------------------
# True duplicate: same role, same body, two sources → still ONE row
# ---------------------------------------------------------------------------


def test_true_duplicate_same_body_two_sources_still_merges(dal_ovm):
    """The other direction (true-duplicate preservation): the SAME role listed
    on the ATS and mirrored on a board — same title, same description body — is
    one role and must stay one row, even though the two sources carry different
    apply URLs."""
    dal_ovm.ensure_company(_OVM_ORG, status="active")

    new_ats = dal_ovm.save_vacancies(
        _OVM_ORG,
        "B",
        [_job_ovm("Data Scientist", desc=_BODY_BACKEND, url="https://acme.example/ats/9")],
    )
    _commit_ovm(dal_ovm)
    new_board = dal_ovm.save_board_vacancies(
        _board_ovm(),
        [
            _job_ovm(
                "Data Scientist",
                desc=_BODY_BACKEND,
                url="https://board.example/mirror",
                org_override=_OVM_ORG,
            )
        ],
    )
    _commit_ovm(dal_ovm)

    rows = _rows_ovm(dal_ovm)
    assert new_ats == 1
    assert new_board == 0, "same-body duplicate across two sources must merge, not fork"
    assert len(rows) == 1


def test_short_body_sibling_still_merges(dal_ovm):
    """A body too short to fingerprint carries no trustworthy distinct-role
    signal, so the exact hash still merges (backward compatible; a same-title
    language pair or a stub posting must not fork)."""
    dal_ovm.ensure_company(_OVM_ORG, status="active")
    # A real but medium body: clears the content gate, yet stays below
    # _MIN_DESC_FP_CHARS (1000) so its fingerprint is None (no distinct-role
    # signal). Both sides share it, so there is nothing to fork on.
    stub = "We are hiring a policy analyst to support our research team with briefs. " * 6
    assert dal_ovm.description_fingerprint(stub) is None

    new_ats = dal_ovm.save_vacancies(
        _OVM_ORG, "B", [_job_ovm("Policy Analyst", desc=stub, url="https://acme.example/ats/x")]
    )
    _commit_ovm(dal_ovm)
    new_board = dal_ovm.save_board_vacancies(
        _board_ovm(),
        [
            _job_ovm(
                "Policy Analyst", desc=stub, url="https://board.example/y", org_override=_OVM_ORG
            )
        ],
    )
    _commit_ovm(dal_ovm)

    assert new_ats == 1
    assert new_board == 0
    assert len(_rows_ovm(dal_ovm)) == 1
