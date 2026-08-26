"""Tests for resolve_canonical_name and the company registry machinery.

These exercise the alias-resolution LOGIC, not any specific company. The
registry they resolve against is INJECTED (made-up placeholder names), so they
encode no particular company list and, unlike the versions they replace, they
still test something when there is no database: those skipped whenever the
registry was empty, which is every run without a DB — including CI.
"""

import pytest
import company_registry
from config import resolve_canonical_name


@pytest.fixture()
def registry(monkeypatch):
    """A two-company registry, injected the way the loader would have built it.

    resolve_canonical_name reads these module globals at call time, so patching
    them exercises the real resolution order against known data."""
    companies = {
        "Northwind Aid Trust": {"strategy": "greenhouse", "status": "active", "slug": "northwind"},
        "Fictive Robotics Guild": {"strategy": "lever", "status": "active", "slug": "frg"},
    }
    monkeypatch.setattr(company_registry, "COMPANIES", companies)
    monkeypatch.setattr(company_registry, "_COMPANIES_LOWER", {k.lower(): k for k in companies})
    monkeypatch.setattr(company_registry, "_ALIAS_INDEX", {"northwind": "Northwind Aid Trust"})
    return companies


# ---------------------------------------------------------------------------
# resolve_canonical_name — generic machinery
# ---------------------------------------------------------------------------


def test_CN01_exact_companies_key_returned_as_is(registry):
    """An exact registry key resolves to itself."""
    for key in registry:
        assert resolve_canonical_name(key) == key


def test_CN02_case_insensitive_companies_match(registry):
    """Resolution is case-insensitive for an existing registry key."""
    for key in registry:
        assert resolve_canonical_name(key.lower()) == key
        assert resolve_canonical_name(key.upper()) == key


def test_CN03_alias_resolves_to_its_canonical_name(registry):
    """An alias (any case) resolves; the whitespace normalizer runs first."""
    assert resolve_canonical_name("Northwind") == "Northwind Aid Trust"
    assert resolve_canonical_name("  northwind  ") == "Northwind Aid Trust"


def test_CN06_unknown_name_returned_unchanged():
    """An unknown name passes through unchanged (no false match)."""
    name = "SomeCompletelyUnknownOrg_XYZ_12345"
    assert resolve_canonical_name(name) == name


def test_CN08_loader_gives_every_entry_a_strategy(monkeypatch):
    """The loader's row -> config mapping, driven by a fake company table.

    Every monitored entry must carry 'strategy' (the fetch dispatcher keys on
    it), a scrape strategy must get its 'url', and a nonsense tier is dropped
    rather than passed through."""
    rows = [
        ("Northwind Aid Trust", "greenhouse", "active", "A", "", "northwind", None, None),
        (
            "Fictive Robotics Guild",
            "firecrawl_scrape",
            "active",
            "not-a-tier",
            "https://frg.test/jobs",
            None,
            {"depth": 2},
            None,
        ),
    ]

    class _Cursor:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

    monkeypatch.setattr(company_registry, "get_conn", lambda: _Conn())
    built = company_registry._build_companies_from_db()

    assert set(built) == {"Northwind Aid Trust", "Fictive Robotics Guild"}
    for name, cfg in built.items():
        assert "strategy" in cfg, f"{name} missing 'strategy' key"
    assert built["Northwind Aid Trust"]["tier"] == "A"
    assert built["Fictive Robotics Guild"]["tier"] is None
    assert built["Fictive Robotics Guild"]["url"] == "https://frg.test/jobs"
    assert built["Fictive Robotics Guild"]["depth"] == 2
    assert company_registry.registry_load_failed() is False


def test_CN09_all_known_names_covers_every_company_row(monkeypatch):
    """`config._ALL_CSV_NAMES` is the backward-compat name for the registry's
    _ALL_KNOWN_NAMES: EVERY company row, including the inactive and candidate
    ones the monitored registry leaves out.

    (The assertion this replaces compared two lengths that are both 0 without a
    database, so it passed on nothing.)"""
    from config import _ALL_CSV_NAMES

    assert isinstance(_ALL_CSV_NAMES, set)

    rows = [("Northwind Aid Trust",), ("Fictive Robotics Guild",), ("Dormant Trust",)]

    class _Cursor:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

    monkeypatch.setattr(company_registry, "get_conn", lambda: _Conn())
    assert company_registry._load_all_known_names() == {name for (name,) in rows}


# ---------------------------------------------------------------------------
# Job boards are opt-in
# ---------------------------------------------------------------------------


def test_boards_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JOB_BOARDS", raising=False)
    import config as cfg

    assert cfg._select_enabled_boards() == {}


def test_boards_enable_subset_via_env(monkeypatch):
    monkeypatch.setenv("JOB_BOARDS", "reliefweb")
    import config as cfg

    enabled = cfg._select_enabled_boards()
    assert set(enabled) == {"reliefweb"}


def test_boards_enable_all_via_env(monkeypatch):
    monkeypatch.setenv("JOB_BOARDS", "all")
    import config as cfg

    enabled = cfg._select_enabled_boards()
    assert set(enabled) == set(cfg._ALL_JOB_BOARDS)


def test_boards_unknown_id_ignored(monkeypatch):
    monkeypatch.setenv("JOB_BOARDS", "80k_hours,does_not_exist")
    import config as cfg

    enabled = cfg._select_enabled_boards()
    assert set(enabled) == {"80k_hours"}
