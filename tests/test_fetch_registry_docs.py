"""The fetcher registry as a contract.

Guards that a recognised ATS is always assignable to a registered fetcher,
and that the generated docs/catalogue tables never drift from the live
registry. Absorbed test_discover_ats_registry_guard.py,
test_fetch_engines_doc.py, test_board_catalogue_matches_config.py.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import discover_ats  # noqa: E402
import gen_board_table  # noqa: E402
from discover_ats import (  # noqa: E402
    ATS_PATTERNS,
    HTML_ONLY_PATTERNS,
    WORKING_ATS_STRATEGIES,
    resolve_assignable_strategy,
)
from fetchers.registry import BOARD_FETCHERS, COMPANY_FETCHERS  # noqa: E402


# ---------------------------------------------------------------------------
# --- from test_discover_ats_registry_guard.py ---
# Guard: discover_ats can never auto-assign a strategy with no fetcher.
#
# THE BLOCKER CLASS. ``discover_ats.py`` recognizes ATSes by URL/HTML pattern
# (``ATS_PATTERNS``) and, on ``--apply``, writes the recognized strategy onto the
# company row. Several recognized strategies (personio, occupop, applied, breezy,
# jazzhr) had NO registered fetcher, so any company auto-assigned one hit
# ``error: no fetcher registered for strategy '<X>'`` in fetch_vacancies forever
# (ARIA sat dead on ``pinpoint`` this way until a fetcher was added).
#
# Two invariants keep the class closed:
#
# 1. Every strategy discover_ats can auto-ASSIGN (every ATS_PATTERNS /
#    HTML_ONLY_PATTERNS target, routed through ``resolve_assignable_strategy``)
#    resolves to a registered COMPANY_FETCHERS strategy.
# 2. Every strategy in ``WORKING_ATS_STRATEGIES`` (the never-downgrade guard) is
#    registered — an unfetchable "working" strategy would be protected from ever
#    being fixed.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# --- from test_fetch_engines_doc.py ---
# Guard: every registered fetch strategy has a section in docs/fetch-engines.md.
#
# The reference page must document EVERY strategy in `COMPANY_FETCHERS` /
# `BOARD_FETCHERS` — a registered engine with no section is a doc claim that
# doesn't match code, i.e. a crash-severity bug (STRATEGY guardrail 4). New/renamed
# strategies must fail CI here instead of silently going undocumented, mirroring
# `tests/test_board_catalogue_matches_config.py`.
#
# Sections are anchored mechanically by a per-engine HTML marker
# `<!-- ENGINE: <strategy> -->`, so the guard keys off the strategy string, not on
# prose that merely mentions a provider name. It is bidirectional: a marker for an
# unregistered strategy (a stale/orphan section) also fails.
# ---------------------------------------------------------------------------

DOC = REPO / "docs" / "fetch-engines.md"

# `<!-- ENGINE: greenhouse -->` — one per documented engine section.
_MARKER = re.compile(r"<!--\s*ENGINE:\s*([A-Za-z0-9_]+)\s*-->")


def _documented_strategies() -> list[str]:
    return _MARKER.findall(DOC.read_text(encoding="utf-8"))


def _registered_strategies() -> set[str]:
    return set(COMPANY_FETCHERS) | set(BOARD_FETCHERS)


def test_doc_exists():
    assert DOC.exists(), "docs/fetch-engines.md is expected to exist"


def test_every_registered_strategy_has_a_section():
    documented = set(_documented_strategies())
    registered = _registered_strategies()
    missing = registered - documented
    assert not missing, (
        "Registered fetch strateg(ies) with no section in docs/fetch-engines.md — "
        "add a `<!-- ENGINE: <strategy> -->` section for each:\n  " + "\n  ".join(sorted(missing))
    )


def test_no_orphan_sections():
    documented = set(_documented_strategies())
    registered = _registered_strategies()
    orphans = documented - registered
    assert not orphans, (
        "docs/fetch-engines.md documents strateg(ies) that are NOT registered in "
        "COMPANY_FETCHERS/BOARD_FETCHERS — remove the stale section(s) or fix the "
        "marker:\n  " + "\n  ".join(sorted(orphans))
    )


def test_markers_are_unique():
    """Exactly one section per strategy — a duplicated marker means a copy-paste."""
    documented = _documented_strategies()
    dupes = sorted({s for s in documented if documented.count(s) > 1})
    assert not dupes, f"Duplicate <!-- ENGINE: --> marker(s) in fetch-engines.md: {dupes}"


def test_stated_counts_match_the_registries():
    """The '(N)' in the two section headers equals the live registry sizes, so the
    prose count can never drift from what the pipeline actually registers."""
    text = DOC.read_text(encoding="utf-8")
    assert f"COMPANY_FETCHERS`, {len(COMPANY_FETCHERS)})" in text, (
        f"Company engine count in fetch-engines.md != {len(COMPANY_FETCHERS)} registered"
    )
    assert f"BOARD_FETCHERS`, {len(BOARD_FETCHERS)})" in text, (
        f"Board engine count in fetch-engines.md != {len(BOARD_FETCHERS)} registered"
    )


# --- self-test: the marker regex must actually catch a strategy string --------


def test_marker_regex_extracts_strategy():
    assert _MARKER.findall("<!-- ENGINE: greenhouse -->") == ["greenhouse"]
    assert _MARKER.findall("<!--ENGINE:linkedin_guest-->") == ["linkedin_guest"]
    assert _MARKER.findall("no marker here") == []


# ---------------------------------------------------------------------------
# --- from test_board_catalogue_matches_config.py ---
# Guard: the committed board catalogue matches config — one board count, not three.
#
# Three docs used to state three different board counts. The fix made
# `docs/job-boards-catalogue.md` the single source: its board table is generated
# from `config._ALL_JOB_BOARDS` (== `settings.boards()`, a pure `defaults.toml`
# read) by `scripts/gen_board_table.py`. This test regenerates the table and
# asserts the committed block is byte-identical — so a new/renamed `[boards.*]`
# block that isn't re-rendered fails CI instead of silently drifting.
#
# Regenerate after a board change: `python3 scripts/gen_board_table.py --write`.
# ---------------------------------------------------------------------------


def test_catalogue_table_matches_config():
    committed = gen_board_table.committed_block()
    rendered = gen_board_table.render_table()
    assert committed == rendered, (
        "docs/job-boards-catalogue.md is out of sync with config/defaults.toml "
        "[boards.*] — run `python3 scripts/gen_board_table.py --write` and commit."
    )


def test_generator_source_is_all_job_boards():
    """The renderer must read the same set config exposes as _ALL_JOB_BOARDS, so
    the doc count can never diverge from what the pipeline actually loads."""
    import settings

    assert gen_board_table._boards() == settings.boards()


def test_every_board_row_is_present_and_stated_count_matches():
    """The stated count in the block equals the number of table rows equals the
    number of configured boards."""
    import settings

    boards = settings.boards()
    block = gen_board_table.render_table()
    # One `| \`id\` |` row per board.
    rows = [ln for ln in block.splitlines() if ln.startswith("| `")]
    assert len(rows) == len(boards)
    assert f"**{len(boards)} built-in job boards**" in block
    for board_id in boards:
        assert f"| `{board_id}` |" in block, f"{board_id} missing from the table"


# --- docs/index.html onboarding board picker (BOARD_CATALOG JS block) -------


def test_no_generated_doc_is_stale():
    """BOTH generated docs (the .md table AND the docs/index.html board picker)
    must match config — a new/renamed [boards.*] block that wasn't regenerated
    fails CI instead of silently drifting."""
    stale = gen_board_table.stale_targets()
    assert not stale, (
        "Generated board doc(s) out of sync with config/defaults.toml [boards.*]: "
        + ", ".join(p.name for p in stale)
        + " — run `python3 scripts/gen_board_table.py --write` and commit."
    )


def test_onboarding_catalog_carries_every_board_with_both_languages():
    """The onboarding picker (docs/index.html BOARD_CATALOG) lists every board
    and — because a new user may run the questionnaire in either language — a
    non-empty EN (`audience`) and RU (`audience_ru`) line for each."""
    import settings

    boards = settings.boards()
    block = gen_board_table.render_board_catalog_js()
    for board_id, cfg in boards.items():
        assert f'id: "{board_id}"' in block, f"{board_id} missing from BOARD_CATALOG"
        assert cfg.get("audience"), f"{board_id} has no EN audience in config"
        assert cfg.get("audience_ru"), f"{board_id} has no RU audience in config"
    # One object per board, no more (the picker shows exactly the config set).
    assert block.count("{ id:") == len(boards)
