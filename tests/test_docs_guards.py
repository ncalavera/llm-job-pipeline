"""Public-docs guard rails: no stale slash-command names, no dead local links.

Absorbed from tests/test_no_stale_pipeline_commands.py and
tests/test_docs_links_exist.py. Both walk the same user-facing doc set for a
different kind of rot: a slash-command name that was renamed or folded away
but still appears as a live instruction, and a repo-relative Markdown/HTML
link that points at a file no longer in the tree.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# --- from test_no_stale_pipeline_commands.py ---
#
# Regression: no stale job-pipeline slash-command names in public docs/runbooks.
#
# Merges two near-duplicate guard files that both watched the SAME
# command-consolidation history, just at different points in it:
#
#   - the 13-command -> 6-command merge (``/jobs-fetch``, ``/jobs-apply``,
#     ``/jobs-vac``, ... folded into ``/jobs-new``, ``/jobs-review``, etc.)
#   - the earlier ``/score`` -> ``/jobs-score`` rename (before ``/jobs-score``
#     itself was folded into the same 13->6 merge)
#
# Both are "does this stale name still appear as a live slash-command"
# regex-guards over the same directories, so they are one parametrized test now.
# The live commands today are ``/jobs-new``, ``/jobs-review``, ``/jobs-add``,
# ``/jobs-profile``, ``/jobs-digest``, ``/jobs-update``.
#
# Scope: text files under ``docs/`` (excluding the historical ``docs/plans/`` and
# ``docs/brainstorms/`` design records, and ``docs/solutions/`` debugging
# post-mortems, which legitimately name old commands or unrelated URL paths),
# ``.claude/commands/``, and the root user-facing docs. Scripts are NOT scanned —
# they contain ATS URLs like ``jobs.lever.co`` and paths like
# ``scripts/score_vacancies.py`` that would false-positive.
#
# A stale ref is the literal old slash-command at a command boundary: line
# start, or right after whitespace / backtick / ``(`` / markdown emphasis
# (``*`` ``_``) / quote / ``>``. This excludes URL/path suffixes like
# ``.../boards/${SLUG}/jobs`` (the ``/`` is preceded by ``}``) and
# ``jobs.lever.co`` / ``scripts/score_vacancies.py`` (the ``/`` is preceded by a
# word char, not a boundary), while still catching ``**/jobs-fetch**`` and
# ``"/jobs-fetch"``. The trailing ``(?![\\w-])`` keeps the bare ``jobs`` pattern
# from matching ``/jobs-new`` and friends, and the bare ``score`` pattern from
# matching ``/jobs-score`` or ``/scoreboard``.


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


# --- from test_docs_links_exist.py ---
#
# Guard: no dead repo-relative links in the user-facing root docs.
#
# README / INSTALL* / AGENTS.md kept promising `docs/*` files that did not exist
# (dead links are a documentation bug of the same severity as a crash — STRATEGY
# guardrail 4). This walks each of those docs for repo-relative Markdown links and
# fails on any that point at a file not present in the tree.
#
# Scope: only local links are checked. External URLs (`http`, `https`, `mailto`,
# `tel`), pure in-page anchors (`#section`) and template placeholders (`{{...}}`)
# are skipped. A `path#anchor` link is checked for the FILE only (the anchor
# fragment is not verified — headings move; files should not vanish).
#
# `.html` docs have no Markdown links, so they are scanned with a second regex
# for `href="..."`/`src="..."` attributes instead — same normalisation, same
# skip rules.

# The user-facing entry docs whose links must never rot.
SCANNED_DOCS = [
    "README.md",
    "INSTALL.md",
    "INSTALL-EASY.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "CONCEPTS.md",
    "docs/ARCHITECTURE.md",
    "docs/fetch-engines.md",
    "docs/job-boards-catalogue.md",
    "docs/manual-trial-protocol.md",
    "docs/index.html",
]

# Inline Markdown link target: the `(...)` half of `[text](target)`.
_LINK = re.compile(r"\]\(([^)]+)\)")

# HTML attribute link target: the `"..."` half of `href="..."`/`src="..."`.
_HTML_LINK = re.compile(r'(?:href|src)="([^"]*)"')

_SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#", "{{")


def _link_target(raw: str) -> str | None:
    """Normalise a raw `(...)` payload to a repo-relative file path, or None to
    skip (external URL, in-page anchor, placeholder)."""
    target = raw.strip()
    # `](path "Title")` — drop an optional link title.
    target = target.split()[0] if target.split() else target
    # `](<path>)` — angle-bracket form.
    target = target.strip("<>")
    if not target or target.startswith(_SKIP_PREFIXES):
        return None
    # Drop a #fragment / ?query — we verify the file, not the anchor.
    target = target.split("#", 1)[0].split("?", 1)[0]
    return target or None


def _iter_links():
    """Yield (doc_relpath, lineno, raw_target) for every inline link found."""
    for name in SCANNED_DOCS:
        doc = REPO / name
        if not doc.exists():
            continue
        pattern = _HTML_LINK if doc.suffix == ".html" else _LINK
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for m in pattern.finditer(line):
                yield name, i, m.group(1)


def test_no_dead_repo_relative_links():
    dead: list[str] = []
    for name, lineno, raw in _iter_links():
        target = _link_target(raw)
        if target is None:
            continue
        # Resolve relative to the doc's directory (all scanned docs are at repo
        # root, so this is repo-root-relative).
        resolved = (REPO / name).parent / target
        if not resolved.exists():
            dead.append(f"{name}:{lineno} -> {target}")
    assert not dead, (
        "Dead repo-relative link(s) in the root docs — write the target or "
        "remove the link:\n" + "\n".join(dead)
    )


# --- self-tests: the checker must actually catch a bad link ---------------


def test_link_target_skips_external_and_anchors():
    assert _link_target("https://example.com") is None
    assert _link_target("#section") is None
    assert _link_target("mailto:x@y.z") is None
    assert _link_target("{{USER_PROFILE}}") is None


def test_link_target_extracts_local_paths():
    assert _link_target("docs/ARCHITECTURE.md") == "docs/ARCHITECTURE.md"
    assert _link_target("INSTALL.md#9-telegram-digest-optional") == "INSTALL.md"
    assert _link_target("<sql/schema.sql>") == "sql/schema.sql"


def test_html_link_regex_extracts_attrs():
    assert _HTML_LINK.findall('<img src="assets/robot-courier.jpg" alt="x">') == [
        "assets/robot-courier.jpg"
    ]
    assert _HTML_LINK.findall('<link rel="icon" href="favicon.svg">') == ["favicon.svg"]


@pytest.mark.parametrize("name", SCANNED_DOCS)
def test_scanned_docs_present(name):
    assert (REPO / name).exists(), f"{name} is expected to exist and be scanned"
