"""Regression: no stale ``/score`` slash-command in public docs / runbooks.

The real command is ``/jobs-score`` (the skill / slash-command was renamed).
A bare ``/score`` in docs or .claude command runbooks would tell a public user
to invoke a command that does not exist.

Scope of the scan: every text file under ``docs/`` and ``.claude/commands/``.

What counts as a stale reference: the literal token ``/score`` at a command
boundary that is NOT part of ``/jobs-score``. Prose like "fetch/score/filter"
(a path-style enumeration, not a slash-command) and the legitimate
``/jobs-score`` command must NOT false-positive.

Current state: zero legitimate ``/score`` usages exist, so the allowlist is
empty and the assertion is for zero hits.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
COMMANDS = REPO / ".claude" / "commands"

# A bare ``/score`` slash-command: ``/score`` preceded by start-of-line or a
# character that is NOT one that would make it part of a larger token
# (so ``/jobs-score`` and ``fetch/score/filter`` are excluded — both have an
# alnum/``-``/``/`` immediately before the ``s``... no: the leading slash is the
# anchor). We anchor on the literal ``/score`` and require that:
#   - what precedes the ``/`` is NOT a word char / ``-`` / ``/`` (so
#     ``jobs-score`` -> there is no ``/`` before ``score`` at all, and
#     ``fetch/score`` -> ``/`` IS preceded by ``h`` (a word char) → excluded),
#   - what follows ``/score`` is NOT a word char / ``-`` (so ``/scoreboard`` and
#     ``/score-foo`` do not match; ``/jobs-score`` never reaches here).
#
# Net effect: only a real top-level ``/score`` slash-command (e.g. at line start,
# after whitespace, after backtick, after '(') is flagged.
STALE_SCORE = re.compile(r"(?<![\w/-])/score(?![\w-])")

# Files whose stale ``/score`` is a deliberate, documented legacy note. Empty by
# design: there are currently zero legitimate uses. Add a path here ONLY with a
# comment justifying it.
ALLOWLIST: set[str] = set()


def _scanned_files() -> list[Path]:
    out: list[Path] = []
    for root in (DOCS, COMMANDS):
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                out.append(p)
    return out


def test_no_bare_score_slash_command():
    hits: list[str] = []
    for p in _scanned_files():
        rel = str(p.relative_to(REPO))
        if rel in ALLOWLIST:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if STALE_SCORE.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:120]}")
    assert not hits, (
        "Stale `/score` slash-command found — the real command is `/jobs-score`:\n"
        + "\n".join(hits)
    )


def test_regex_distinguishes_score_from_jobs_score():
    """Guards the matcher itself so it can never silently start passing."""
    assert STALE_SCORE.search("run /score now")  # bare command → flagged
    assert STALE_SCORE.search("`/score`")  # in backticks → flagged
    assert STALE_SCORE.search("(/score)")  # in parens → flagged
    assert not STALE_SCORE.search("run /jobs-score now")  # the real command
    assert not STALE_SCORE.search("fetch/score/filter")  # path-style prose
    assert not STALE_SCORE.search("/scoreboard")  # different command
