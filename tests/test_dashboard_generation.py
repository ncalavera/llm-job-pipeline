"""Regression for the dashboard generator (``scripts/report/__init__.py``).

1. ``generate_dashboard()`` must NEVER rewrite the tracked, hand-maintained
   ``public/index.html`` shell. Regenerating it reintroduced stale copy and
   dirtied git on every run. Only ``public/data.js`` may be (re)written.

2. The dashboard style resolver is locked to exactly {illustrated, minimal},
   defaulting to ``illustrated`` and falling back to ``illustrated`` on a bad
   ``DASHBOARD_STYLE``. The shipped ``config/defaults.toml`` default is
   ``illustrated``.

3. Snapshot persistence (full mode upserts ``dashboard_snapshot``, simple mode
   writes ``data.js``) and the Boards / Settings / Today section payloads
   (``prepare_boards_catalog``, ``prepare_settings_payload``,
   ``prepare_learning_hint``), plus the application-projection privacy
   invariant and the end-to-end baked ``data.js`` shape.

The generator loads from the local SQLite backend: conftest clears
SUPABASE_DB_URL for the session, and each test points JOBSEARCH_DB_PATH at an
isolated temp file so an empty DB is used (no network, no real data).

Absorbed ``tests/test_dashboard_sections.py`` (section-payload coverage).
"""

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = str(REPO / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

INDEX_HTML = REPO / "public" / "index.html"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _force_sqlite(monkeypatch, db_file: Path):
    """Point the whole backend chain at a fresh temp SQLite file and reload it."""
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
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "generator test must run on the SQLite backend"


# ---------------------------------------------------------------------------
# 1. generate_dashboard() does not touch the tracked index.html
# ---------------------------------------------------------------------------


def test_generate_dashboard_leaves_index_html_untouched(tmp_path, monkeypatch):
    assert INDEX_HTML.exists(), "tracked public/index.html must exist"
    before = _sha256(INDEX_HTML)

    _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    import config
    import report

    # Redirect output to a temp dir so the real public/data.js is not clobbered.
    out_dir = tmp_path / "public_out"
    out_dir.mkdir()
    monkeypatch.setattr(config, "PUBLIC_DIR", out_dir, raising=False)
    monkeypatch.setattr(report, "PUBLIC_DIR", out_dir, raising=False)

    report.generate_dashboard()

    # The tracked shell is byte-identical.
    assert _sha256(INDEX_HTML) == before, "generate_dashboard() rewrote index.html"
    # The generator wrote ONLY data.js — never an index.html.
    written = sorted(p.name for p in out_dir.iterdir())
    assert written == ["data.js"], f"generator wrote unexpected files: {written}"


# ---------------------------------------------------------------------------
# 2. dashboard style resolver
# ---------------------------------------------------------------------------


@pytest.fixture()
def resolver(monkeypatch):
    """Import report with a clean SQLite chain and a cleared settings cache."""
    # No DB access needed for the resolver, but keep the env neutral.
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    for mod in ("report", "settings"):
        sys.modules.pop(mod, None)
    import settings

    settings.clear_cache()
    import report

    yield report
    settings.clear_cache()


def test_style_default_is_illustrated(resolver, monkeypatch):
    monkeypatch.delenv("DASHBOARD_STYLE", raising=False)
    assert resolver._resolve_dashboard_style() == "illustrated"


def test_style_env_minimal(resolver, monkeypatch):
    monkeypatch.setenv("DASHBOARD_STYLE", "minimal")
    assert resolver._resolve_dashboard_style() == "minimal"


def test_style_env_garbage_falls_back_to_illustrated(resolver, monkeypatch):
    monkeypatch.setenv("DASHBOARD_STYLE", "garbage")
    assert resolver._resolve_dashboard_style() == "illustrated"


def test_supported_styles_are_exactly_illustrated_and_minimal(resolver):
    assert set(resolver._DASHBOARD_STYLES) == {"illustrated", "minimal"}


def test_defaults_toml_dashboard_style_is_illustrated():
    import settings

    settings.clear_cache()
    cfg = settings.load_defaults().get("dashboard", {})
    assert cfg.get("style") == "illustrated"


# ---------------------------------------------------------------------------
# 3. Snapshot persistence — full mode upserts the dashboard_snapshot row,
#    simple mode writes data.js. (U2)
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, sink):
        self._sink = sink

    def execute(self, sql, params=None):
        self._sink["sql"] = sql  # last statement
        self._sink["params"] = params
        self._sink.setdefault("calls", []).append((sql, params))


class _FakeConn:
    """psycopg2-connection stand-in that records every execute for assertions."""

    def __init__(self):
        self.sink = {"calls": []}
        self.committed = False

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self.sink)

    def commit(self):
        self.committed = True


def _fresh_report(monkeypatch):
    """Reload report (and its db_backend dependency) with a clean SQLite chain."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    for mod in ("database_supabase", "config", "db_conn", "db_backend", "report"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    import report

    return report, db_backend


def test_full_mode_upserts_snapshot_payload_round_trip(monkeypatch):
    """Full mode: the snapshot row's payload is byte/shape-identical to what the
    static data.js path would carry — so the live endpoint and the baked file are
    interchangeable and client-side filtering can't regress."""
    report, db_backend = _fresh_report(monkeypatch)

    fake = _FakeConn()
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(db_backend, "get_conn", lambda: fake)

    payload = {
        "config": {"language": "en"},
        "stats": {"total": 3},
        "vacancy_ids": ["a", "b"],
        "groups": [{"id": "a"}, {"id": "b"}],
        "companies": [],
        "triage_reviews": [],
        "archived_groups": [],
    }
    report._persist_dashboard(payload)

    assert fake.committed, "full-mode upsert must commit"
    sql = fake.sink["sql"].lower()
    assert "insert into dashboard_snapshot" in sql
    assert "on conflict" in sql
    # The 'current' upsert (the last statement) carries the exact payload.
    json_param = fake.sink["params"][0]
    assert json_param.adapted == payload


def test_full_mode_keeps_previous_snapshot_before_overwrite(monkeypatch):
    """Before overwriting 'current', the old payload is copied to 'previous' so a
    bad run can be rolled back — the live snapshot is not a single destructive
    overwrite."""
    report, db_backend = _fresh_report(monkeypatch)
    fake = _FakeConn()
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(db_backend, "get_conn", lambda: fake)

    report._persist_dashboard({"groups": []})

    statements = " | ".join(sql.lower() for sql, _ in fake.sink["calls"])
    assert "'previous'" in statements, "must snapshot the old payload as 'previous'"
    assert "'current'" in statements
    # previous is captured before current is overwritten.
    prev_idx = next(i for i, (s, _) in enumerate(fake.sink["calls"]) if "'previous'" in s.lower())
    curr_idx = next(
        i for i, (s, _) in enumerate(fake.sink["calls"]) if "values ('current'" in s.lower()
    )
    assert prev_idx < curr_idx


def test_full_mode_does_not_write_data_js(monkeypatch, tmp_path):
    """Full mode persists only to the snapshot row — never public/data.js."""
    report, db_backend = _fresh_report(monkeypatch)

    out_dir = tmp_path / "public_out"
    out_dir.mkdir()
    monkeypatch.setattr(report, "PUBLIC_DIR", out_dir, raising=False)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(db_backend, "get_conn", lambda: _FakeConn())

    report._persist_dashboard({"groups": []})

    assert list(out_dir.iterdir()) == [], "full mode must not write data.js"


def test_simple_mode_writes_data_js_and_skips_snapshot(monkeypatch, tmp_path):
    """Simple mode (IS_SQLITE) writes data.js and never touches the DB."""
    report, db_backend = _fresh_report(monkeypatch)
    assert db_backend.IS_SQLITE, "this test must run on the SQLite backend"

    out_dir = tmp_path / "public_out"
    out_dir.mkdir()
    monkeypatch.setattr(report, "PUBLIC_DIR", out_dir, raising=False)

    def _boom():
        raise AssertionError("simple mode must not open a DB connection for persistence")

    monkeypatch.setattr(db_backend, "get_conn", _boom)

    report._persist_dashboard({"groups": [{"id": "x"}]})

    assert [p.name for p in out_dir.iterdir()] == ["data.js"]
    assert "VACANCY_DATA" in (out_dir / "data.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# --- from test_dashboard_sections.py ---
#
# Generator coverage for the dashboard's Boards / Settings / Today additions.
#
# The six-section dashboard restructure adds three baked payload surfaces so
# the Boards, Settings and Today sections render from the static data.js
# (simple mode) with no live API:
#
#   * ``prepare_boards_catalog``   — the [boards.*] catalogue + enabled state.
#   * ``prepare_settings_payload`` — RESOLVED dials (volume / scoring /
#     thresholds) with a neutral "where to change" pointer per row. Values
#     only, never prose.
#   * ``prepare_learning_hint``    — a tiny "verdicts pending" flag for Today.
#
# Plus the privacy invariant that the application projection strips artifact
# VALUES and free-text notes, keeping only the display shape (status/channel/
# date + keys).
#
# Runs fully offline on the SQLite backend against an isolated empty temp DB.
# ---------------------------------------------------------------------------


def _force_sqlite_sections(monkeypatch, db_file: Path):
    """Same shape as ``_force_sqlite`` above, but additionally evicts the
    ``settings``/``scoring_settings`` modules that the section-payload tests
    reload (kept distinct: the two helpers differ, per the merge rules)."""
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
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
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
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import prepare_boards_catalog

    catalog = prepare_boards_catalog()
    assert all(b["enabled"] is False for b in catalog)
    assert all(b["last_fetched"] == "" for b in catalog)


# ---------------------------------------------------------------------------
# Settings payload — RESOLVED values only, neutral source pointers
# ---------------------------------------------------------------------------


def test_settings_payload_groups_and_shape(tmp_path, monkeypatch):
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
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
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
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
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
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
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import prepare_learning_hint

    # Empty DB → no verdicts, no garbage, no proposals → no hint.
    assert prepare_learning_hint() is None


# ---------------------------------------------------------------------------
# Application projection privacy — keys only, never values/notes
# ---------------------------------------------------------------------------


def test_project_application_strips_values_and_notes(tmp_path, monkeypatch):
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
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
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
    from report.data_prep import _project_application

    assert _project_application(None) is None


def test_build_group_ships_raw_source_board(tmp_path, monkeypatch):
    """Each group carries the RAW source_board (== board.name, "" for direct
    ATS) so the browser can DERIVE per-board yield — the one payload change for
    a raw provenance field, never a pre-computed aggregate."""
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
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
    _force_sqlite_sections(monkeypatch, tmp_path / "jobsearch.db")
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
