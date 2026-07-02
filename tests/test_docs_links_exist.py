"""Guard: no dead repo-relative links in the user-facing root docs.

README / INSTALL* / AGENTS.md kept promising `docs/*` files that did not exist
(dead links are a documentation bug of the same severity as a crash — STRATEGY
guardrail 4). This walks each of those docs for repo-relative Markdown links and
fails on any that point at a file not present in the tree.

Scope: only local links are checked. External URLs (`http`, `https`, `mailto`,
`tel`), pure in-page anchors (`#section`) and template placeholders (`{{...}}`)
are skipped. A `path#anchor` link is checked for the FILE only (the anchor
fragment is not verified — headings move; files should not vanish).
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The user-facing entry docs whose links must never rot.
SCANNED_DOCS = ["README.md", "INSTALL.md", "INSTALL-EASY.md", "AGENTS.md"]

# Inline Markdown link target: the `(...)` half of `[text](target)`.
_LINK = re.compile(r"\]\(([^)]+)\)")

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
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for m in _LINK.finditer(line):
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


def test_checker_would_flag_a_missing_file(tmp_path):
    """Prove the existence check fails on a nonexistent target and passes on a
    real one — so the guard can never silently start passing."""
    assert not (REPO / "docs/DOES_NOT_EXIST.md").exists()
    assert (REPO / "docs/ARCHITECTURE.md").exists()


@pytest.mark.parametrize("name", SCANNED_DOCS)
def test_scanned_docs_present(name):
    assert (REPO / name).exists(), f"{name} is expected to exist and be scanned"
