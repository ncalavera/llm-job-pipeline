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


def test_save_vacancies_merges_renamed_role_on_both_backends(backend):
    """A seniority-word rename of the same role must merge onto the live row
    (one vacancy), inheriting a user decision instead of resurfacing as unseen
    -- identical on SQLite and Postgres."""
    dal = backend
    req = "https://example.test/req/geo-pm"  # same req across the rename
    dal.ensure_company("Northwind Aid Trust", status="active")
    dal.save_vacancies(
        "Northwind Aid Trust", "B", [_job("Product Manager, Geo Expansion", url=req)]
    )
    _commit(dal)
    h = dal.make_vacancy_id("Northwind Aid Trust", "Product Manager, Geo Expansion")
    cur = dal.get_conn().cursor()
    cur.execute("UPDATE vacancy SET status = 'applied' WHERE dedup_hash = %s", (h,))
    cur.close()
    _commit(dal)

    new = dal.save_vacancies(
        "Northwind Aid Trust", "B", [_job("Senior Product Manager, Geo Expansion", url=req)]
    )
    _commit(dal)

    rows = dal.load_vacancies(include_inactive_companies=True)
    assert new == 0
    assert len(rows) == 1
    assert next(iter(rows.values()))["status"] == "applied"


def test_archive_gone_keeps_renamed_live_role_on_both_backends(backend):
    """A role re-listed under a renamed title is still live -- archive_gone must
    not tombstone it, on either backend."""
    dal = backend
    req = "https://example.test/req/analyst"  # same req across the rename
    dal.ensure_company("Northwind Aid Trust", status="active")
    dal.save_vacancies("Northwind Aid Trust", "B", [_job("Data Analyst", url=req)])
    _commit(dal)

    listing = [_job("Senior Data Analyst", url=req)]
    dal.save_vacancies("Northwind Aid Trust", "B", listing)
    _commit(dal)
    archived = dal.archive_gone_vacancies("Northwind Aid Trust", listing)
    _commit(dal)

    rows = dal.load_vacancies(include_inactive_companies=True)
    assert int(archived) == 0
    assert len(rows) == 1
    assert next(iter(rows.values()))["status"] == "unseen"


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
# Application entity -- create / idempotent-per-vacancy / status move
# must behave identically on SQLite and Postgres, including artifact-JSON merge.
# ---------------------------------------------------------------------------


def test_application_lifecycle_parity(backend):
    dal = backend
    import applications

    cid = dal.ensure_company("Northwind Aid Trust", status="active")
    _commit(dal)
    dal.save_vacancies("Northwind Aid Trust", "B", [_job("Programme Officer")])
    _commit(dal)
    vid = _by_title(dal, "Programme Officer")

    app_id = applications.record_application(
        cid, vid, channel="site", artifacts={"cv_version": "v1.pdf"}
    )
    # Idempotent per vacancy: a second record UPDATES the same row and MERGES
    # artifacts -- identical on both backends despite JSONB vs JSON-text storage.
    again = applications.record_application(
        cid, vid, status="interview", artifacts={"cover_letter_path": "cl.md"}
    )
    assert app_id == again
    assert len(applications.list_for_company(cid)) == 1

    app = applications.get_for_vacancy(vid)
    assert app["status"] == "interview"
    assert app["artifacts"] == {"cv_version": "v1.pdf", "cover_letter_path": "cl.md"}

    assert applications.set_status(app_id, "offer")
    assert applications.get(app_id)["status"] == "offer"


def test_application_re_record_preserves_fields_parity(backend):
    """Re-recording without channel/applied_at preserves both (never NULLs the
    channel, never resets applied_at to today); an explicit value still wins.
    Identical on SQLite (TEXT) and Postgres (DATE), the adapter boundary aside."""
    dal = backend
    import applications

    cid = dal.ensure_company("Northwind Aid Trust", status="active")
    _commit(dal)
    dal.save_vacancies("Northwind Aid Trust", "B", [_job("Programme Officer")])
    _commit(dal)
    vid = _by_title(dal, "Programme Officer")

    applications.record_application(cid, vid, channel="site", applied_at="2026-01-10")
    # Re-record to move status / add an artifact, passing neither field.
    applications.record_application(
        cid, vid, status="interview", artifacts={"cover_letter_path": "cl.md"}
    )
    app = applications.get_for_vacancy(vid)
    assert app["status"] == "interview"
    assert app["channel"] == "site"  # preserved, not clobbered to NULL
    assert app["applied_at"] == "2026-01-10"  # preserved, not reset to today

    # A later re-record with explicit values still overwrites.
    applications.record_application(cid, vid, channel="referral", applied_at="2026-02-20")
    app = applications.get_for_vacancy(vid)
    assert app["channel"] == "referral"
    assert app["applied_at"] == "2026-02-20"


def test_company_evidence_research_write_parity(backend):
    """save_company_evidence lands research in the same table the WANT scorer
    reads, idempotent by (company_id, source, url), on both backends."""
    dal = backend
    cid = dal.ensure_company("Northwind Aid Trust", status="active")
    _commit(dal)

    dal.save_company_evidence(cid, "manual_url", url="https://example.test/impact", content="first")
    dal.save_company_evidence(
        cid, "manual_url", url="https://example.test/impact", content="second"
    )
    rows = dal.load_company_evidence_summary().get(str(cid), [])
    assert len(rows) == 1
    assert rows[0]["source"] == "manual_url"


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


def test_auto_discovered_company_status_matches_across_backends(backend):
    """save_vacancies() auto-discovering a brand-new company (no prior
    ensure_company call) must land it in the SAME status on both backends --
    the default rule (config/defaults.toml [thresholds] auto_discovery_status,
    "candidate") is a setting, not a derivative of IS_SQLITE."""
    dal = backend
    new = dal.save_vacancies("Fictive Wildlife Alliance", "B", [_job("Ops Lead")])
    _commit(dal)
    assert new == 1
    fitness = dal.get_company_fitness_map()
    assert fitness["Fictive Wildlife Alliance"]["status"] == "candidate"


def test_auto_discovered_company_known_registry_name_still_gated(backend, monkeypatch):
    """A brand-new company whose name happens to match
    company_registry._ALL_KNOWN_NAMES must NOT be fast-tracked to 'active' --
    on EITHER backend. Regression for the old save_board_vacancies branch that
    special-cased a "known" name straight to status='active', skipping the
    candidate gate regardless of backend. The fixed DAL does not even look at
    the registry for this decision any more; patching it here proves that."""
    import company_registry

    dal = backend
    monkeypatch.setattr(company_registry, "_ALL_KNOWN_NAMES", {"Fictive Registry Org"})

    board_cfg = {
        "name": "Fictive Registry Org",
        "strategy": "custom",
        "tier": "B",
        "url": "https://board.example/fictive-registry",
    }
    new = dal.save_board_vacancies(board_cfg, [_job("Registry Role")])
    _commit(dal)

    assert new == 1
    fitness = dal.get_company_fitness_map()
    assert fitness["Fictive Registry Org"]["status"] == "candidate"  # gated, not fast-tracked


def test_auto_discovery_status_setting_overrides_identically(backend, monkeypatch):
    """The auto-discovery status rule is a setting: overriding it via
    AUTO_DISCOVERY_STATUS must flip the outcome the SAME way on both
    backends."""
    dal = backend
    monkeypatch.setenv("AUTO_DISCOVERY_STATUS", "active")

    new = dal.save_vacancies("Fictive Opt-In Org", "B", [_job("Program Lead")])
    _commit(dal)

    assert new == 1
    fitness = dal.get_company_fitness_map()
    assert fitness["Fictive Opt-In Org"]["status"] == "active"


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


def test_board_enabled_flag_round_trips(backend):
    """A board is enabled/disabled by a persisted flag, identical on both
    backends. Syncing the catalog does NOT enable it (boards stay opt-in);
    only set_board_enabled(..., True) does."""
    dal = backend
    dal.sync_boards({"fictive_board": _BOARD_CFG})
    _commit(dal)
    assert dal.get_enabled_boards() == []  # synced != enabled -- boards are opt-in

    dal.set_board_enabled("fictive_board", True)  # commits internally
    assert dal.get_enabled_boards() == ["fictive_board"]

    dal.set_board_enabled("fictive_board", False)
    assert dal.get_enabled_boards() == []


def test_set_board_enabled_persists_across_reconnect(backend):
    """The whole point of the feature: an enabled board survives the process. Prove
    the write is committed (not merely held in the session) by dropping the
    connection and reading back on a fresh one -- and that enabling a board that
    was never synced upserts a bare catalog row rather than failing."""
    dal = backend
    dal.set_board_enabled("fictive_ghost_board", True)  # never synced -> bare upsert + commit
    dal.close_conn()
    assert "fictive_ghost_board" in dal.get_enabled_boards()


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
