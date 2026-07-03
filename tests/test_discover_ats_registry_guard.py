"""Guard: discover_ats can never auto-assign a strategy with no fetcher.

THE BLOCKER CLASS. ``discover_ats.py`` recognizes ATSes by URL/HTML pattern
(``ATS_PATTERNS``) and, on ``--apply``, writes the recognized strategy onto the
company row. Several recognized strategies (personio, occupop, applied, breezy,
jazzhr) had NO registered fetcher, so any company auto-assigned one hit
``error: no fetcher registered for strategy '<X>'`` in fetch_vacancies forever
(ARIA sat dead on ``pinpoint`` this way until a fetcher was added).

Two invariants keep the class closed:

1. Every strategy discover_ats can auto-ASSIGN (every ATS_PATTERNS /
   HTML_ONLY_PATTERNS target, routed through ``resolve_assignable_strategy``)
   resolves to a registered COMPANY_FETCHERS strategy.
2. Every strategy in ``WORKING_ATS_STRATEGIES`` (the never-downgrade guard) is
   registered — an unfetchable "working" strategy would be protected from ever
   being fixed.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import discover_ats  # noqa: E402
from discover_ats import (  # noqa: E402
    ATS_PATTERNS,
    HTML_ONLY_PATTERNS,
    WORKING_ATS_STRATEGIES,
    resolve_assignable_strategy,
)
from fetchers.registry import BOARD_FETCHERS, COMPANY_FETCHERS  # noqa: E402

# ATSes recognized by a pattern but with no company fetcher — must fall back.
UNFETCHABLE_ATS = ["personio", "occupop", "applied", "breezy", "jazzhr"]


def _registry() -> set:
    return set(COMPANY_FETCHERS) | set(BOARD_FETCHERS)


def _pattern_targets() -> set:
    return {strategy for _, strategy in ATS_PATTERNS} | {
        strategy for _, strategy, _ in HTML_ONLY_PATTERNS
    }


# ---------------------------------------------------------------------------
# Invariant 1 — nothing auto-assignable is unregistered
# ---------------------------------------------------------------------------


def test_fallback_target_is_registered():
    """The whole safety net rests on firecrawl_scrape existing as a fetcher."""
    assert "firecrawl_scrape" in COMPANY_FETCHERS


def test_every_pattern_target_resolves_to_a_registered_strategy():
    """After ``resolve_assignable_strategy``, every ATS discover_ats can assign
    maps to a registered COMPANY_FETCHERS strategy — so ``--apply`` can never
    write a strategy that fetch_vacancies has no fetcher for."""
    unregistered = {
        strategy: resolve_assignable_strategy(strategy)
        for strategy in _pattern_targets()
        if resolve_assignable_strategy(strategy) not in COMPANY_FETCHERS
    }
    assert not unregistered, (
        "discover_ats can auto-assign strateg(ies) with no fetcher:\n  "
        + "\n  ".join(f"{k} -> {v}" for k, v in sorted(unregistered.items()))
    )


def test_unfetchable_ats_fall_back_to_firecrawl_scrape():
    for ats in UNFETCHABLE_ATS:
        assert resolve_assignable_strategy(ats) == "firecrawl_scrape", ats


def test_registered_ats_resolve_to_themselves():
    for ats in ("greenhouse", "ashby", "recruitee", "pinpoint", "smartrecruiters"):
        assert resolve_assignable_strategy(ats) == ats, ats


# ---------------------------------------------------------------------------
# Invariant 2 — the never-downgrade set is honest
# ---------------------------------------------------------------------------


def test_working_strategies_are_all_registered():
    missing = WORKING_ATS_STRATEGIES - _registry()
    assert not missing, (
        "WORKING_ATS_STRATEGIES lists strateg(ies) with no registered fetcher — "
        "the never-downgrade guard would protect a dead strategy:\n  "
        + "\n  ".join(sorted(missing))
    )


def test_removed_strategies_are_gone_from_working_set():
    """The unfetchable ATSes (and the orphan bare 'rss') must NOT sit in the
    never-downgrade guard anymore."""
    for strategy in [*UNFETCHABLE_ATS, "rss"]:
        assert strategy not in WORKING_ATS_STRATEGIES, strategy


# ---------------------------------------------------------------------------
# Recognition is preserved — only assignment changed
# ---------------------------------------------------------------------------


def test_unfetchable_ats_are_still_recognized():
    """Discovery should still DETECT these ATSes (so results/logs name them);
    only the assigned strategy is downgraded to firecrawl_scrape."""
    targets = _pattern_targets()
    for ats in UNFETCHABLE_ATS:
        assert ats in targets, ats


def test_detect_maps_personio_url_but_assignment_falls_back():
    hits = discover_ats.detect_ats_in_urls(["https://acme.jobs.personio.de/"])
    assert hits and hits[0]["ats"] == "personio"  # still recognized
    assert resolve_assignable_strategy(hits[0]["ats"]) == "firecrawl_scrape"  # safe to assign
