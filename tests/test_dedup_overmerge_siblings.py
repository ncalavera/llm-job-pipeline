"""The dedup key must not OVER-merge two distinct sibling roles.

make_vacancy_id() hashes only org+title and dedup_hash is UNIQUE, so two
GENUINELY different open roles that share a title+org collide on the canonical
hash. When one arrives via a direct ATS and the other via a job board in the
same run, the exact-hash merge used to collapse them into one row — destroying a
live vacancy (guardrail 1). The disambiguator is the DESCRIPTION body: two
distinct roles carry different, role-specific job descriptions, so a differing
description fingerprint forks them into two rows; a shared body (a true
two-source duplicate) still merges to one.

The apply URL is deliberately NOT the signal: the board save folds one role's
several location-specific URLs onto one row (multi-location posting), so a
differing URL is not a distinct-role signal — see
tests/test_save_board_vacancies_characterization.py::
test_location_merge_adds_new_key_and_refreshes_url.

This is the INVERSE of the under-merge guard against a whitespace-mangled org
forking a true duplicate (see tests/test_dedup_org_whitespace.py); the two goals
coexist here — a true duplicate (same role, same body, two sources) still merges
to ONE row.

Harness mirrors tests/test_dedup_renamed_roles.py: SQLite backend, isolated temp
DB per test, reload the DAL chain. All orgs/roles are invented.
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


_ORG = "Acme Foundation"

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


def _job(title, *, desc, url, city="Berlin, Germany", org_override=None):
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


def _board():
    return {"name": _ORG, "url": "https://board.example/feed", "tier": "B"}


def _commit(dal):
    dal.get_conn().commit()


def _rows(dal):
    return dal.load_vacancies(include_inactive_companies=True)


def _hashes(dal):
    cur = dal.get_conn().cursor()
    cur.execute("SELECT dedup_hash FROM vacancy")
    hs = sorted(r[0] for r in cur.fetchall())
    cur.close()
    return hs


# ---------------------------------------------------------------------------
# Over-merge: two distinct siblings (same title, different body) must NOT collapse
# ---------------------------------------------------------------------------


def test_distinct_siblings_same_title_via_ats_and_board_stay_two_rows(dal):
    """Two genuinely different roles with the SAME title but DIFFERENT job
    descriptions, one from the direct ATS and one from a job board in one run,
    must survive as two rows. Before the fix the board save merged onto the ATS
    row via the exact hash and one live vacancy was lost."""
    dal.ensure_company(_ORG, status="active")

    new_ats = dal.save_vacancies(
        _ORG, "B", [_job("Product Manager", desc=_BODY_BACKEND, url="https://acme.example/ats/1")]
    )
    _commit(dal)
    new_board = dal.save_board_vacancies(
        _board(),
        [
            _job(
                "Product Manager",
                desc=_BODY_FRONTEND,
                url="https://board.example/2",
                org_override=_ORG,
            )
        ],
    )
    _commit(dal)

    rows = _rows(dal)
    assert new_ats == 1
    assert new_board == 1, "the distinct-body sibling must be a NEW row, not a merge"
    assert len(rows) == 2, "distinct same-title siblings collapsed into one (data loss)"
    assert sorted(v["title"] for v in rows.values()) == ["Product Manager", "Product Manager"]

    # One row keeps the canonical hash; the other is body-salted so both coexist.
    canonical = dal.make_vacancy_id(_ORG, "Product Manager")
    salted = dal.make_sibling_vacancy_id(canonical, dal.description_fingerprint(_BODY_FRONTEND))
    assert _hashes(dal) == sorted([canonical, salted])


def test_sibling_fork_is_idempotent_across_reruns(dal):
    """Re-collecting the same two siblings changes nothing: each re-matches its
    OWN row (the canonical one, and the body-salted one by its fingerprint)."""
    dal.ensure_company(_ORG, status="active")

    for _ in range(2):
        dal.save_vacancies(
            _ORG,
            "B",
            [_job("Product Manager", desc=_BODY_BACKEND, url="https://acme.example/ats/1")],
        )
        _commit(dal)
        dal.save_board_vacancies(
            _board(),
            [
                _job(
                    "Product Manager",
                    desc=_BODY_FRONTEND,
                    url="https://board.example/2",
                    org_override=_ORG,
                )
            ],
        )
        _commit(dal)

    assert len(_rows(dal)) == 2  # no runaway duplication


# ---------------------------------------------------------------------------
# True duplicate: same role, same body, two sources → still ONE row
# ---------------------------------------------------------------------------


def test_true_duplicate_same_body_two_sources_still_merges(dal):
    """The other direction (true-duplicate preservation): the SAME role listed
    on the ATS and mirrored on a board — same title, same description body — is
    one role and must stay one row, even though the two sources carry different
    apply URLs."""
    dal.ensure_company(_ORG, status="active")

    new_ats = dal.save_vacancies(
        _ORG, "B", [_job("Data Scientist", desc=_BODY_BACKEND, url="https://acme.example/ats/9")]
    )
    _commit(dal)
    new_board = dal.save_board_vacancies(
        _board(),
        [
            _job(
                "Data Scientist",
                desc=_BODY_BACKEND,
                url="https://board.example/mirror",
                org_override=_ORG,
            )
        ],
    )
    _commit(dal)

    rows = _rows(dal)
    assert new_ats == 1
    assert new_board == 0, "same-body duplicate across two sources must merge, not fork"
    assert len(rows) == 1


def test_short_body_sibling_still_merges(dal):
    """A body too short to fingerprint carries no trustworthy distinct-role
    signal, so the exact hash still merges (backward compatible; a same-title
    language pair or a stub posting must not fork)."""
    dal.ensure_company(_ORG, status="active")
    # A real but medium body: clears the content gate, yet stays below
    # _MIN_DESC_FP_CHARS (1000) so its fingerprint is None (no distinct-role
    # signal). Both sides share it, so there is nothing to fork on.
    stub = "We are hiring a policy analyst to support our research team with briefs. " * 6
    assert dal.description_fingerprint(stub) is None

    new_ats = dal.save_vacancies(
        _ORG, "B", [_job("Policy Analyst", desc=stub, url="https://acme.example/ats/x")]
    )
    _commit(dal)
    new_board = dal.save_board_vacancies(
        _board(),
        [_job("Policy Analyst", desc=stub, url="https://board.example/y", org_override=_ORG)],
    )
    _commit(dal)

    assert new_ats == 1
    assert new_board == 0
    assert len(_rows(dal)) == 1
