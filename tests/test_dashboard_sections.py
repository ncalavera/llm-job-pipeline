"""Generator coverage for the dashboard's Boards / Settings / Today additions.

The six-section dashboard restructure adds three baked payload surfaces so the
Boards, Settings and Today sections render from the static data.js (simple mode)
with no live API:

  * ``prepare_boards_catalog``   — the [boards.*] catalogue + enabled state.
  * ``prepare_settings_payload`` — RESOLVED dials (volume / scoring / thresholds)
    with a neutral "where to change" pointer per row. Values only, never prose.
  * ``prepare_learning_hint``    — a tiny "verdicts pending" flag for Today.

Plus the privacy invariant that the application projection strips artifact VALUES
and free-text notes, keeping only the display shape (status/channel/date + keys).

Runs fully offline on the SQLite backend against an isolated empty temp DB.
"""

import importlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = str(REPO / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def _force_sqlite(monkeypatch, db_file: Path):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "report",
        "settings",
        "scoring_settings",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "section-payload test must run on SQLite"


# ---------------------------------------------------------------------------
# Boards catalogue
# ---------------------------------------------------------------------------


def test_boards_catalog_has_every_configured_board(tmp_path, monkeypatch):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    import settings
    from report.data_prep import prepare_boards_catalog

    catalog = prepare_boards_catalog()
    assert catalog, "catalogue must not be empty"
    assert len(catalog) == len(settings.boards())

    ids = [b["id"] for b in catalog]
    assert ids == sorted(ids), "boards must be sorted by id for a stable diff"

    for b in catalog:
        assert set(b) >= {
            "id",
            "name",
            "audience",
            "strategy",
            "tier",
            "ttl_days",
            "url",
            "enabled",
            "last_fetched",
        }
        assert isinstance(b["audience"], str)
        assert isinstance(b["enabled"], bool)


def test_boards_catalog_enabled_state_degrades_on_empty_db(tmp_path, monkeypatch):
    """No board table yet (fork on an old schema / empty DB): every board reads
    as disabled with no last_fetched, never a crash."""
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import prepare_boards_catalog

    catalog = prepare_boards_catalog()
    assert all(b["enabled"] is False for b in catalog)
    assert all(b["last_fetched"] == "" for b in catalog)


# ---------------------------------------------------------------------------
# Settings payload — RESOLVED values only, neutral source pointers
# ---------------------------------------------------------------------------


def test_settings_payload_groups_and_shape(tmp_path, monkeypatch):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import prepare_settings_payload

    payload = prepare_settings_payload()
    keys = [g["key"] for g in payload["groups"]]
    assert keys == [
        "settings_grp_product",
        "settings_grp_volume",
        "settings_grp_scoring",
        "settings_grp_thresholds",
    ]

    for group in payload["groups"]:
        assert group["rows"], "every settings group has rows"
        for row in group["rows"]:
            assert set(row) == {"key", "value", "source"}
            # The source is a neutral file pointer — never prose, never a secret.
            assert row["source"].startswith("config/"), row["source"]


def test_settings_volume_values_match_defaults(tmp_path, monkeypatch):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    import settings
    from report.data_prep import prepare_settings_payload

    vol = settings.volume()
    rows = {
        r["key"]: r["value"]
        for g in prepare_settings_payload()["groups"]
        if g["key"] == "settings_grp_volume"
        for r in g["rows"]
    }
    assert rows["set_max_active_companies"] == vol["max_active_companies"]
    assert rows["set_daily_scoring_limit"] == vol["daily_scoring_limit"]
    assert rows["set_digest_size"] == vol["digest_size"]


def test_settings_scoring_model_is_a_known_tier(tmp_path, monkeypatch):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import prepare_settings_payload

    rows = {
        r["key"]: r["value"]
        for g in prepare_settings_payload()["groups"]
        if g["key"] == "settings_grp_scoring"
        for r in g["rows"]
    }
    assert rows["set_scoring_model"] in {"haiku", "sonnet", "opus"}
    assert rows["set_screen_model"] in {"haiku", "sonnet", "opus"}


# ---------------------------------------------------------------------------
# Learning hint
# ---------------------------------------------------------------------------


def test_learning_hint_is_none_when_nothing_pending(tmp_path, monkeypatch):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import prepare_learning_hint

    # Empty DB → no verdicts, no garbage, no proposals → no hint.
    assert prepare_learning_hint() is None


# ---------------------------------------------------------------------------
# Application projection privacy — keys only, never values/notes
# ---------------------------------------------------------------------------


def test_project_application_strips_values_and_notes(tmp_path, monkeypatch):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import _project_application

    full = {
        "status": "applied",
        "channel": "email",
        "applied_at": "2026-06-01",
        "artifacts": {"cover_letter": "SECRET PROSE", "cv_version": "v3"},
        "notes": "PRIVATE NOTE",
        "cover_letter_path": "cover.md",
    }
    projected = _project_application(full)
    assert projected["status"] == "applied"
    assert projected["channel"] == "email"
    assert projected["artifact_count"] == 2
    # Only the artifact KEYS survive, each blanked to True.
    assert projected["artifacts"] == {"cover_letter": True, "cv_version": True}
    # Nothing private rides along.
    blob = json.dumps(projected)
    assert "SECRET PROSE" not in blob
    assert "PRIVATE NOTE" not in blob
    assert "cover_letter_path" not in projected
    assert "notes" not in projected


def test_project_application_passthrough_none(tmp_path, monkeypatch):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import _project_application

    assert _project_application(None) is None


def test_build_group_ships_raw_source_board(tmp_path, monkeypatch):
    """Each group carries the RAW source_board (== board.name, "" for direct
    ATS) so the browser can DERIVE per-board yield — the one payload change for
    a raw provenance field, never a pre-computed aggregate."""
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import _build_group

    vac = {
        "id": "v1",
        "org": "Acme",
        "title": "Engineer",
        "llm_score": 72,
        "source_board": "80,000 Hours",
        "locations": [],
    }
    g = _build_group(vac, org_colors={}, company_hq={})
    assert g["source_board"] == "80,000 Hours"

    # A direct-ATS role (no board) ships "" — never null, so the browser's
    # `(g.source_board || "").trim()` join is always a string.
    direct = _build_group(
        {"id": "v2", "org": "Acme", "title": "Nurse", "llm_score": 50, "locations": []},
        org_colors={},
        company_hq={},
    )
    assert direct["source_board"] == ""


# ---------------------------------------------------------------------------
# End-to-end: the baked data.js carries the three new keys
# ---------------------------------------------------------------------------


def test_generated_data_js_carries_section_payloads(tmp_path, monkeypatch):
    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    import config
    import report

    out_dir = tmp_path / "public_out"
    out_dir.mkdir()
    monkeypatch.setattr(config, "PUBLIC_DIR", out_dir, raising=False)
    monkeypatch.setattr(report, "PUBLIC_DIR", out_dir, raising=False)

    report.generate_dashboard()

    text = (out_dir / "data.js").read_text(encoding="utf-8")
    payload = json.loads(text.split("= ", 1)[1].rstrip().rstrip(";"))
    assert isinstance(payload["boards_catalog"], list) and payload["boards_catalog"]
    assert [g["key"] for g in payload["settings"]["groups"]] == [
        "settings_grp_product",
        "settings_grp_volume",
        "settings_grp_scoring",
        "settings_grp_thresholds",
    ]
    # learning is present as a key (None when nothing is pending).
    assert "learning" in payload


# ---------------------------------------------------------------------------
# Screening payload (bulk-screening inbox, U3): raw fields, ready roles always
# shipped, processing counts over tonight's cohort
# ---------------------------------------------------------------------------

_MIGRATIONS = REPO / "sql" / "migrations"
_POSTING = "We are hiring a Programme Manager. Fluent Spanish is required. " * 8
_SCREENING = {
    "posting_facts": {"duties": "Runs programmes.", "requirements": []},
    "profile_comparison": [],
    "unknowns": ["salary"],
}


def _screening_db(tmp_path, monkeypatch):
    import sqlite3

    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    import database_supabase as db

    db.get_conn().commit()  # baseline schema is created on first connect
    raw = sqlite3.connect(tmp_path / "jobsearch.db")
    for name in ("0025_add_vacancy_scoring_excluded_reason", "0027_add_vacancy_screening"):
        raw.executescript((_MIGRATIONS / f"{name}.sqlite.sql").read_text(encoding="utf-8"))
    raw.commit()
    raw.close()
    return db


def _seed_role(db, vac_id, *, score, status, first_seen, state=None, screening=None):
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM company WHERE canonical_name = %s",
        ("Acme",),
    )
    row = cur.fetchone()
    if row:
        cid = row[0]
    else:
        cid = "c-acme"
        cur.execute(
            "INSERT INTO company (id, canonical_name, status) VALUES (%s, %s, 'active')",
            (cid, "Acme"),
        )
    cur.execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, full_description, llm_score, "
        "status, first_seen, last_seen, screening_state, screening) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            vac_id,
            f"h-{vac_id}",
            cid,
            f"Role {vac_id}",
            _POSTING,
            score,
            status,
            first_seen,
            first_seen,
            state,
            json.dumps(screening) if screening is not None else None,
        ),
    )
    conn.commit()
    cur.close()


def _payload(monkeypatch):
    import report.data_prep as data_prep

    return importlib.reload(data_prep).prepare_report_data()


def test_ready_role_ships_at_any_score_and_status(tmp_path, monkeypatch):
    """R6: a prepared role survives the score floor and a Put-aside decision."""
    from datetime import date

    db = _screening_db(tmp_path, monkeypatch)
    today = date.today().isoformat()
    _seed_role(db, "low-unseen", score=12, status="unseen", first_seen=today, state="ready")
    _seed_role(db, "low-passed", score=12, status="passed", first_seen=today, state="ready")
    _seed_role(db, "low-plain", score=12, status="unseen", first_seen=today)

    ids = {g["id"] for g in _payload(monkeypatch)["groups"]}
    assert {"low-unseen", "low-passed"} <= ids
    assert "low-plain" not in ids


def test_screening_json_round_trips_raw(tmp_path, monkeypatch):
    """R5: the stored screening object ships unchanged, with its state and
    prepared_at as raw fields — the browser derives the groups."""
    from datetime import date

    db = _screening_db(tmp_path, monkeypatch)
    _seed_role(
        db,
        "ready",
        score=70,
        status="unseen",
        first_seen=date.today().isoformat(),
        state="ready",
        screening=_SCREENING,
    )
    _seed_role(db, "plain", score=70, status="unseen", first_seen=date.today().isoformat())

    by_id = {g["id"]: g for g in _payload(monkeypatch)["groups"]}
    assert by_id["ready"]["screening"] == _SCREENING
    assert by_id["ready"]["screening_state"] == "ready"
    assert by_id["ready"]["screening_prepared_at"] == ""
    assert by_id["plain"]["screening"] is None
    assert by_id["plain"]["screening_state"] is None


def test_processing_counts_cover_tonights_cohort_only(tmp_path, monkeypatch):
    """R12: unprepared and failed roles inside the 14-day cohort are counted
    (not shipped); an unprepared role older than the window is neither."""
    from datetime import date, timedelta

    db = _screening_db(tmp_path, monkeypatch)
    today = date.today()
    fresh = (today - timedelta(days=2)).isoformat()
    stale = (today - timedelta(days=30)).isoformat()
    _seed_role(db, "unprep-fresh", score=12, status="unseen", first_seen=fresh)
    _seed_role(db, "failed-fresh", score=12, status="unseen", first_seen=fresh, state="failed")
    _seed_role(db, "unprep-stale", score=12, status="unseen", first_seen=stale)

    data = _payload(monkeypatch)
    assert {g["id"] for g in data["groups"]} == set()
    assert data["stats"]["screening_processing"] == {"unprepared": 1, "failed": 1}


def test_screening_prepared_at_datetime_becomes_iso_text():
    """Postgres returns timestamptz as datetime; the snapshot is json.dumps'd
    (2026-09-06 live publish: 'Object of type datetime is not JSON serializable')."""
    from datetime import datetime, timezone
    import json
    import database_supabase as ds

    row = {
        "id": "x",
        "company_id": "c",
        "screening_prepared_at": datetime(2026, 9, 6, 17, 51, tzinfo=timezone.utc),
    }
    vac = ds._row_to_vacancy(dict(row)) if hasattr(ds, "_row_to_vacancy") else None
    assert vac is not None
    json.dumps(vac["screening_prepared_at"])
    assert vac["screening_prepared_at"].startswith("2026-09-06T17:51")
