"""Before/after SNAPSHOT test for vacancy filtering — the safety net for the
filters refactor (scripts/filters.py consolidation).

It pins the OUTCOME (stable_key=(org,title) → outcome) of a curated vacancy set
at FOUR entry points. It must be GREEN on the current code and stay GREEN,
unchanged, after the refactor moves the filter helpers into scripts/filters.py.

Entry points:
  1. save_vacancies        (database_supabase) — direct ATS write path.
  2. save_board_vacancies  (database_supabase) — job-board write path.
  3. classify_vacancies     (filter_vacancies)  — read-only /filter triage.
  4. score._load_and_dedup  (score_vacancies)   — pre-score dedup + gate.

Everything is keyed by stable_key=(org,title) — NEVER by UUID (random) and
NEVER against a live DB. Each entry point runs on its own fresh temp SQLite DB
with clocks frozen to 2026-06-23, so outcomes are deterministic.

The expected outcomes (GOLDEN, below) were captured empirically from the
current code — every behaviour class was verified to actually fire (no stubs).
"""

import importlib
import json
import sys
from datetime import date, datetime, timedelta as _td
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "filters_snapshot_vacancies.json"
FROZEN_TODAY = date(2026, 6, 23)


# ---------------------------------------------------------------------------
# Harness — isolated temp SQLite + frozen clock (mirrors test_e2e_pipeline.py)
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
    assert db_backend.IS_SQLITE, "snapshot test must run on the SQLite backend"
    import database_supabase as db

    return db


class _FrozenDateMeta(type):
    """Make isinstance(real_date, _FrozenDate) True so the DAL's
    `isinstance(value, date)` date→ISO conversion keeps working after we swap
    the module-level `date` for the frozen subclass."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, date)


class _FrozenDate(date, metaclass=_FrozenDateMeta):
    """A date subclass whose today() is pinned to FROZEN_TODAY.

    Everything else (fromisoformat, arithmetic, .days) delegates to real date.
    """

    @classmethod
    def today(cls):
        return FROZEN_TODAY


class _FrozenDateTimeMeta(type):
    """Mirrors _FrozenDateMeta for datetime (isinstance stays truthful)."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, datetime)


class _FrozenDateTime(datetime, metaclass=_FrozenDateTimeMeta):
    """A datetime subclass whose now() is pinned to FROZEN_TODAY (any tz).

    database_supabase stamps first_seen/last_seen via
    datetime.now(DASHBOARD_TZ).date() — deterministic regardless of the fetch
    machine's local tz; freezing datetime.now() alongside date.today() keeps
    that stamp pinned too, whatever DASHBOARD_TZ resolves to.
    """

    @classmethod
    def now(cls, tz=None):
        return datetime(FROZEN_TODAY.year, FROZEN_TODAY.month, FROZEN_TODAY.day, tzinfo=tz)


def _freeze_clock(monkeypatch, module):
    """Pin `module.date.today()` and `module.datetime.now()` to FROZEN_TODAY.

    Both database_supabase and filter_vacancies do `from datetime import date,
    datetime` and call date.today() / date.fromisoformat() / datetime.now();
    swapping the module-level names for the frozen subclasses freezes today()
    and now() while keeping fromisoformat()/arithmetic/isinstance() intact.
    """
    monkeypatch.setattr(module, "date", _FrozenDate)
    if hasattr(module, "datetime"):
        monkeypatch.setattr(module, "datetime", _FrozenDateTime)


def _fresh_db(tmp_path, monkeypatch, name):
    db = _force_sqlite(monkeypatch, tmp_path / name)
    _freeze_clock(monkeypatch, db)
    return db


def _load_fixtures():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["_frozen_today"] == FROZEN_TODAY.isoformat()
    return data["vacancies"]


def _stable_key(entry):
    return (entry["org"], entry["job"]["title"])


def _excluded_countries():
    """Union of every _excluded_country a fixture entry needs."""
    out = set()
    for e in _load_fixtures():
        c = e.get("_excluded_country")
        if c:
            out.add(c)
    return frozenset(out)


# ---------------------------------------------------------------------------
# Seeding — pre-existing DB state described by each fixture's _seed block
# ---------------------------------------------------------------------------


def _seed_entry(db, entry):
    """Create whatever pre-existing state the fixture's _seed block asks for.

    Returns the set of dedup_hashes already present in the DB before the entry
    point runs (used to tell INSERTED from UPDATED).
    """
    seed = entry.get("_seed") or {}
    org = entry["org"]
    title = entry["job"]["title"]
    db.ensure_company(org, status="active")
    dedup_hash = db.make_vacancy_id(db.resolve_canonical_name(org), title)

    conn = db.get_conn()
    cur = conn.cursor()

    # Pre-existing vacancy row (resurrection / protected-liked cases). last_seen
    # is stamped OLD on purpose: an UPDATE bumps it to frozen-today, a SKIP
    # leaves it old — that delta tells UPDATE from SKIP for a row already present.
    existing_status = seed.get("existing_status")
    if existing_status:
        company_id = db.resolve_company_id(org)
        first = FROZEN_TODAY.isoformat()
        last_old = (FROZEN_TODAY - _td(days=30)).isoformat()
        cur.execute(
            "INSERT INTO vacancy (dedup_hash, company_id, title, snippet, "
            "full_description, first_seen, last_seen, locations, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                dedup_hash,
                company_id,
                title,
                entry["job"].get("snippet", ""),
                entry["job"].get("full_description", ""),
                first,
                last_old,
                "[]",
                existing_status,
            ),
        )

    # archived_hash tombstone (rearchived / resurrection cases). Default
    # CURRENT_TIMESTAMP keeps it inside the 90-day TTL.
    reason = seed.get("archived_hash_reason")
    if reason:
        cur.execute(
            "INSERT INTO archived_hash (dedup_hash, reason) VALUES (?, ?)",
            (dedup_hash, reason),
        )

    conn.commit()
    cur.close()
    return dedup_hash


def _apply_first_seen(db, entry, dedup_hash):
    """Overwrite first_seen for blind cases relative to the frozen today."""
    seed = entry.get("_seed") or {}
    days = seed.get("first_seen_days_ago")
    if days is None:
        return
    from datetime import timedelta

    fs = (FROZEN_TODAY - timedelta(days=days)).isoformat()
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE vacancy SET first_seen = ? WHERE dedup_hash = ?", (fs, dedup_hash))
    conn.commit()
    cur.close()


def _row_state(db, dedup_hash):
    """Return (status, last_seen) for a vacancy, or (None, None) when absent."""
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, last_seen FROM vacancy WHERE dedup_hash = ?", (dedup_hash,))
    row = cur.fetchone()
    cur.close()
    return (row[0], row[1]) if row else (None, None)


# ===========================================================================
# Entry point 1 + 2 — write paths (save_vacancies / save_board_vacancies)
# ===========================================================================


def _run_write_path(tmp_path, monkeypatch, *, board):
    """Run every fixture through a write path, one fresh DB per fixture, and
    classify each into an outcome.

    Outcomes: INSERTED / UPDATED (resurrected) / SKIPPED_blacklist /
    SKIPPED_not_enough_content / SKIPPED_junk / SKIPPED_archived.

    Each fixture runs in its OWN db so cross-fixture interactions can't leak;
    the seed block reconstructs the only pre-state each case needs.
    """
    outcomes = {}
    for i, entry in enumerate(_load_fixtures()):
        db = _fresh_db(tmp_path, monkeypatch, f"{'board' if board else 'ats'}_{i}.db")
        try:
            dedup_hash = _seed_entry(db, entry)
            status_before, last_before = _row_state(db, dedup_hash)

            job = dict(entry["job"])
            if board:
                board_cfg = {"name": entry["org"], "url": "https://board.test", "tier": "C"}
                # board path keys org off board name unless org_override given
                job.setdefault("org_override", entry["org"])
                db.save_board_vacancies(board_cfg, [job])
            else:
                db.save_vacancies(entry["org"], "A", [job])
            db.get_conn().commit()

            status_after, last_after = _row_state(db, dedup_hash)
            outcome = _classify_write_outcome(
                entry, status_before, status_after, last_before, last_after
            )
            outcomes[_stable_key(entry)] = outcome
        finally:
            db.close_conn()
    return outcomes


# Reject reason per behaviour class, used only when the merge made NO change to
# the row (absent after, or a pre-existing row left untouched). The reason is a
# property of the input — fixed and verified empirically — so attaching it by
# class keeps the outcome label stable across the refactor.
_SKIP_REASON_BY_CLASS = {
    "blacklist_title": "SKIPPED_blacklist",
    "protected_liked_blacklist": "SKIPPED_blacklist",
    "junk_content": "SKIPPED_junk",
}


def _classify_write_outcome(entry, status_before, status_after, last_before, last_after):
    """Map (before, after) row state to a stable outcome label.

    A merge either INSERTs, UPDATEs (bumps last_seen, maybe resurrects), or
    SKIPs (`continue`, no write). Discriminators:
      * row absent after  → SKIPPED (reason from class; archived-tombstone else)
      * row absent before, present after → INSERTED
      * row present both, last_seen bumped → UPDATED (+resurrection check)
      * row present both, last_seen unchanged → SKIPPED (reason from class)
    """
    if status_after is None:
        # Rejected by a content/blacklist/junk gate, or skipped on an archived
        # tombstone (no pre-existing row to leave behind).
        return _SKIP_REASON_BY_CLASS.get(entry["_class"], "SKIPPED_archived")

    if status_before is None:
        return "INSERTED"

    # Pre-existing row. last_seen bump is the proof an UPDATE actually ran.
    if last_after != last_before:
        if status_before == "archived" and status_after == "unseen":
            return "UPDATED_resurrected"
        return "UPDATED"

    # Pre-existing row left untouched → the merge skipped it.
    return _SKIP_REASON_BY_CLASS.get(entry["_class"], "SKIPPED_archived")


# ===========================================================================
# Entry point 3 — classify_vacancies (read-only /filter triage)
# ===========================================================================


def _run_classify(tmp_path, monkeypatch):
    """Seed ALL fixtures into ONE db (classify is a whole-pool read), then map
    each stable_key to its category.

    classify only sees active, unscored, non-archived rows. So the write path
    runs first to populate the pool exactly as /filter would see it after a
    fetch — except blind/thin/geo rows, which the ATS write path keeps (thin,
    geo) or rejects (blind: not_enough_content). To exercise classify's blind
    branches we insert blind rows directly (a fetcher with a URL but no desc is
    what /filter is meant to triage, but merge drops them on content), matching
    how /filter sees rows that some OTHER fetch inserted with a URL fallback.
    """
    db = _fresh_db(tmp_path, monkeypatch, "classify.db")
    try:
        import filter_vacancies

        importlib.reload(filter_vacancies)
        _freeze_clock(monkeypatch, filter_vacancies)
        monkeypatch.setattr(filter_vacancies, "_BANNED_COUNTRIES", _excluded_countries())
        # The geo gate is inert unless a ban is configured; force it on so the
        # patched country set is honoured under the test's empty profile.
        monkeypatch.setattr(filter_vacancies, "_GEO_ACTIVE", True)

        keys_by_class = {}
        for entry in _load_fixtures():
            dedup_hash = _seed_entry_for_classify(db, entry)
            _apply_first_seen(db, entry, dedup_hash)
            keys_by_class[_stable_key(entry)] = entry["_class"]

        # classify_vacancies() returns the WHOLE pool, decided roles included,
        # because the grouping rules need them as context. What /filter acts on
        # is only_undecided(...) of it — so that is what the golden pins, and
        # a decided role reads ABSENT there exactly as it always has.
        all_cats = filter_vacancies.classify_vacancies()
        cats = filter_vacancies.only_undecided(all_cats)
        key_to_cat = {}
        for cat, items in cats.items():
            for _vid, vac in items:
                key_to_cat[(vac["org"], vac["title"])] = cat
        context_keys = {
            (vac["org"], vac["title"]) for items in all_cats.values() for _vid, vac in items
        }

        outcomes = {}
        for key, cls in keys_by_class.items():
            outcomes[key] = key_to_cat.get(key, "ABSENT")
        return outcomes, context_keys
    finally:
        db.close_conn()


def _seed_entry_for_classify(db, entry):
    """Insert a fixture row directly so classify sees the pool as /filter would.

    classify is downstream of merge; it triages whatever sits unscored in the
    DB. We insert each fixture row directly (bypassing merge's write gates) so
    every behaviour class — including blind rows merge would have dropped — is
    present for classify to categorise. Protected/archived seeds still apply.
    """
    seed = entry.get("_seed") or {}
    org = entry["org"]
    title = entry["job"]["title"]
    db.ensure_company(org, status="active")
    canonical = db.resolve_canonical_name(org)
    dedup_hash = db.make_vacancy_id(canonical, title)
    company_id = db.resolve_company_id(org)

    status = seed.get("existing_status", "unseen")
    today = FROZEN_TODAY.isoformat()
    loc = db._make_location_entry(entry["job"])

    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO vacancy (dedup_hash, company_id, title, snippet, "
        "full_description, first_seen, last_seen, locations, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            dedup_hash,
            company_id,
            title,
            entry["job"].get("snippet", ""),
            entry["job"].get("full_description", ""),
            today,
            today,
            json.dumps([loc]),
            status,
        ),
    )
    reason = seed.get("archived_hash_reason")
    if reason:
        cur.execute(
            "INSERT INTO archived_hash (dedup_hash, reason) VALUES (?, ?)",
            (dedup_hash, reason),
        )
    conn.commit()
    cur.close()
    return dedup_hash


# ===========================================================================
# Entry point 4 — score._load_and_dedup
# ===========================================================================


def _run_score_dedup(tmp_path, monkeypatch):
    """Seed all fixtures, run _load_and_dedup, return (stats, roles_keys).

    Snapshot = score stats {blacklisted, blind, total, candidates,
    roles_available} plus the set of stable_keys that survived into `roles`.
    """
    db = _fresh_db(tmp_path, monkeypatch, "score.db")
    try:
        for entry in _load_fixtures():
            _seed_entry_for_classify(db, entry)

        import score_vacancies

        importlib.reload(score_vacancies)
        roles, _fitness, stats = score_vacancies._load_and_dedup(include_candidates=False)
        roles_keys = {key for key, _rep, _members in roles}
        return stats, roles_keys
    finally:
        db.close_conn()


# ===========================================================================
# GOLDEN — captured from current code; every class verified to actually fire.
# ===========================================================================

GOLDEN_MERGE_ATS = {
    ("Aurora Labs", "Backend Engineer"): "INSERTED",
    ("Aurora Labs", "Talent Pool — General Application"): "SKIPPED_blacklist",
    ("Globex Foundation", "Program Officer"): "SKIPPED_junk",
    ("Globex Foundation", "Field Coordinator"): "INSERTED",
    ("Initech", "Data Analyst"): "INSERTED",
    ("Initech", "Product Manager"): "INSERTED",
    ("Initech", "UX Designer"): "INSERTED",
    ("Umbrella Corp", "Operations Lead"): "SKIPPED_archived",
    ("Hooli", "Site Reliability Engineer"): "UPDATED_resurrected",
    # A blacklisted title we ALREADY track (liked) that the source still lists is
    # NOT imported (gate holds) but its last_seen IS refreshed — otherwise a live
    # role drifts stale and shows as "expired" in triage (last_seen bump = UPDATE).
    ("Pied Piper", "Talent Pool — General Application"): "UPDATED",
}

GOLDEN_MERGE_BOARD = {
    ("Aurora Labs", "Backend Engineer"): "INSERTED",
    ("Aurora Labs", "Talent Pool — General Application"): "SKIPPED_blacklist",
    ("Globex Foundation", "Program Officer"): "SKIPPED_junk",
    ("Globex Foundation", "Field Coordinator"): "INSERTED",
    ("Initech", "Data Analyst"): "INSERTED",
    ("Initech", "Product Manager"): "INSERTED",
    ("Initech", "UX Designer"): "INSERTED",
    ("Umbrella Corp", "Operations Lead"): "SKIPPED_archived",
    ("Hooli", "Site Reliability Engineer"): "SKIPPED_archived",
    # Same as the ATS path: a still-listed blacklisted role we already track has
    # its last_seen refreshed rather than left to age into a false "expired".
    ("Pied Piper", "Talent Pool — General Application"): "UPDATED",
}

GOLDEN_CLASSIFY = {
    ("Aurora Labs", "Backend Engineer"): "ready",
    ("Aurora Labs", "Talent Pool — General Application"): "delete_blacklist",
    ("Globex Foundation", "Program Officer"): "delete_junk",
    ("Globex Foundation", "Field Coordinator"): "delete_geo",
    ("Initech", "Data Analyst"): "delete_stale_blind",
    ("Initech", "Product Manager"): "reenrich_blind",
    ("Initech", "UX Designer"): "reenrich_thin",
    ("Umbrella Corp", "Operations Lead"): "delete_rearchived",
    ("Hooli", "Site Reliability Engineer"): "ABSENT",
    ("Pied Piper", "Talent Pool — General Application"): "ABSENT",
}

GOLDEN_SCORE_STATS = {
    "blacklisted": 2,
    "company_title_filtered": 0,
    "blind": 2,
    "total": 9,
    "candidates": 0,
    "roles_available": 5,
}

GOLDEN_SCORE_ROLES = {
    ("Aurora Labs", "Backend Engineer"),
    ("Globex Foundation", "Program Officer"),
    ("Globex Foundation", "Field Coordinator"),
    ("Initech", "UX Designer"),
    ("Umbrella Corp", "Operations Lead"),
}


# ===========================================================================
# TESTS
# ===========================================================================


def test_merge_vacancies_snapshot(tmp_path, monkeypatch):
    """Entry point 1: direct-ATS write path outcomes are pinned."""
    outcomes = _run_write_path(tmp_path, monkeypatch, board=False)
    assert outcomes == GOLDEN_MERGE_ATS


def test_merge_board_vacancies_snapshot(tmp_path, monkeypatch):
    """Entry point 2: job-board write path outcomes are pinned.

    Same gates as the ATS path, but resurrection is BLOCKED (include_gone=True),
    so the SRE case skips-archived instead of resurrecting — the one cell that
    differs from the ATS snapshot, by design.
    """
    outcomes = _run_write_path(tmp_path, monkeypatch, board=True)
    assert outcomes == GOLDEN_MERGE_BOARD


def test_classify_vacancies_snapshot(tmp_path, monkeypatch):
    """Entry point 3: /filter triage categories are pinned."""
    outcomes, _ = _run_classify(tmp_path, monkeypatch)
    assert outcomes == GOLDEN_CLASSIFY


def test_a_liked_role_is_context_for_the_grouping_but_never_work(tmp_path, monkeypatch):
    """The `protected_liked_blacklist` fixture: a role Nikita liked, with a
    blacklisted title. /filter must never act on it — and the grouping rules
    must still see it, so that its siblings are judged against where the role
    actually exists."""
    outcomes, context_keys = _run_classify(tmp_path, monkeypatch)
    key = ("Pied Piper", "Talent Pool — General Application")
    assert outcomes[key] == "ABSENT"  # never work
    assert key in context_keys  # always context


def test_score_load_and_dedup_snapshot(tmp_path, monkeypatch):
    """Entry point 4: pre-score dedup stats + surviving roles are pinned."""
    stats, roles_keys = _run_score_dedup(tmp_path, monkeypatch)
    assert stats == GOLDEN_SCORE_STATS
    assert roles_keys == GOLDEN_SCORE_ROLES


def test_backend_is_sqlite(tmp_path, monkeypatch):
    """Guard: the harness never touches a real Postgres."""
    _force_sqlite(monkeypatch, tmp_path / "guard.db")
    import db_backend

    assert db_backend.IS_SQLITE
