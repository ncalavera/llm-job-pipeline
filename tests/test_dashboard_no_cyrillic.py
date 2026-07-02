"""Regression: the English public dashboard ships zero Cyrillic.

The dashboard shell + logic (``public/index.html``, ``public/app.js``,
``public/style.css``, ``public/modules/*.js``) is the public, English-language UI.
Raw Cyrillic (``[Ѐ-ӿ]``, U+0400–U+04FF) or its escaped form (``\\u04XX``) in any
of these tracked files means owner-language copy leaked into the public shell.

``public/data.js`` is excluded: it is gitignored, generated, and legitimately
carries vacancy text in any language.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PUBLIC = REPO / "public"

RAW_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
ESCAPED_CYRILLIC = re.compile(r"\\u04[0-9a-fA-F]{2}")


def _dashboard_files() -> list[Path]:
    files: list[Path] = []
    for rel in ("index.html", "app.js", "style.css"):
        p = PUBLIC / rel
        if p.exists():
            files.append(p)
    modules = PUBLIC / "modules"
    if modules.exists():
        files.extend(sorted(modules.glob("*.js")))
    return files


def test_dashboard_files_present():
    """The scan must actually have files to scan (catches a moved layout)."""
    files = _dashboard_files()
    assert files, "no public dashboard files found — scan would be vacuous"
    # data.js must never be in scope.
    assert all(p.name != "data.js" for p in files)


def test_no_raw_cyrillic_in_dashboard():
    hits: list[str] = []
    for p in _dashboard_files():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if RAW_CYRILLIC.search(line):
                hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")
    assert not hits, "Raw Cyrillic leaked into the English dashboard:\n" + "\n".join(hits)


def test_no_escaped_cyrillic_in_dashboard():
    hits: list[str] = []
    for p in _dashboard_files():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if ESCAPED_CYRILLIC.search(line):
                hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")
    assert not hits, "Escaped Cyrillic (\\u04XX) leaked into the English dashboard:\n" + "\n".join(
        hits
    )
