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
