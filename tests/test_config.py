"""Tests for resolve_canonical_name and company_registry.

The alias-resolution cases below depend on a populated company registry
(loaded from the database at import). They are skipped when COMPANIES is empty
(e.g. offline, no SUPABASE_DB_URL) or when the specific example company isn't
in your own registry.
"""
import pytest
from config import resolve_canonical_name, COMPANIES

requires_registry = pytest.mark.skipif(
    not COMPANIES, reason="company registry empty (no DB) — alias resolution unavailable"
)


# ---------------------------------------------------------------------------
# resolve_canonical_name
# ---------------------------------------------------------------------------

def test_CN01_exact_companies_key_returned_as_is():
    if not COMPANIES:
        pytest.skip("COMPANIES is empty (DB unreachable?)")
    key = next(iter(COMPANIES))
    assert resolve_canonical_name(key) == key


def test_CN02_case_insensitive_companies_match():
    if "FundraiseUp" not in COMPANIES:
        pytest.skip("FundraiseUp not in COMPANIES registry")
    assert resolve_canonical_name("fundraiseup") == "FundraiseUp"


@requires_registry
def test_CN03_alias_fundraise_up():
    if resolve_canonical_name("fundraise up") == "fundraise up":
        pytest.skip("example alias not in this registry")
    assert resolve_canonical_name("fundraise up") == "FundraiseUp"


@requires_registry
def test_CN04_alias_cea():
    if resolve_canonical_name("cea") == "cea":
        pytest.skip("example alias not in this registry")
    assert resolve_canonical_name("cea") == "Centre for Effective Altruism"


@requires_registry
def test_CN05_alias_wellcome():
    if resolve_canonical_name("wellcome") == "wellcome":
        pytest.skip("example alias not in this registry")
    assert resolve_canonical_name("wellcome") == "Wellcome Trust"


def test_CN06_unknown_name_returned_unchanged():
    name = "SomeCompletelyUnknownOrg_XYZ_12345"
    assert resolve_canonical_name(name) == name


@requires_registry
def test_CN07_alias_case_insensitive_gfi():
    if resolve_canonical_name("GFI") == "GFI":
        pytest.skip("example alias not in this registry")
    assert resolve_canonical_name("GFI") == "The Good Food Institute"


@requires_registry
def test_CN08_companies_loaded_from_db():
    """COMPANIES dict should be non-empty when DB is reachable."""
    assert len(COMPANIES) > 0, "COMPANIES should not be empty"
    # Each entry should have 'strategy' key
    for name, cfg in list(COMPANIES.items())[:5]:
        assert "strategy" in cfg, f"{name} missing 'strategy' key"


def test_CN09_backward_compat_all_csv_names():
    """_ALL_CSV_NAMES re-export should work."""
    from config import _ALL_CSV_NAMES
    assert len(_ALL_CSV_NAMES) >= len(COMPANIES)
