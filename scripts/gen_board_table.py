#!/usr/bin/env python3
"""Render the built-in job-board table straight from config — the ONE source.

Three docs used to state three different board counts ("Ten" / "six" / "6
built-ins"). The fix: derive the catalogue table from ``config._ALL_JOB_BOARDS``
(equivalently ``settings.boards()`` — a pure ``config/defaults.toml`` read, no DB)
so the count and the audience lines can never drift from the actual
``[boards.*]`` blocks. ``docs/job-boards-catalogue.md`` embeds the output of
``render_table()`` between AUTO-GENERATED markers; ``tests/test_board_catalogue_
matches_config.py`` fails if the committed block and this renderer disagree.

Usage::

    python3 scripts/gen_board_table.py            # print the block to stdout
    python3 scripts/gen_board_table.py --check     # exit 1 if the doc is stale
    python3 scripts/gen_board_table.py --write      # rewrite the doc block

Only ``id``, ``name`` and ``audience`` are rendered — the human-facing columns.
Board order follows the TOML (insertion order), so the table reads top-to-bottom
the way ``config/defaults.toml`` is written.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PROJECT_ROOT = SCRIPTS_DIR.parent
CATALOGUE_PATH = PROJECT_ROOT / "docs" / "job-boards-catalogue.md"

BEGIN_MARKER = "<!-- BEGIN AUTO-GENERATED BOARD TABLE (scripts/gen_board_table.py) -->"
END_MARKER = "<!-- END AUTO-GENERATED BOARD TABLE -->"


def _boards() -> dict:
    """All defined boards, {id: cfg}. Pure TOML read (no DB import)."""
    import settings

    return settings.boards()


def _cell(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def render_table() -> str:
    """The auto-generated catalogue block (no surrounding markers).

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


def _split_doc(text: str) -> tuple[str, str]:
    """Return (before, after) around the AUTO-GENERATED block markers."""
    if BEGIN_MARKER not in text or END_MARKER not in text:
        raise ValueError(
            f"{CATALOGUE_PATH.name} is missing the AUTO-GENERATED markers "
            f"({BEGIN_MARKER!r} / {END_MARKER!r})"
        )
    before = text.split(BEGIN_MARKER, 1)[0]
    after = text.split(END_MARKER, 1)[1]
    return before, after


def committed_block() -> str:
    """The current table block committed in the catalogue (between markers)."""
    text = CATALOGUE_PATH.read_text(encoding="utf-8")
    inner = text.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0]
    return inner.strip("\n")


def rendered_doc() -> str:
    """The catalogue with a freshly rendered block spliced back in."""
    text = CATALOGUE_PATH.read_text(encoding="utf-8")
    before, after = _split_doc(text)
    return f"{before}{BEGIN_MARKER}\n{render_table()}\n{END_MARKER}{after}"


def main(argv: list[str]) -> int:
    if "--check" in argv:
        if committed_block() != render_table():
            print(
                "docs/job-boards-catalogue.md is STALE — run "
                "`python3 scripts/gen_board_table.py --write`",
                file=sys.stderr,
            )
            return 1
        print("docs/job-boards-catalogue.md is up to date.")
        return 0
    if "--write" in argv:
        CATALOGUE_PATH.write_text(rendered_doc(), encoding="utf-8")
        print(f"Wrote {CATALOGUE_PATH.relative_to(PROJECT_ROOT)}")
        return 0
    print(render_table())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
