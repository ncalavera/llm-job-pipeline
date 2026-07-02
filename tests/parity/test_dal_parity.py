"""Backend-parity characterization: SQLite and Postgres must behave the same
across the DAL surfaces the daily pipeline actually depends on -- saving
vacancies, company-status transitions, job-board TTL math, and
archive/resurrect.

Each test is parametrized over the `backend` fixture (sqlite / postgres, see
conftest.py) and asserts the SAME expected outcome for both. A divergence
shows up as one parametrized instance failing while its sibling passes.

Fully invented organisations and roles -- no real company or geography.
"""

import pytest

pytestmark = pytest.mark.parity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job(title, *, city="Berlin, Germany", url=None):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role, not a template.",
        "full_description": (f"We are hiring a {title}. " * 12) + "Own the work end to end.",
        "location": city,
        "url": url or f"https://example.test/jobs/{title.lower().replace(' ', '-')}",
    }


def _commit(dal):
    dal.get_conn().commit()


def _by_title(dal, title):
    for vid, v in dal.load_vacancies(include_inactive_companies=True).items():
        if v["title"] == title:
            return vid
    raise AssertionError(f"vacancy {title!r} not found")


# ---------------------------------------------------------------------------
# 1. save_vacancies -- idempotency + dedup
# ---------------------------------------------------------------------------


def test_save_vacancies_dedups_on_repeat_fetch(backend):
    dal = backend
    dal.ensure_company("Northwind Aid Trust", status="active")
    first = dal.save_vacancies("Northwind Aid Trust", "B", [_job("Programme Officer")])
    _commit(dal)
    second = dal.save_vacancies("Northwind Aid Trust", "B", [_job("Programme Officer")])
    _commit(dal)

    assert first == 1
    assert second == 0  # same dedup_hash -> merge into the existing row, not a new one

    rows = dal.load_vacancies(include_inactive_companies=True)
    titles = [v["title"] for v in rows.values()]
    assert titles.count("Programme Officer") == 1


def test_save_vacancies_merges_second_location_into_same_role(backend):
    dal = backend
    dal.ensure_company("Northwind Aid Trust", status="active")
    dal.save_vacancies(
        "Northwind Aid Trust", "B", [_job("Programme Officer", city="Berlin, Germany")]
    )
    _commit(dal)
    dal.save_vacancies(
        "Northwind Aid Trust", "B", [_job("Programme Officer", city="Lisbon, Portugal")]
    )
    _commit(dal)

    vid = _by_title(dal, "Programme Officer")
    vac = dal.load_vacancies(include_inactive_companies=True)[vid]
    assert len(vac["locations"]) == 2


# ---------------------------------------------------------------------------
# 2. Company status
# ---------------------------------------------------------------------------


def test_ensure_company_status_honored_verbatim(backend):
    dal = backend
    dal.ensure_company("Fictive Robotics Guild", status="active")
    _commit(dal)
    fitness = dal.get_company_fitness_map()
    assert fitness["Fictive Robotics Guild"]["status"] == "active"


def test_ensure_company_default_status_is_candidate(backend):
    dal = backend
    dal.ensure_company("Contoso Relief Fund")  # no status kwarg -> the default
    _commit(dal)
    fitness = dal.get_company_fitness_map()
    assert fitness["Contoso Relief Fund"]["status"] == "candidate"


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Known, tracked divergence: database_supabase.AUTO_DISCOVERED_STATUS "
        "lands a brand-new board/ATS-discovered company 'active' on SQLite but "
        "'candidate' on Postgres, so simple mode skips the company-review gate "
        "entirely. A parallel fix unifies this to 'candidate' on both backends; "
        "once it lands the SQLite side of this test should pass too and this "
        "xfail marker should be removed."
    ),
)
def test_auto_discovered_company_status_matches_across_backends(backend):
    """save_vacancies() auto-discovering a brand-new company (no prior
    ensure_company call) must land it in the SAME status on both backends.
    Today it does not -- see the xfail reason above."""
    dal = backend
    new = dal.save_vacancies("Fictive Wildlife Alliance", "B", [_job("Ops Lead")])
    _commit(dal)
    assert new == 1
    fitness = dal.get_company_fitness_map()
    assert fitness["Fictive Wildlife Alliance"]["status"] == "candidate"


def test_auto_review_candidates_approves_and_rejects_by_threshold(backend):
    dal = backend
    dal.ensure_company("High Fit Org", status="candidate")
    dal.ensure_company("Low Fit Org", status="candidate")
    dal.ensure_company("Grey Zone Org", status="candidate")
    _commit(dal)
    dal.save_company_enrichment("High Fit Org", alignment_score=90)
    dal.save_company_enrichment("Low Fit Org", alignment_score=5)
    dal.save_company_enrichment("Grey Zone Org", alignment_score=40)
    _commit(dal)

    result = dal.auto_review_candidates(approve_threshold=60, reject_threshold=25, enabled=True)
    _commit(dal)

    assert result["approved"] == ["High Fit Org"]
    assert result["rejected"] == ["Low Fit Org"]
    assert result["pending"] == ["Grey Zone Org"]

    fitness = dal.get_company_fitness_map()
    assert fitness["High Fit Org"]["status"] == "active"
    assert fitness["Low Fit Org"]["status"] == "inactive"
    assert fitness["Grey Zone Org"]["status"] == "candidate"


# ---------------------------------------------------------------------------
# 3. Job-board TTL math
# ---------------------------------------------------------------------------

_BOARD_CFG = {
    "name": "Fictive Impact Board",
    "strategy": "custom",
    "tier": "B",
    "ttl_days": 7,
    "url": "https://board.example/feed",
}


def _backdate_board(dal, board_id, days):
    """Push last_fetched `days` into the past using the SAME now()-interval
    idiom the DAL itself uses (get_archived_hashes), so the SQLite<->Postgres
    translation layer is exercised identically to production code -- not a
    hand-rolled Python datetime that could silently differ per backend."""
    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE board SET last_fetched = now() - interval '%s days' WHERE id = %s",
        (days, board_id),
    )
    cur.close()
    conn.commit()


def test_board_ttl_never_fetched_is_always_due(backend):
    dal = backend
    dal.sync_boards({"fictive_board": _BOARD_CFG})
    _commit(dal)
    assert dal.should_fetch_board("fictive_board", ttl_days=7) is True


def test_board_ttl_just_fetched_is_not_due(backend):
    dal = backend
    dal.sync_boards({"fictive_board": _BOARD_CFG})
    _commit(dal)
    dal.mark_board_fetched("fictive_board")
    _commit(dal)
    assert dal.should_fetch_board("fictive_board", ttl_days=7) is False


def test_board_ttl_boundary_math_matches_across_backends(backend):
    """The exact TTL boundary (days-elapsed >= ttl_days) must resolve the
    same way on both backends -- the specific arithmetic the backend audit
    flagged as a latency risk (should_fetch_board mixes a decoded SQLite
    timestamp / a native Postgres TIMESTAMPTZ into datetime.now(tzinfo) math)."""
    dal = backend
    dal.sync_boards({"fictive_board": _BOARD_CFG})
    _commit(dal)
    dal.mark_board_fetched("fictive_board")
    _commit(dal)
    _backdate_board(dal, "fictive_board", days=10)

    assert dal.should_fetch_board("fictive_board", ttl_days=7) is True  # 10 >= 7 -> due
    assert dal.should_fetch_board("fictive_board", ttl_days=14) is False  # 10 < 14 -> not due
    assert dal.should_fetch_board("fictive_board", ttl_days=10) is True  # boundary: 10 >= 10


# ---------------------------------------------------------------------------
# 4. Archive / resurrect
# ---------------------------------------------------------------------------


def test_restore_archived_vacancy_to_unseen(backend):
    dal = backend
    dal.ensure_company("Fictive Robotics Guild", status="active")
    dal.save_vacancies("Fictive Robotics Guild", "A", [_job("Programme Lead")])
    _commit(dal)
    vid = _by_title(dal, "Programme Lead")

    dal.archive_gone_vacancies("Fictive Robotics Guild", [])
    _commit(dal)
    assert dal.load_vacancies(include_inactive_companies=True)[vid]["status"] == "archived"

    dal.update_vacancy_status(vid, "unseen")
    _commit(dal)
    assert dal.load_vacancies(include_inactive_companies=True)[vid]["status"] == "unseen"


def test_archive_gone_never_touches_a_decided_status(backend):
    dal = backend
    dal.ensure_company("Fictive Robotics Guild", status="active")
    dal.save_vacancies("Fictive Robotics Guild", "A", [_job("Liked Role"), _job("Unseen Role")])
    _commit(dal)
    liked_id = _by_title(dal, "Liked Role")
    dal.update_vacancy_status(liked_id, "liked")
    _commit(dal)

    # Fresh listing is empty -> both roles are "gone", but only the unseen one
    # may be archived; the liked decision must survive untouched.
    archived = dal.archive_gone_vacancies("Fictive Robotics Guild", [])
    _commit(dal)
    assert archived == 1

    statuses = {
        v["title"]: v["status"]
        for v in dal.load_vacancies(include_inactive_companies=True).values()
    }
    assert statuses["Liked Role"] == "liked"
    assert statuses["Unseen Role"] == "archived"


def test_direct_refetch_resurrects_gone_role(backend):
    dal = backend
    dal.ensure_company("Fictive Robotics Guild", status="active")
    dal.save_vacancies("Fictive Robotics Guild", "A", [_job("Programme Lead")])
    _commit(dal)
    vid = _by_title(dal, "Programme Lead")

    dal.archive_gone_vacancies("Fictive Robotics Guild", [])
    _commit(dal)
    assert dal.load_vacancies(include_inactive_companies=True)[vid]["status"] == "archived"

    # The company's OWN ATS lists it again -> merge resurrects it to unseen.
    new = dal.save_vacancies("Fictive Robotics Guild", "A", [_job("Programme Lead")])
    _commit(dal)
    assert new == 0  # same dedup_hash, existing row -- not counted as new
    assert dal.load_vacancies(include_inactive_companies=True)[vid]["status"] == "unseen"
