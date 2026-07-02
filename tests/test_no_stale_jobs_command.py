"""Regression: no stale job-pipeline slash-commands after the 13→6 merge.

The pipeline was consolidated into 6 commands: ``/jobs-new``,
``/jobs-review``, ``/jobs-add``, ``/jobs-profile``, ``/jobs-digest``,
``/jobs-update``. Ten old slash-commands were deleted. A leftover ``/jobs-fetch``
(etc.) in a doc or runbook would tell a user to invoke a command that no longer
exists.

Scope: text files under ``docs/`` (excluding the historical ``docs/plans/`` and
``docs/brainstorms/`` design records, which legitimately name the old commands),
``.claude/commands/``, and the root user-facing docs. Scripts are NOT scanned —
they contain ATS URLs like ``jobs.lever.co`` that would false-positive.

A stale ref is the literal old slash-command at a command boundary. URLs/paths
(``jobs.lever.co``, ``vacancies/jobs-archive/``) never match because they have no
leading ``/`` at the command boundary, or the ``/`` is preceded by a word char.
"""

import re
from pathlib import Path

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
]

# A slash-command appears at a command boundary: line start, or right after
# whitespace / backtick / ``(`` / markdown emphasis (``*`` ``_``) / quote / ``>``.
# This excludes URL/path suffixes like ``.../boards/${SLUG}/jobs`` (the ``/`` is
# preceded by ``}``) and ``jobs.lever.co`` (no leading slash), while still catching
# ``**/jobs-fetch**`` and `"/jobs-fetch"`. The trailing ``(?![\w-])`` keeps the bare
# ``jobs`` pattern from matching ``/jobs-new`` and friends.
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


def test_no_stale_jobs_slash_command():
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


def test_patterns_match_and_exclude_correctly():
    for name in STALE_NAMES:
        pat = PATTERNS[name]
        assert pat.search(f"run /{name} now"), name  # bare → flagged
        assert pat.search(f"`/{name}`"), name  # backticks → flagged
        assert pat.search(f"**/{name}**"), name  # markdown bold → flagged
        assert pat.search(f'"/{name}"'), name  # quoted → flagged
    # survivors, URLs, and path prose must NOT match any stale pattern
    for safe in (
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
        "`jobs`",
    ):  # README deprecation table uses slash-less old names
        assert not any(p.search(safe) for p in PATTERNS.values()), safe
