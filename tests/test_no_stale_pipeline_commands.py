"""Regression: no stale job-pipeline slash-command names in public docs/runbooks.

Merges two near-duplicate guard files that both watched the SAME
command-consolidation history, just at different points in it:

  - the 13-command -> 6-command merge (``/jobs-fetch``, ``/jobs-apply``,
    ``/jobs-vac``, ... folded into ``/jobs-new``, ``/jobs-review``, etc.)
  - the earlier ``/score`` -> ``/jobs-score`` rename (before ``/jobs-score``
    itself was folded into the same 13->6 merge)

Both are "does this stale name still appear as a live slash-command"
regex-guards over the same directories, so they are one parametrized test now.
The live commands today are ``/jobs-new``, ``/jobs-review``, ``/jobs-add``,
``/jobs-profile``, ``/jobs-digest``, ``/jobs-update``.

Scope: text files under ``docs/`` (excluding the historical ``docs/plans/`` and
``docs/brainstorms/`` design records, and ``docs/solutions/`` debugging
post-mortems, which legitimately name old commands or unrelated URL paths),
``.claude/commands/``, and the root user-facing docs. Scripts are NOT scanned —
they contain ATS URLs like ``jobs.lever.co`` and paths like
``scripts/score_vacancies.py`` that would false-positive.

A stale ref is the literal old slash-command at a command boundary: line
start, or right after whitespace / backtick / ``(`` / markdown emphasis
(``*`` ``_``) / quote / ``>``. This excludes URL/path suffixes like
``.../boards/${SLUG}/jobs`` (the ``/`` is preceded by ``}``) and
``jobs.lever.co`` / ``scripts/score_vacancies.py`` (the ``/`` is preceded by a
word char, not a boundary), while still catching ``**/jobs-fetch**`` and
``"/jobs-fetch"``. The trailing ``(?![\\w-])`` keeps the bare ``jobs`` pattern
from matching ``/jobs-new`` and friends, and the bare ``score`` pattern from
matching ``/jobs-score`` or ``/scoreboard``.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Old commands that must NOT appear as slash-commands anymore.
STALE_NAMES = [
    "jobs",  # the bare daily command
    "jobs-fetch",
    "jobs-filter",
    "jobs-score",
    "jobs-start",
    "jobs-finish",
    "jobs-apply",
    "jobs-archive",
    "jobs-vac",
    "jobs-rules",
    "score",  # pre-rename name for /jobs-score
]

PATTERNS = {
    name: re.compile(r"(?:^|(?<=[\s`(*_\"'>]))/" + re.escape(name) + r"(?![\w-])")
    for name in STALE_NAMES
}

SCAN_DIRS = [REPO / "docs", REPO / ".claude" / "commands"]
SCAN_ROOT_FILES = ["README.md", "AGENTS.md", "INSTALL.md", "INSTALL-EASY.md"]

# Path prefixes (relative to repo) whose stale names are intentional design
# records, not live instructions. `docs/solutions/` are debugging post-mortems
# that quote URL paths like `/jobs/` (a data.org redirect target), which are not
# slash-command references.
EXCLUDE_PREFIXES = ("docs/plans/", "docs/brainstorms/", "docs/solutions/")


def _scanned_files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                out.append(p)
    for name in SCAN_ROOT_FILES:
        p = REPO / name
        if p.exists():
            out.append(p)
    return out


def test_no_stale_pipeline_slash_commands():
    hits: list[str] = []
    for p in _scanned_files():
        rel = str(p.relative_to(REPO)).replace("\\", "/")
        if rel.startswith(EXCLUDE_PREFIXES):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for name, pat in PATTERNS.items():
                if pat.search(line):
                    hits.append(f"{rel}:{i} [/{name}]: {line.strip()[:120]}")
    assert not hits, (
        "Stale job-pipeline slash-command(s) found — the live commands are "
        "/jobs-new /jobs-review /jobs-add /jobs-profile /jobs-digest /jobs-update:\n"
        + "\n".join(hits)
    )


# Strings that must never match ANY stale pattern: the live commands and
# various URL/path shapes that share a substring with a stale name.
_SAFE_STRINGS = (
    "/jobs-new",
    "/jobs-review",
    "/jobs-add",
    "/jobs-profile",
    "/jobs-digest",
    "/jobs-update",
    "jobs.lever.co",
    "vacancies/jobs-archive/x.json",
    "a/jobs/b",
    'boards/${SLUG_GUESS}/jobs"',
    "accounts/x/jobs",
    "`jobs`",  # README deprecation table uses slash-less old names
    "fetch/score/filter",  # path-style prose, not a slash-command
    "/scoreboard",  # different command
    "scripts/score_vacancies.py",
)


@pytest.mark.parametrize("name", STALE_NAMES)
def test_pattern_matches_and_excludes_correctly(name):
    """Guards the matchers themselves so they can never silently start passing."""
    pat = PATTERNS[name]
    assert pat.search(f"run /{name} now"), name  # bare → flagged
    assert pat.search(f"`/{name}`"), name  # backticks → flagged
    assert pat.search(f"**/{name}**"), name  # markdown bold → flagged
    assert pat.search(f'"/{name}"'), name  # quoted → flagged
    assert pat.search(f"(/{name})"), name  # parens → flagged
    for safe in _SAFE_STRINGS:
        assert not pat.search(safe), f"{name} pattern false-positived on {safe!r}"


def test_bare_score_pattern_ignores_jobs_score():
    """`score` must fire on a bare `/score` but never on `/jobs-score` — the
    two are separate stale names (one from the /score rename, one from the
    later 13->6 merge) tracked by separate patterns in STALE_NAMES."""
    assert PATTERNS["score"].search("run /score now")
    assert not PATTERNS["score"].search("run /jobs-score now")
