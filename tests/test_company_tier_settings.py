"""calculate_company_tier must read its weights/cutoffs from defaults.toml.

The bug: the composite weights (0.70/0.30) and tier cutoffs (65/50/35) were
hardcoded in scripts/database_supabase.py, so editing config/defaults.toml never
moved a single tier. These tests point the loader at a temp defaults.toml and
prove that changing the TOML changes the computed tier + composite.
"""

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import settings  # noqa: E402


def _reload_db(monkeypatch, toml_path: Path):
    """Point settings at toml_path and return a fresh database_supabase."""
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(toml_path))
    settings.clear_cache()
    import database_supabase

    importlib.reload(database_supabase)
    return database_supabase


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    monkeypatch.delenv("DEFAULTS_TOML_PATH", raising=False)
    settings.clear_cache()
    import database_supabase

    importlib.reload(database_supabase)


def _write_toml(path: Path, *, s, a, b, alpha, beta):
    path.write_text(
        textwrap.dedent(f"""
        [thresholds]
        tier_s = {s}
        tier_a = {a}
        tier_b = {b}
        tier_c = 0
        composite_alignment_weight = {alpha}
        composite_boost_weight = {beta}
    """).strip()
        + "\n",
        encoding="utf-8",
    )


def test_default_cutoffs_match_shipped(monkeypatch, tmp_path):
    """Sanity: with shipped numbers, alignment 64 → A, 65 → S."""
    toml = tmp_path / "defaults.toml"
    _write_toml(toml, s=65, a=50, b=35, alpha=0.70, beta=0.30)
    db = _reload_db(monkeypatch, toml)
    assert db.calculate_company_tier(64) == ("A", 64.0)
    assert db.calculate_company_tier(65) == ("S", 65.0)
    assert db.calculate_company_tier(49) == ("B", 49.0)
    assert db.calculate_company_tier(34) == ("C", 34.0)


def test_changing_cutoffs_moves_tier(monkeypatch, tmp_path):
    """Lower the S cutoff to 60 → an alignment of 62 becomes S (was A)."""
    toml = tmp_path / "defaults.toml"
    _write_toml(toml, s=60, a=45, b=30, alpha=0.70, beta=0.30)
    db = _reload_db(monkeypatch, toml)
    assert db.calculate_company_tier(62) == ("S", 62.0)  # 62 >= 60 now
    assert db.calculate_company_tier(46) == ("A", 46.0)  # 46 >= 45 now
    assert db.calculate_company_tier(31) == ("B", 31.0)  # 31 >= 30 now


def test_changing_weights_moves_composite(monkeypatch, tmp_path):
    """Custom boost mixing uses the TOML weights, not hardcoded 0.70/0.30."""
    toml = tmp_path / "defaults.toml"
    # Flip the emphasis hard onto the boost: 0.10 alignment + 0.90 boost.
    _write_toml(toml, s=65, a=50, b=35, alpha=0.10, beta=0.90)
    db = _reload_db(monkeypatch, toml)
    # alignment 40, boost 80 → 0.10*40 + 0.90*80 = 4 + 72 = 76.0 → S
    tier, composite = db.calculate_company_tier(40, custom_boost=80)
    assert composite == 76.0
    assert tier == "S"
    # With the shipped 0.70/0.30 this would be 0.70*40 + 0.30*80 = 52.0 → A.


def test_none_alignment_returns_none(monkeypatch, tmp_path):
    toml = tmp_path / "defaults.toml"
    _write_toml(toml, s=65, a=50, b=35, alpha=0.70, beta=0.30)
    db = _reload_db(monkeypatch, toml)
    assert db.calculate_company_tier(None) == (None, None)
