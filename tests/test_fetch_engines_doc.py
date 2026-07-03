"""Guard: every registered fetch strategy has a section in docs/fetch-engines.md.

The reference page must document EVERY strategy in `COMPANY_FETCHERS` /
`BOARD_FETCHERS` — a registered engine with no section is a doc claim that
doesn't match code, i.e. a crash-severity bug (STRATEGY guardrail 4). New/renamed
strategies must fail CI here instead of silently going undocumented, mirroring
`tests/test_board_catalogue_matches_config.py`.

Sections are anchored mechanically by a per-engine HTML marker
`<!-- ENGINE: <strategy> -->`, so the guard keys off the strategy string, not on
prose that merely mentions a provider name. It is bidirectional: a marker for an
unregistered strategy (a stale/orphan section) also fails.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from fetchers.registry import BOARD_FETCHERS, COMPANY_FETCHERS  # noqa: E402

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
