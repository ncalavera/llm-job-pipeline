"""Archive, tombstone, restore, expiry protection, and last-seen refresh.

Covers the full lifecycle of a vacancy row once its posting goes quiet: the
gone-from-source and board-stale sweeps that archive it, the tombstone
(archived_hash) table that blocks a re-import within its TTL, restoring an
archived row back to the live catalog, the high-fit "expiring" protection that
keeps a strong role visible instead of silently burying it, and the gated
last-seen refresh that keeps a still-listed-but-filtered role from rendering
as falsely expired. Absorbs:

  * tests/test_archive_restore.py
  * tests/test_archive_stale_board.py
  * tests/test_archived_hashes_ttl.py
  * tests/test_score_tombstone_no_resurrect.py
  * tests/test_protect_expiring.py
  * tests/test_gated_last_seen_refresh.py

Fully offline on the local SQLite backend throughout. All orgs/roles invented.
"""

import importlib
import sys
from datetime import date, datetime, timedelta, timezone

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db

    yield db
    db.close_conn()


def _job(title, city="Berlin, Germany"):
    return {
        "title": title,
        "snippet": f"{title} blurb.",
        "full_description": f"We are hiring a {title}. " * 12,
        "location": city,
        "url": f"https://acme.example/job/{title.lower().replace(' ', '-')}",
    }


def _id_by_title(db, title):
    for vid, v in db.load_vacancies(include_inactive_companies=True).items():
        if v["title"] == title:
            return vid
    raise AssertionError(f"vacancy {title!r} not found")


def _commit(db):
    db.get_conn().commit()


# ===========================================================================
# --- from test_archive_restore.py ---
# ===========================================================================
#
# Tests for the archive + restore flow (gone-from-source, restore, resurrect).
#
# Existing coverage:
#   * test_sqlite_backend.test_gone_from_source_archives_unseen — archive_gone
#     archives an unseen role absent from the fresh listing.
#   * test_e2e_pipeline.test_day2_refetch_dedup_new_and_gone — archive_gone in the
#     day-2 refetch.
#
# This section fills the GAPS those leave open:
#   * Restore: an archived vacancy moved back to 'unseen' via update_vacancy_status
#     re-enters the live catalog.
#   * Direct-ATS resurrection: a vacancy archived as gone-from-source that the
#     company's OWN ATS re-lists is resurrected to 'unseen' on the next
#     save_vacancies (include_gone=False path).
#   * Job-board cooldown: a board re-import of a gone-from-source posting is
#     SKIPPED within the TTL cooldown (include_gone=True path), so a lagging board
#     can't undo the source's closure.
#   * Decided statuses are NOT archived by archive_gone_vacancies.

# ---------------------------------------------------------------------------
# Restore: archived → unseen re-enters the live catalog
# ---------------------------------------------------------------------------


def test_restore_archived_vacancy_to_unseen(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Programme Lead")])
    _commit(dal)
    vid = _id_by_title(dal, "Programme Lead")

    # Archive it (gone from source).
    dal.archive_gone_vacancies("Acme Robotics", [])
    _commit(dal)
    assert dal.load_vacancies()[vid]["status"] == "archived"
    # And it drops out of an archived-excluding listing (how vac.py list shows it).
    live = dal.load_vacancies(status_exclude=["archived"])
    assert vid not in live

    # Restore: move back to unseen.
    dal.update_vacancy_status(vid, "unseen")
    _commit(dal)
    # Now visible again in the archived-excluding catalog.
    live = dal.load_vacancies(status_exclude=["archived"])
    assert vid in live
    assert live[vid]["status"] == "unseen"


# ---------------------------------------------------------------------------
# archive_gone never touches a DECIDED status
# ---------------------------------------------------------------------------


def test_archive_gone_skips_decided_status(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies(
        "Acme Robotics",
        "A",
        [
            _job("Liked Role"),
            _job("Unseen Role"),
        ],
    )
    _commit(dal)
    liked = _id_by_title(dal, "Liked Role")
    dal.update_vacancy_status(liked, "liked")
    _commit(dal)

    # Fresh listing is empty → both roles are "gone", but only the unseen one
    # may be archived; the liked decision is protected.
    archived = dal.archive_gone_vacancies("Acme Robotics", [])
    _commit(dal)
    assert archived == 1

    final = {
        v["title"]: v["status"]
        for v in dal.load_vacancies(include_inactive_companies=True).values()
    }
    assert final["Liked Role"] == "liked"  # protected
    assert final["Unseen Role"] == "archived"  # archived


# ---------------------------------------------------------------------------
# Direct-ATS resurrection: the company's own re-listing revives a gone role
# ---------------------------------------------------------------------------


def test_direct_refetch_resurrects_gone_role(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Programme Lead")])
    _commit(dal)
    vid = _id_by_title(dal, "Programme Lead")

    # Source drops it → archived as gone_from_source.
    dal.archive_gone_vacancies("Acme Robotics", [])
    _commit(dal)
    assert dal.load_vacancies()[vid]["status"] == "archived"

    # The company's OWN ATS lists it again → merge resurrects it to unseen
    # (include_gone=False ignores the gone_from_source cooldown for the source).
    new = dal.save_vacancies("Acme Robotics", "A", [_job("Programme Lead")])
    _commit(dal)
    # Not counted as new (same dedup_hash, existing row), but un-archived.
    assert new == 0
    assert dal.load_vacancies()[vid]["status"] == "unseen"


# ---------------------------------------------------------------------------
# Score-below-threshold archive hash is recorded and skips re-merge
# ---------------------------------------------------------------------------


def test_score_below_threshold_hash_blocks_remerge(dal):
    """A dedup_hash archived for a non-gone reason (e.g. score_below_threshold)
    is within the TTL cooldown, so a fresh merge of the same role is skipped —
    the score-archive flow's data guarantee."""
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Low Score Role")])
    _commit(dal)
    vid = _id_by_title(dal, "Low Score Role")
    h = dal.load_vacancies()[vid]["dedup_hash"]

    # Simulate the low-score archive: delete the row and record its hash.
    dal.delete_vacancies([vid])
    dal.record_archived_hashes([(h, "score_below_threshold")])
    _commit(dal)
    assert h in dal.get_archived_hashes()
    assert dal.load_vacancies() == {}

    # Re-merge the same role: blocked by the cooldown (recently archived).
    new = dal.save_vacancies("Acme Robotics", "A", [_job("Low Score Role")])
    _commit(dal)
    assert new == 0
    assert dal.load_vacancies() == {}


# ===========================================================================
# --- from test_archive_stale_board.py ---
# ===========================================================================
#
# Tests for archive_stale_board_vacancies — the board-row gone-from-source sweep.
#
# Board-sourced vacancies have no ATS to reconcile against (archive_gone_vacancies
# only runs on a direct re-fetch), so an unseen board row that drops off its board
# lingers forever and wastes scoring budget. archive_stale_board_vacancies archives
# board rows (source_board IS NOT NULL) whose last_seen predates a board_stale_days
# cutoff — but ONLY for boards successfully fetched this run (positive evidence),
# never on wall-clock alone. High-fit rows (llm_score >= PROTECT_SCORE) flip to
# 'expiring' instead; archived rows are tombstoned 'gone_from_source'; decided
# statuses and company-fetched rows are never touched; 0 disables the sweep.
#
# Every call passes stale_days explicitly so a BOARD_STALE_DAYS env override on
# the host can never change the assertions (this section's fixture also clears
# it before the config reload, mirroring the SUPABASE_DB_URL hygiene) — this is
# why this section keeps its own ``dal_stale`` fixture rather than reusing the
# shared ``dal`` above.

BOARD = "80,000 Hours"
STALE = (date.today() - timedelta(days=30)).isoformat()
FRESH = (date.today() - timedelta(days=1)).isoformat()


def _force_sqlite_stale(monkeypatch, db_file):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.delenv("BOARD_STALE_DAYS", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as db

    return db


@pytest.fixture()
def dal_stale(tmp_path, monkeypatch):
    db = _force_sqlite_stale(monkeypatch, tmp_path / "jobsearch.db")
    yield db
    db.close_conn()


def _add_migration_columns(dal_stale):
    """Simulate a migrated DB (migrations 0013 + 0014) where vacancy carries
    source_board and status_reason. The base test schema predates both."""
    cur = dal_stale.get_conn().cursor()
    cur.execute("ALTER TABLE vacancy ADD COLUMN source_board TEXT")
    cur.execute("ALTER TABLE vacancy ADD COLUMN status_reason TEXT")
    dal_stale.get_conn().commit()
    cur.close()


@pytest.fixture()
def company(dal_stale):
    _add_migration_columns(dal_stale)
    dal_stale.ensure_company("Acme Labs", status="active")
    dal_stale.get_conn().commit()
    return dal_stale


def _insert(
    dal_stale,
    *,
    dedup_hash,
    title,
    last_seen,
    source_board,
    status="unseen",
    llm_score=None,
    status_reason=None,
):
    """Insert one vacancy row with explicit provenance/age for the sweep."""
    company_id = dal_stale.resolve_company_id("Acme Labs")
    cur = dal_stale.get_conn().cursor()
    cur.execute(
        "INSERT INTO vacancy (dedup_hash, company_id, title, first_seen, last_seen, "
        "status, source_board, llm_score, status_reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            dedup_hash,
            company_id,
            title,
            last_seen,
            last_seen,
            status,
            source_board,
            llm_score,
            status_reason,
        ),
    )
    dal_stale.get_conn().commit()
    cur.close()


def _row(dal_stale, dedup_hash):
    cur = dal_stale.get_conn().cursor()
    cur.execute("SELECT status, status_reason FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
    r = cur.fetchone()
    cur.close()
    return {"status": r[0], "status_reason": r[1]}


def _tombstone(dal_stale, dedup_hash):
    cur = dal_stale.get_conn().cursor()
    cur.execute("SELECT reason FROM archived_hash WHERE dedup_hash = %s", (dedup_hash,))
    r = cur.fetchone()
    cur.close()
    return r[0] if r else None


def test_stale_board_row_is_archived_with_token_and_tombstone(company):
    """A board-sourced unseen row older than the threshold, whose board WAS
    fetched this run, is archived with the machine token and a gone_from_source
    tombstone (so a stale board snapshot cannot resurrect it)."""
    dal_stale = company
    _insert(
        dal_stale,
        dedup_hash="h_stale",
        title="Stale Board Role",
        last_seen=STALE,
        source_board=BOARD,
    )

    n = dal_stale.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal_stale.get_conn().commit()

    assert n == 1
    row = _row(dal_stale, "h_stale")
    assert row["status"] == "archived"
    assert row["status_reason"] == "board_stale"
    assert _tombstone(dal_stale, "h_stale") == "gone_from_source"


def test_high_fit_row_flips_to_expiring_not_archived(company):
    """Latency protection (KTD1/KTD2): a stale board row scoring >= PROTECT_SCORE
    is flipped to 'expiring' (visible, alerted), never archived or tombstoned."""
    dal_stale = company
    _insert(
        dal_stale,
        dedup_hash="h_fit",
        title="High Fit Role",
        last_seen=STALE,
        source_board=BOARD,
        llm_score=84,
    )

    n = dal_stale.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal_stale.get_conn().commit()

    assert n == 0 and n.protected == 1
    row = _row(dal_stale, "h_fit")
    assert row["status"] == "expiring"
    assert row["status_reason"] is None
    assert _tombstone(dal_stale, "h_fit") is None


def test_unfetched_board_is_never_swept(company):
    """Positive-evidence precondition: a stale row whose board was NOT
    successfully fetched this run (skipped via --no-boards / TTL, or a broken
    fetcher) is untouched — wall-clock staleness alone is not evidence."""
    dal_stale = company
    _insert(
        dal_stale,
        dedup_hash="h_skip",
        title="TTL Skipped Role",
        last_seen=STALE,
        source_board=BOARD,
    )

    assert dal_stale.archive_stale_board_vacancies(set(), stale_days=14) == 0
    assert dal_stale.archive_stale_board_vacancies({"Other Board"}, stale_days=14) == 0
    dal_stale.get_conn().commit()
    assert _row(dal_stale, "h_skip")["status"] == "unseen"


def test_company_sourced_row_is_not_touched(company):
    """A company-fetched row (source_board NULL) is reconciled against its own ATS,
    never by this rule — even when equally stale."""
    dal_stale = company
    _insert(
        dal_stale,
        dedup_hash="h_company",
        title="Direct ATS Role",
        last_seen=STALE,
        source_board=None,
    )

    n = dal_stale.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal_stale.get_conn().commit()

    assert n == 0
    assert _row(dal_stale, "h_company")["status"] == "unseen"


def test_fresh_board_row_stays(company):
    """A board-sourced row still re-seen within the window is left untouched."""
    dal_stale = company
    _insert(
        dal_stale,
        dedup_hash="h_fresh",
        title="Fresh Board Role",
        last_seen=FRESH,
        source_board=BOARD,
    )

    n = dal_stale.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal_stale.get_conn().commit()

    assert n == 0
    assert _row(dal_stale, "h_fresh")["status"] == "unseen"


def test_decided_statuses_survive_the_sweep(company):
    """Only status='unseen' is swept: stale board rows the user already decided
    on (liked/to_apply) or that are held visible (expiring) are never revisited."""
    dal_stale = company
    for i, status in enumerate(("liked", "to_apply", "expiring")):
        _insert(
            dal_stale,
            dedup_hash=f"h_dec{i}",
            title=f"Decided Role {i}",
            last_seen=STALE,
            source_board=BOARD,
            status=status,
        )

    n = dal_stale.archive_stale_board_vacancies({BOARD}, stale_days=14)
    dal_stale.get_conn().commit()

    assert n == 0 and n.protected == 0
    for i, status in enumerate(("liked", "to_apply", "expiring")):
        assert _row(dal_stale, f"h_dec{i}")["status"] == status


def test_zero_stale_days_disables_the_sweep(company):
    """board_stale_days = 0 means DISABLED (llm_score_threshold convention) —
    NOT 'archive everything older than today'."""
    dal_stale = company
    _insert(
        dal_stale, dedup_hash="h_zero", title="Would Be Swept", last_seen=STALE, source_board=BOARD
    )

    n = dal_stale.archive_stale_board_vacancies({BOARD}, stale_days=0)
    dal_stale.get_conn().commit()

    assert n == 0
    assert _row(dal_stale, "h_zero")["status"] == "unseen"


def test_premigration_schema_is_a_noop(dal_stale):
    """On a schema predating migrations 0013/0014 (base test schema: no
    source_board / status_reason column) the sweep degrades to a clean no-op."""
    assert dal_stale.archive_stale_board_vacancies({BOARD}, stale_days=14) == 0


def test_board_resurrect_clears_status_reason(company):
    """A row archived by the sweep and later re-listed by its board resurrects to
    'unseen' with status_reason cleared — a machine-archival reason must never
    sit on a live row."""
    dal_stale = company
    org, title = "Acme Labs", "Reopened Role"
    h = dal_stale.make_vacancy_id(org, title)
    _insert(
        dal_stale,
        dedup_hash=h,
        title=title,
        last_seen=STALE,
        source_board=BOARD,
        status="archived",
        status_reason="board_stale",
    )

    # No tombstone in archived_hash here: the board genuinely re-lists the role.
    board_cfg = {"name": BOARD, "url": "https://board.test/feed", "tier": "B"}
    job = {
        "title": title,
        "org_override": org,
        "location": "Berlin, Germany",
        "snippet": "A genuine snippet long enough to clear the content gate here.",
        "full_description": "Real long job description body here. " * 6,
    }
    dal_stale.save_board_vacancies(board_cfg, [job])
    dal_stale.get_conn().commit()

    row = _row(dal_stale, h)
    assert row["status"] == "unseen"
    assert row["status_reason"] is None


# ===========================================================================
# --- from test_archived_hashes_ttl.py ---
# ===========================================================================
#
# Regression test: get_archived_hashes(ttl_days) TTL window.
#
# Context
# -------
# A plan refactor initially suspected that the TTL window was broken because
# ``interval '%s days'`` looks like an improperly-parameterised SQL literal.
# Empirically it works correctly on both backends:
#
# * SQLite — the translator in db_backend._translate_sql converts
#   ``now() - interval '%s days'`` → ``datetime('now', /*INTERVALDAYS*/)`` and
#   then _SqliteCursor._prepare rewrites the bound int param into ``'-N days'``.
# * psycopg2 — executes ``interval '%s days'`` with a plain ``%s`` substitution
#   inside the string literal, which Postgres handles correctly.
#
# This test is the regression guard: it proves that get_archived_hashes(ttl_days)
# correctly excludes rows older than ttl_days and includes rows within the window.
# The function is NOT modified.
#
# Date format note
# ----------------
# SQLite stores ``archived_at`` as TEXT with DEFAULT CURRENT_TIMESTAMP, which
# emits ``'YYYY-MM-DD HH:MM:SS'`` (no 'T', no timezone). The TTL filter compares
# ``archived_at > datetime('now', '-N days')`` — a pure lexicographic comparison.
# Python's ``datetime.isoformat()`` produces ``'YYYY-MM-DDTHH:MM:SS+HH:MM'``,
# which is lexicographically incompatible. We therefore format seeded dates with
# ``strftime('%Y-%m-%d %H:%M:%S', ...)`` to match the SQLite format.
#
# This section's fixture seeds two archived_hash rows on every use, so it keeps
# its own name, ``dal_ttl``, rather than sharing the plain ``dal`` above.


def _force_sqlite(monkeypatch, db_file):
    """Point the whole chain at a fresh temp SQLite file and reload it."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "TTL test must run on the SQLite backend"
    import database_supabase as db

    return db


@pytest.fixture()
def dal_ttl(tmp_path, monkeypatch):
    """DAL bound to an isolated temp SQLite database, seeded with two rows."""
    db = _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")

    now = datetime.now(tz=timezone.utc)
    # Format must match SQLite CURRENT_TIMESTAMP: 'YYYY-MM-DD HH:MM:SS'
    old_iso = (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")
    recent_iso = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason, archived_at) VALUES (%s, %s, %s)",
        ("h_old", "low_score", old_iso),
    )
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason, archived_at) VALUES (%s, %s, %s)",
        ("h_recent", "low_score", recent_iso),
    )
    conn.commit()
    cur.close()

    yield db
    db.close_conn()


def test_default_ttl_excludes_old_includes_recent(dal_ttl):
    """Default 90-day window: h_recent in, h_old out."""
    result = dal_ttl.get_archived_hashes()  # ttl_days=90 by default
    assert "h_recent" in result, "h_recent (10 days old) must be within 90-day TTL"
    assert "h_old" not in result, "h_old (100 days old) must be outside 90-day TTL"


def test_large_ttl_includes_both(dal_ttl):
    """Very large TTL (99999 days): both hashes returned."""
    result = dal_ttl.get_archived_hashes(99999)
    assert "h_recent" in result
    assert "h_old" in result


def test_small_ttl_excludes_both(dal_ttl):
    """Very small TTL (5 days): neither hash is within 5 days."""
    result = dal_ttl.get_archived_hashes(5)
    assert "h_recent" not in result, "h_recent (10 days old) must be outside 5-day TTL"
    assert "h_old" not in result, "h_old (100 days old) must be outside 5-day TTL"


# ===========================================================================
# --- from test_score_tombstone_no_resurrect.py ---
# ===========================================================================
#
# Regression: a 'score_below_threshold' tombstone is not resurrected.
#
# A vacancy we buried for a low score must NOT loop through
# resurrection -> re-score -> re-archive every run when the company's own ATS keeps
# listing it. The direct-ATS save path loads get_archived_hashes(include_gone=False),
# which drops ONLY 'gone_from_source' tombstones (so a role the source merely
# dropped can reopen) while keeping every other reason — crucially
# 'score_below_threshold' — in the blocking set.
#
# Contrast pinned here: a 'gone_from_source' tombstone does NOT block the direct
# ATS path (the company re-listing is ground truth the role reopened).
#
# This section's fixture uses the shared ``_force_sqlite`` helper from the TTL
# section above but does not seed any rows, so it gets its own ``dal_score``.


@pytest.fixture()
def dal_score(tmp_path, monkeypatch):
    db = _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    yield db
    db.close_conn()


def _job_score(title):
    return {
        "title": title,
        "location": "Berlin, Germany",
        "snippet": "A genuine snippet long enough to clear the content gate here.",
        "full_description": "Real long job description body here. " * 6,
    }


def _rows(dal_score, dedup_hash):
    cur = dal_score.get_conn().cursor()
    cur.execute("SELECT COUNT(*) FROM vacancy WHERE dedup_hash = %s", (dedup_hash,))
    n = cur.fetchone()[0]
    cur.close()
    return n


def test_score_below_threshold_tombstone_blocks_reimport(dal_score):
    """Buried-for-low-score role: archived (row deleted + tombstone), then the ATS
    lists it again -> save_vacancies must skip it (no re-insert, no re-score)."""
    dal_score.ensure_company("Acme Labs", status="active")
    dal_score.get_conn().commit()
    dal_score.save_vacancies("Acme Labs", "A", [_job_score("Junior Data Entry")])
    dal_score.get_conn().commit()
    h = dal_score.make_vacancy_id("Acme Labs", "Junior Data Entry")

    cur = dal_score.get_conn().cursor()
    cur.execute("UPDATE vacancy SET llm_score = 8, status = 'unseen' WHERE dedup_hash = %s", (h,))
    dal_score.get_conn().commit()
    cur.close()

    dal_score.archive_vacancies(force=True)  # deletes row + records 'score_below_threshold'
    assert _rows(dal_score, h) == 0

    cur = dal_score.get_conn().cursor()
    cur.execute("SELECT reason FROM archived_hash WHERE dedup_hash = %s", (h,))
    assert cur.fetchone()[0] == "score_below_threshold"
    cur.close()

    # The company's ATS still lists the same role next run.
    new = dal_score.save_vacancies("Acme Labs", "A", [_job_score("Junior Data Entry")])
    dal_score.get_conn().commit()

    assert new == 0, "a score_below_threshold role must not be re-imported"
    assert _rows(dal_score, h) == 0, "no resurrected row -> nothing to re-score/re-archive"


def test_gone_from_source_tombstone_does_not_block_reimport(dal_score):
    """Contrast: a 'gone_from_source' tombstone is dropped on the direct-ATS path,
    so the company re-listing the role resurrects it (new row inserted)."""
    dal_score.ensure_company("Acme Labs", status="active")
    dal_score.get_conn().commit()
    h = dal_score.make_vacancy_id("Acme Labs", "Senior Engineer")
    cur = dal_score.get_conn().cursor()
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason) VALUES (%s, %s)",
        (h, "gone_from_source"),
    )
    dal_score.get_conn().commit()
    cur.close()

    new = dal_score.save_vacancies("Acme Labs", "A", [_job_score("Senior Engineer")])
    dal_score.get_conn().commit()

    assert new == 1, "gone_from_source must not block a direct-ATS re-listing"
    assert _rows(dal_score, h) == 1


def test_include_gone_sets_partition_reasons(dal_score):
    """Pin the set semantics both paths rely on: include_gone=False keeps
    score_below_threshold but drops gone_from_source; include_gone=True keeps both."""
    cur = dal_score.get_conn().cursor()
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason) VALUES ('h_low', 'score_below_threshold')"
    )
    cur.execute(
        "INSERT INTO archived_hash (dedup_hash, reason) VALUES ('h_gone', 'gone_from_source')"
    )
    dal_score.get_conn().commit()
    cur.close()

    ats = dal_score.get_archived_hashes(include_gone=False)  # direct ATS path
    board = dal_score.get_archived_hashes(include_gone=True)  # board path

    assert "h_low" in ats and "h_gone" not in ats
    assert "h_low" in board and "h_gone" in board


# ===========================================================================
# --- from test_protect_expiring.py ---
# ===========================================================================
#
# U2 — latency protection: high-fit roles are never silently lost.
#
# Unseen roles scoring >= PROTECT_SCORE that go gone-from-source or expire past
# their deadline flip to 'expiring' (kept visible, alerted) instead of being
# silently archived/passed. Below the threshold, behaviour is unchanged. A
# re-listed 'expiring' role resurrects to a normal active state.


def _set(db, vid, **cols):
    cur = db.get_conn().cursor()
    sets = ", ".join(f"{k} = %s" for k in cols)
    cur.execute(f"UPDATE vacancy SET {sets} WHERE id = %s", list(cols.values()) + [vid])
    cur.close()
    _commit(db)


def _status(db, title):
    return db.load_vacancies(include_inactive_companies=True)[_id_by_title(db, title)]["status"]


# ---------------------------------------------------------------------------
# Gone-from-source: protect >= PROTECT_SCORE, archive the rest
# ---------------------------------------------------------------------------


def test_high_fit_gone_becomes_expiring_not_archived(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Senior Advisor")])
    _commit(dal)
    vid = _id_by_title(dal, "Senior Advisor")
    _set(dal, vid, llm_score=89)

    archived = dal.archive_gone_vacancies("Acme Robotics", [])  # empty listing
    _commit(dal)
    assert archived == 0, "a protected role must not count as archived"
    assert archived.protected == 1, "it must still be counted as vanished-from-source"
    assert dal.load_vacancies()[vid]["status"] == "expiring"
    # NOT tombstoned — a re-listing must be free to resurrect it.
    h = dal.load_vacancies()[vid]["dedup_hash"]
    assert h not in dal.get_archived_hashes()


def test_low_fit_gone_still_archived(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Junior Clerk")])
    _commit(dal)
    vid = _id_by_title(dal, "Junior Clerk")
    _set(dal, vid, llm_score=45)

    archived = dal.archive_gone_vacancies("Acme Robotics", [])
    _commit(dal)
    assert archived == 1
    assert archived.protected == 0
    assert dal.load_vacancies()[vid]["status"] == "archived"


def test_archived_count_is_backward_compatible_int_with_protected_extra(dal):
    """archive_gone_vacancies must keep behaving as a plain int for callers
    that only ever cared about the archived count (arithmetic, int(x or 0),
    equality) while exposing the protected count as an extra attribute — this
    is what lets fetch_vacancies.py fold protected-expiring into its "gone"
    telemetry without breaking any existing caller."""
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Head of Programmes"), _job("Filing Assistant")])
    _commit(dal)
    _set(dal, _id_by_title(dal, "Head of Programmes"), llm_score=72)  # protected
    _set(dal, _id_by_title(dal, "Filing Assistant"), llm_score=30)  # archived

    result = dal.archive_gone_vacancies("Acme Robotics", [])
    _commit(dal)
    assert result == 1  # archived count, unchanged contract
    assert isinstance(result, int)
    assert int(result or 0) == 1
    assert result.protected == 1
    # This is exactly the sum fetch_vacancies.py writes into fetch_stats.json's
    # "gone" telemetry so the publish gate sees every vanished-from-source role.
    assert int(result or 0) + result.protected == 2


# ---------------------------------------------------------------------------
# Past deadline: protect >= PROTECT_SCORE, pass the rest
# ---------------------------------------------------------------------------


def test_high_fit_expired_becomes_expiring_not_passed(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies(
        "Acme Robotics",
        "A",
        [
            _job("Head of Programmes"),
            _job("Filing Assistant"),
        ],
    )
    _commit(dal)
    hi = _id_by_title(dal, "Head of Programmes")
    lo = _id_by_title(dal, "Filing Assistant")
    _set(dal, hi, llm_score=72, deadline="2020-01-01")
    _set(dal, lo, llm_score=30, deadline="2020-01-01")

    dal.pass_expired_vacancies()
    _commit(dal)
    assert _status(dal, "Head of Programmes") == "expiring"
    assert _status(dal, "Filing Assistant") == "passed"


def test_decided_role_untouched_by_auto_pass(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Liked Role")])
    _commit(dal)
    vid = _id_by_title(dal, "Liked Role")
    _set(dal, vid, llm_score=80, deadline="2020-01-01")
    dal.update_vacancy_status(vid, "liked")
    _commit(dal)

    dal.pass_expired_vacancies()
    _commit(dal)
    # Auto-pass only ever touches 'unseen'; the user's decision is preserved.
    assert _status(dal, "Liked Role") == "liked"


# ---------------------------------------------------------------------------
# Resurrection: a re-listed expiring role returns to active
# ---------------------------------------------------------------------------


def test_expiring_role_resurrects_on_relisting(dal):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies("Acme Robotics", "A", [_job("Senior Advisor")])
    _commit(dal)
    vid = _id_by_title(dal, "Senior Advisor")
    _set(dal, vid, llm_score=89)

    dal.archive_gone_vacancies("Acme Robotics", [])  # → expiring
    _commit(dal)
    assert dal.load_vacancies()[vid]["status"] == "expiring"
    # Pretend the loud alert already fired for it.
    _set(dal, vid, expiring_alerted_at="2026-06-20T00:00:00")

    # The company re-lists it → resurrected to a normal active state, and the
    # alert flag is cleared so a future expiry can re-alert.
    new = dal.save_vacancies("Acme Robotics", "A", [_job("Senior Advisor")])
    _commit(dal)
    assert new == 0
    row = dal.load_vacancies()[vid]
    assert row["status"] == "unseen"
    assert row["expiring_alerted_at"] is None


# ===========================================================================
# --- from test_gated_last_seen_refresh.py ---
# ===========================================================================
#
# Regression: a role the source STILL lists must refresh last_seen even when our
# import gate drops it.
#
# Bug: a "talent pool" posting the user had already liked/decided kept a frozen
# last_seen because ``_gate_job`` blacklists the title and ``save_vacancies`` /
# ``save_board_vacancies`` ``continue`` before touching the existing row. The
# Triage "Expired" column derives from ``last_seen >= STALE_SOURCE_DAYS``, so the
# still-open role rendered as expired (GiveWell "Talent pool" was the real case).
#
# Fix: when a gated job's exact org+title matches a stored row, bump its last_seen
# (``_refresh_gated_last_seen``) — a "still open at source" touch that never
# imports, resurrects or rescores.


def _pipeline_today(db) -> date:
    import config

    return datetime.now(config.DASHBOARD_TZ).date()


def _insert_row(db, org, title, *, status, last_seen, first_seen="2026-02-22"):
    """Insert a vacancy row DIRECTLY (the gate would block save_vacancies from
    importing a junk title), mirroring a role the source listed before it became
    junk-blacklisted / that the user has already decided on."""
    cid = db.resolve_company_id(org) or db.ensure_company(org, status="active")
    dedup = db.make_vacancy_id(org, title)
    cur = db.get_conn().cursor()
    cur.execute(
        """INSERT INTO vacancy (dedup_hash, company_id, title, snippet, full_description,
                first_seen, last_seen, status, locations)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            dedup,
            cid,
            title,
            "join our pool",
            "We keep your CV on file for future roles. " * 20,
            first_seen,
            last_seen,
            status,
            db.Json([{"work_mode": "remote", "url": "https://boards.example/talent-pool"}]),
        ),
    )
    db.get_conn().commit()
    cur.close()
    return dedup


def _last_seen(db, dedup):
    cur = db.get_conn().cursor()
    cur.execute("SELECT last_seen, status FROM vacancy WHERE dedup_hash = %s", (dedup,))
    row = cur.fetchone()
    cur.close()
    return row  # (last_seen: date, status)


def _talent_pool_job():
    """A greenhouse-style row for a 'Talent pool' posting the source still lists."""
    return {
        "title": "Talent pool",
        "location": "Remote",
        "url": "https://boards.example/talent-pool",
        "external_id": "4053379008",
        "snippet": "join our pool",
        "full_description": "We keep your CV on file for future roles. " * 20,
    }


# ---------------------------------------------------------------------------
# Company (direct-ATS) path — the GiveWell greenhouse case.
# ---------------------------------------------------------------------------


def test_gated_still_listed_role_refreshes_last_seen(dal):
    dal.ensure_company("GiveWell", status="active")
    stale = (_pipeline_today(dal) - timedelta(days=21)).isoformat()
    dedup = _insert_row(dal, "GiveWell", "Talent pool", status="skipped", last_seen=stale)

    before, before_status = _last_seen(dal, dedup)
    assert str(before) == stale
    assert before_status == "skipped"

    # The junk title is gated, so nothing is imported (new_count 0) — but the
    # already-known, still-listed role must have its last_seen refreshed.
    new_count = dal.save_vacancies("GiveWell", "S", [_talent_pool_job()])
    dal.get_conn().commit()
    assert new_count == 0

    after, after_status = _last_seen(dal, dedup)
    assert after == _pipeline_today(dal), "still-listed gated role should refresh to today"
    assert after_status == "skipped", "refresh must not resurrect or change status"

    # No phantom / duplicate row was created for the gated title.
    cur = dal.get_conn().cursor()
    cur.execute("SELECT count(*) FROM vacancy WHERE title = %s", ("Talent pool",))
    assert cur.fetchone()[0] == 1
    cur.close()


def test_gated_unknown_title_creates_no_row(dal):
    """A brand-new junk posting we do NOT already track updates zero rows — the
    gate still blocks the import, no phantom row appears."""
    dal.ensure_company("GiveWell", status="active")
    new_count = dal.save_vacancies("GiveWell", "S", [_talent_pool_job()])
    dal.get_conn().commit()
    assert new_count == 0
    cur = dal.get_conn().cursor()
    cur.execute("SELECT count(*) FROM vacancy")
    assert cur.fetchone()[0] == 0
    cur.close()


def test_gated_refresh_leaves_archived_tombstone_untouched(dal):
    """An archived (gone-from-source) junk row is NOT refreshed — a tombstone
    stays a tombstone; only live rows get the 'still listed' touch."""
    dal.ensure_company("GiveWell", status="active")
    stale = (_pipeline_today(dal) - timedelta(days=30)).isoformat()
    dedup = _insert_row(dal, "GiveWell", "Talent pool", status="archived", last_seen=stale)

    dal.save_vacancies("GiveWell", "S", [_talent_pool_job()])
    dal.get_conn().commit()

    after, after_status = _last_seen(dal, dedup)
    assert str(after) == stale, "archived tombstone last_seen must stay frozen"
    assert after_status == "archived"


# ---------------------------------------------------------------------------
# Board path — same fix in save_board_vacancies.
# ---------------------------------------------------------------------------


def test_board_gated_still_listed_role_refreshes_last_seen(dal):
    dal.ensure_company("GiveWell", status="active")
    stale = (_pipeline_today(dal) - timedelta(days=18)).isoformat()
    dedup = _insert_row(dal, "GiveWell", "Talent pool", status="liked", last_seen=stale)

    board_cfg = {"name": "GiveWell", "url": "https://boards.example", "tier": "C"}
    job = _talent_pool_job()
    dal.save_board_vacancies(board_cfg, [job])
    dal.get_conn().commit()

    after, after_status = _last_seen(dal, dedup)
    assert after == _pipeline_today(dal)
    assert after_status == "liked"
