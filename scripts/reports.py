"""Stored research reports — the data layer behind the dashboard's Reports tab.

Every report written for this search is a markdown file in a private repo:
sector research, grant write-ups, company dossiers, the research done for one
application. That means the work is only reachable from the laptop it was
written on. This module puts those files in the database, so the dashboard can
show the reading next to what it was written for.

The identity of a report is its ``slug``, derived from the source filename.
Re-importing an edited file therefore UPDATES the stored report instead of
forking a second copy — the same rule ``vac add`` uses for an application.

Pure helpers (slug, title, kind derivation) are separated from the writes on
purpose: they are what the CLI's behaviour actually rests on, and they test
without a database.
"""

import re
from pathlib import Path

from statuses import VALID_REPORT_KINDS

#: Directories whose reports have an obvious kind, most specific first — the
#: first match wins, so "research/sectors" beats the bare "research" that
#: contains it, and "docs/cv" (research written FOR one company's application)
#: beats a file inside it merely named "research-notes.md".
#:
#: Matched against the DIRECTORIES only, never the filename. Matching the whole
#: path let "docs/cv/givedirectly/research-notes.md" answer 'research', because
#: the word appears in the file's own name — the file is about a company, and
#: its folder is the only part of the path that says so.
_KIND_BY_DIRECTORY = (
    ("docs/cv", "company"),
    ("research/sectors", "sector"),
    ("research/companies", "company"),
    ("companies", "company"),
    ("sectors", "sector"),
    ("grants", "grant"),
    ("research", "research"),
    ("reports", "research"),
)

#: A slug is what ends up in a URL and in the unique index, so it is reduced to
#: the characters that survive both without escaping.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug_from_path(path) -> str:
    """The stem of a markdown file as a URL-safe slug.

    ``research/sectors/EA Funding 2026.md`` -> ``ea-funding-2026``.

    Deliberately built from the filename alone, not the directory: the file is
    what gets renamed, moved and re-imported, and a slug that changed when a
    file moved between folders would fork a second copy of the same report on
    the next import.
    """
    stem = Path(str(path)).stem
    slug = _SLUG_STRIP.sub("-", stem.lower()).strip("-")
    return slug


#: A setext H1 is the other way markdown writes a top-level heading:
#:     Title
#:     =====
_SETEXT_H1 = re.compile(r"^=+\s*$")


def title_from_markdown(body: str, fallback: str = "") -> str:
    """The document's own H1, or ``fallback``.

    Reads the first ATX heading (``# Title``) or setext heading (``Title`` over
    ``====``) — the two forms these files actually use. Front matter and blank
    lines above it are skipped, and only the top of the file is searched: an
    ``# H1`` appearing on page four is a section, not the document's name.
    """
    lines = (body or "").splitlines()
    previous = ""
    for line in lines[:40]:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if previous and _SETEXT_H1.match(stripped):
            return previous
        previous = stripped
    return fallback


def kind_from_path(path) -> str:
    """The report kind implied by the DIRECTORIES the file sits in, else 'other'.

    Only the folders are read — see _KIND_BY_DIRECTORY for why the filename is
    deliberately excluded.
    """
    parent = Path(str(path).replace("\\", "/")).parent
    directories = "/" + "/".join(part.lower() for part in parent.parts).strip("/") + "/"
    for fragment, kind in _KIND_BY_DIRECTORY:
        if "/" + fragment + "/" in directories:
            return kind
    return "other"


def humanize_slug(slug: str) -> str:
    """A last-resort title when a file has no H1: the slug as words."""
    return " ".join(word for word in slug.split("-") if word).capitalize()


#: How many trailing path segments a stored source_path keeps when the file
#: lives outside the working directory. Two is enough to identify a file
#: ("sectors/ea-funding-2026.md") without pinning it to one laptop.
_SOURCE_PATH_SEGMENTS = 2


def display_source_path(path) -> str:
    """The provenance string stored with a report.

    An absolute path is recorded relative to the working directory when the file
    is under it, else trimmed to its last two segments. The dashboard SHOWS this
    line, and "/Users/<name>/Projects/personal/job-search-2026/research/..." on
    screen is both noise and a needless leak of one machine's directory layout —
    the field answers "which file is this", not "where was it on that laptop".
    """
    file_path = Path(str(path))
    if not file_path.is_absolute():
        return file_path.as_posix()
    try:
        return file_path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        parts = file_path.parts[-_SOURCE_PATH_SEGMENTS:]
        return "/".join(parts)


def read_report_file(path) -> dict:
    """Everything derivable from one markdown file, with nothing written yet.

    Returns {slug, title, kind, body_md, source_path}. Raises FileNotFoundError
    for a missing file and ValueError for an empty one — an empty report is
    never what was meant, and storing it would replace a good one on re-import.
    """
    file_path = Path(path)
    body = file_path.read_text(encoding="utf-8")
    if not body.strip():
        raise ValueError(f"{file_path} is empty — nothing to store")

    slug = slug_from_path(file_path)
    if not slug:
        raise ValueError(f"{file_path} has no usable name for a slug")

    return {
        "slug": slug,
        "title": title_from_markdown(body, humanize_slug(slug)),
        "kind": kind_from_path(file_path),
        "body_md": body,
        "source_path": display_source_path(file_path),
    }


# ---------------------------------------------------------------------------
# Writes and reads
# ---------------------------------------------------------------------------


def _conn():
    from database_supabase import get_conn

    return get_conn()


def table_ready() -> bool:
    """True once migration 0023 has run. Lets a caller say "run migrate.py"
    instead of failing with "no such table" — the same courtesy
    learning.table_ready() gives its own optional table."""
    from database_supabase import _table_has_column

    return _table_has_column("report", "slug")


def upsert_report(*, slug, title, kind, body_md, source_path=None) -> str:
    """Insert or update one report by slug. Returns its id.

    ``created_at`` survives an update (the report was first written when it was
    first written); ``updated_at`` moves, because it is what the list sorts by.
    """
    if kind not in VALID_REPORT_KINDS:
        raise ValueError(
            f"Unknown report kind {kind!r}. Allowed: {', '.join(sorted(VALID_REPORT_KINDS))}"
        )
    if not slug or not title or not (body_md or "").strip():
        raise ValueError("a report needs a slug, a title and a body")

    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM report WHERE slug = %s", (slug,))
    row = cur.fetchone()
    if row:
        report_id = row[0]
        cur.execute(
            "UPDATE report SET title = %s, kind = %s, body_md = %s, "
            "source_path = %s, updated_at = now() WHERE id = %s",
            (title, kind, body_md, source_path, report_id),
        )
    else:
        cur.execute(
            "INSERT INTO report (slug, title, kind, body_md, source_path) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (slug, title, kind, body_md, source_path),
        )
        report_id = cur.fetchone()[0]
    cur.close()
    return str(report_id)


def list_reports() -> list[dict]:
    """Every stored report, newest first, without its body."""
    from db_backend import RealDictCursor

    cur = _conn().cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT slug, title, kind, source_path, created_at, updated_at "
        "FROM report ORDER BY updated_at DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def get_report(slug: str) -> dict | None:
    """One report in full, or None."""
    from db_backend import RealDictCursor

    cur = _conn().cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT slug, title, kind, body_md, source_path, created_at, updated_at "
        "FROM report WHERE slug = %s",
        (slug,),
    )
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None
