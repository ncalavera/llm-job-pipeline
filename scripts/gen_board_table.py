#!/usr/bin/env python3
"""Render the built-in job-board catalogue straight from config — the ONE source.

Three docs used to state three different board counts ("Ten" / "six" / "6
built-ins"). The fix: derive every board listing from ``config._ALL_JOB_BOARDS``
(equivalently ``settings.boards()`` — a pure ``config/defaults.toml`` read, no DB)
so the count, the audience lines and the onboarding picker can never drift from
the actual ``[boards.*]`` blocks.

Two docs embed generated blocks between AUTO-GENERATED markers, both guarded by
``tests/test_board_catalogue_matches_config.py``:

* ``docs/job-boards-catalogue.md`` — the human reference table (EN audience).
* ``docs/index.html`` — the onboarding questionnaire's board picker, as a JS
  ``BOARD_CATALOG`` array carrying each board's EN + RU audience (``audience`` /
  ``audience_ru``) so a new user picks boards knowingly in either language and
  the chosen set lands in the generated setup prompt (DHA-360).

Usage::

    python3 scripts/gen_board_table.py            # print the .md block to stdout
    python3 scripts/gen_board_table.py --check     # exit 1 if EITHER doc is stale
    python3 scripts/gen_board_table.py --write      # rewrite BOTH doc blocks

Board order follows the TOML (insertion order), so both docs read top-to-bottom
the way ``config/defaults.toml`` is written.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PROJECT_ROOT = SCRIPTS_DIR.parent
CATALOGUE_PATH = PROJECT_ROOT / "docs" / "job-boards-catalogue.md"
DOCS_INDEX_PATH = PROJECT_ROOT / "docs" / "index.html"

BEGIN_MARKER = "<!-- BEGIN AUTO-GENERATED BOARD TABLE (scripts/gen_board_table.py) -->"
END_MARKER = "<!-- END AUTO-GENERATED BOARD TABLE -->"

# The onboarding picker's markers live inside a <script> block, so they are JS
# line comments (not HTML comments). Six-space indent matches the surrounding JS
# and is baked INTO the marker strings so the splice preserves it on both the
# opening and closing marker (prettier keeps embedded-JS comments at their block
# indent, so an unindented marker would drift on the next format).
JS_INDENT = "      "
JS_BEGIN_MARKER = JS_INDENT + "// BEGIN AUTO-GENERATED BOARD CATALOG (scripts/gen_board_table.py)"
JS_END_MARKER = JS_INDENT + "// END AUTO-GENERATED BOARD CATALOG"


def _boards() -> dict:
    """All defined boards, {id: cfg}. Pure TOML read (no DB import)."""
    import settings

    return settings.boards()


def _cell(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def render_table() -> str:
    """The auto-generated catalogue table block (no surrounding markers).

    A single stated count (derived) plus one row per board. Kept deterministic
    so the guard test can assert byte-equality against the committed doc.
    """
    boards = _boards()
    lines = [
        f"The pipeline ships **{len(boards)} built-in job boards**. Every one is "
        "opt-in (off by default) and free (public API/feed, no key).",
        "",
        "| ID | Board | Who it fits |",
        "| --- | --- | --- |",
    ]
    for board_id, cfg in boards.items():
        name = _cell(cfg.get("name", board_id))
        audience = _cell(cfg.get("audience", ""))
        lines.append(f"| `{board_id}` | {name} | {audience} |")
    return "\n".join(lines)


def render_board_catalog_js() -> str:
    """The onboarding picker's ``BOARD_CATALOG`` JS array (no surrounding markers).

    One object per board with its id, display name, url and BOTH audience lines
    (``en`` from ``audience``, ``ru`` from ``audience_ru``, falling back to EN so
    a board added without a translation still renders). ``json.dumps`` handles
    all string escaping, so a name/audience with a quote can never break the JS.
    """
    boards = _boards()
    # `// prettier-ignore` keeps the formatter (PostToolUse hook / CI) from
    # reflowing our one-object-per-line layout — otherwise it expands each entry
    # to multi-line and the byte-exact --check guard trips on every edit.
    rows = [f"{JS_INDENT}// prettier-ignore", f"{JS_INDENT}const BOARD_CATALOG = ["]
    for board_id, cfg in boards.items():
        name = cfg.get("name", board_id)
        en = cfg.get("audience", "")
        ru = cfg.get("audience_ru", en)
        url = cfg.get("url", "")
        rows.append(
            f"{JS_INDENT}  {{ id: {json.dumps(board_id)}, "
            f"name: {json.dumps(name)}, url: {json.dumps(url)}, "
            f"en: {json.dumps(en, ensure_ascii=False)}, "
            f"ru: {json.dumps(ru, ensure_ascii=False)} }},"
        )
    rows.append(f"{JS_INDENT}];")
    return "\n".join(rows)


# One generated block per doc: (path, begin marker, end marker, renderer).
_TARGETS = [
    (CATALOGUE_PATH, BEGIN_MARKER, END_MARKER, render_table),
    (DOCS_INDEX_PATH, JS_BEGIN_MARKER, JS_END_MARKER, render_board_catalog_js),
]


def _split_doc(path: Path, text: str, begin: str, end: str) -> tuple[str, str]:
    """Return (before, after) around a doc's AUTO-GENERATED block markers."""
    if begin not in text or end not in text:
        raise ValueError(f"{path.name} is missing the AUTO-GENERATED markers ({begin!r} / {end!r})")
    return text.split(begin, 1)[0], text.split(end, 1)[1]


def _committed_block(path: Path, begin: str, end: str) -> str:
    """The block currently committed in ``path`` (between its markers)."""
    text = path.read_text(encoding="utf-8")
    return text.split(begin, 1)[1].split(end, 1)[0].strip("\n")


def _rendered_doc(path: Path, begin: str, end: str, renderer) -> str:
    """``path`` with a freshly rendered block spliced back between its markers."""
    text = path.read_text(encoding="utf-8")
    before, after = _split_doc(path, text, begin, end)
    return f"{before}{begin}\n{renderer()}\n{end}{after}"


# Back-compat shims for tests that import the original catalogue-only helpers.
def committed_block() -> str:
    return _committed_block(CATALOGUE_PATH, BEGIN_MARKER, END_MARKER)


def rendered_doc() -> str:
    return _rendered_doc(CATALOGUE_PATH, BEGIN_MARKER, END_MARKER, render_table)


def stale_targets() -> list[Path]:
    """Every doc whose committed block disagrees with a fresh render."""
    stale = []
    for path, begin, end, renderer in _TARGETS:
        if _committed_block(path, begin, end) != renderer():
            stale.append(path)
    return stale


def main(argv: list[str]) -> int:
    if "--check" in argv:
        stale = stale_targets()
        if stale:
            names = ", ".join(p.relative_to(PROJECT_ROOT).as_posix() for p in stale)
            print(
                f"{names} is/are STALE — run `python3 scripts/gen_board_table.py --write`",
                file=sys.stderr,
            )
            return 1
        print("Board catalogue docs are up to date.")
        return 0
    if "--write" in argv:
        for path, begin, end, renderer in _TARGETS:
            path.write_text(_rendered_doc(path, begin, end, renderer), encoding="utf-8")
            print(f"Wrote {path.relative_to(PROJECT_ROOT)}")
        return 0
    print(render_table())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
