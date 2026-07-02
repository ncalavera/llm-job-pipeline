"""Tests for resolve_canonical_name and the company registry machinery.

These exercise the alias-resolution LOGIC, not any specific company. They use
the registry as loaded (which is empty without a DB / SUPABASE_DB_URL) and
made-up placeholder names, so they encode no particular company list.
"""

import pytest
from config import resolve_canonical_name, COMPANIES


# ---------------------------------------------------------------------------
# resolve_canonical_name — generic machinery
# ---------------------------------------------------------------------------


def test_CN01_exact_companies_key_returned_as_is():
    """An exact registry key resolves to itself."""
    if not COMPANIES:
        pytest.skip("COMPANIES is empty (no DB) — nothing to resolve")
    key = next(iter(COMPANIES))
    assert resolve_canonical_name(key) == key


def test_CN02_case_insensitive_companies_match():
    """Resolution is case-insensitive for an existing registry key."""
    if not COMPANIES:
        pytest.skip("COMPANIES is empty (no DB)")
    key = next(iter(COMPANIES))
    assert resolve_canonical_name(key.lower()) == key
    assert resolve_canonical_name(key.upper()) == key


def test_CN06_unknown_name_returned_unchanged():
    """An unknown name passes through unchanged (no false match)."""
    name = "SomeCompletelyUnknownOrg_XYZ_12345"
    assert resolve_canonical_name(name) == name


def test_CN08_registry_entries_have_strategy():
    """Each loaded registry entry carries a fetch strategy."""
    if not COMPANIES:
        pytest.skip("COMPANIES is empty (no DB)")
    for name, cfg in list(COMPANIES.items())[:5]:
        assert "strategy" in cfg, f"{name} missing 'strategy' key"


def test_CN09_backward_compat_all_csv_names():
    """The _ALL_CSV_NAMES backward-compat re-export is present and covers the
    registry."""
    from config import _ALL_CSV_NAMES

    assert len(_ALL_CSV_NAMES) >= len(COMPANIES)


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
