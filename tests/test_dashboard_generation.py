"""Regression for the dashboard generator (``scripts/report/__init__.py``).

1. ``generate_dashboard()`` must NEVER rewrite the tracked, hand-maintained
   ``public/index.html`` shell. Regenerating it reintroduced stale copy and
   dirtied git on every run. Only ``public/data.js`` may be (re)written.

2. The dashboard style resolver is locked to exactly {illustrated, minimal},
   defaulting to ``illustrated`` and falling back to ``illustrated`` on a bad
   ``DASHBOARD_STYLE``. The shipped ``config/defaults.toml`` default is
   ``illustrated``.

The generator loads from the local SQLite backend: conftest clears
SUPABASE_DB_URL for the session, and each test points JOBSEARCH_DB_PATH at an
isolated temp file so an empty DB is used (no network, no real data).
"""

import hashlib
import importlib
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
    for mod in ("database_supabase", "config", "company_registry",
                "db_conn", "db_backend", "report"):
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
        self._sink["sql"] = sql          # last statement
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
    curr_idx = next(i for i, (s, _) in enumerate(fake.sink["calls"]) if "values ('current'" in s.lower())
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
