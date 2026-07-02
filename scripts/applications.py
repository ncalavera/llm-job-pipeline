"""Application records — the DB side of an application as a first-class entity.

An application is a row in the ``application`` table (migration 0010), NOT extra
columns on the vacancy. It carries its own lifecycle (applied -> interview ->
offer/rejected), the channel it went out on, the date, and references to the
artifacts that were sent: which CV version, the cover letter, interview-question
answers, links to research. The artifact FILES live only in the gitignored
private zone (``config.APPLICATION_ARTIFACTS_DIR``); this module stores their
references + small inline text, so nothing personal enters public code or the
public dashboard.

Backend-agnostic, exactly like ``scripts/learning.py``: ``%s`` placeholders and
``now()`` are translated by the SQLite adapter, ``Json()`` wraps the artifacts
column so it lands as JSONB on Postgres and JSON text on SQLite, and every write
commits explicitly (the DAL does not auto-commit — see AGENTS.md).

Migration-only table: a database that has not run ``migrate.py`` yet degrades
via :func:`table_ready` (mirrors ``learning.table_ready`` /
``database_supabase._scored_by_supported``) instead of crashing the run.
"""

from __future__ import annotations

from datetime import date, datetime

#: The application lifecycle. Mirrors the CHECK constraint in migration 0010.
VALID_STATUSES = ("draft", "applied", "interview", "offer", "rejected", "withdrawn")

#: Advisory channel vocabulary (the DB does not constrain ``channel`` — a user
#: may record any submission route). Surfaced so callers stay consistent.
VALID_CHANNELS = ("site", "email", "form", "referral", "other")


def _conn():
    from db_conn import get_conn

    return get_conn()


def _is_sqlite() -> bool:
    from db_conn import IS_SQLITE

    return IS_SQLITE


def table_ready() -> bool:
    """True once the ``application`` table exists (migration 0010 has run).

    False means ``migrate.py`` has not run yet; callers degrade gracefully
    (the dashboard shows no applications) instead of raising "no such table".
    """
    cur = _conn().cursor()
    try:
        if _is_sqlite():
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='application'")
            return cur.fetchone() is not None
        cur.execute("SELECT to_regclass('public.application')")
        row = cur.fetchone()
        return bool(row and row[0] is not None)
    except Exception:
        return False
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "vacancy_id",
    "company_id",
    "channel",
    "status",
    "applied_at",
    "artifacts",
    "notes",
    "created_at",
    "updated_at",
)


def _to_iso(value):
    """Normalise a temporal column to an ISO string on either backend.

    Postgres returns ``date``/``datetime`` objects; SQLite returns ISO text.
    """
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _artifacts(raw) -> dict:
    """Normalise the artifacts column — dict on Postgres (JSONB), JSON text or
    dict on SQLite (``_JSON_COLUMNS`` usually decodes it to a dict already)."""
    import json

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _row_to_dict(row) -> dict:
    """Map a positional ``application`` row (in ``_COLUMNS`` order) to a
    display-safe dict: ids as str, temporals as ISO strings, artifacts as dict."""
    d = dict(zip(_COLUMNS, row))
    return {
        "id": str(d["id"]),
        "vacancy_id": str(d["vacancy_id"]) if d["vacancy_id"] is not None else None,
        "company_id": str(d["company_id"]) if d["company_id"] is not None else None,
        "channel": d["channel"] or "",
        "status": d["status"] or "",
        "applied_at": _to_iso(d["applied_at"]),
        "artifacts": _artifacts(d["artifacts"]),
        "notes": d["notes"] or "",
        "created_at": _to_iso(d["created_at"]),
        "updated_at": _to_iso(d["updated_at"]),
    }


_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM application"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get(application_id: str) -> dict | None:
    """Return one application by id, or None."""
    if not table_ready():
        return None
    cur = _conn().cursor()
    cur.execute(f"{_SELECT} WHERE id = %s", (application_id,))
    row = cur.fetchone()
    cur.close()
    return _row_to_dict(row) if row else None


def get_for_vacancy(vacancy_id: str) -> dict | None:
    """Return the application attached to a vacancy, or None (1:1 by design)."""
    if not vacancy_id or not table_ready():
        return None
    cur = _conn().cursor()
    cur.execute(f"{_SELECT} WHERE vacancy_id = %s", (vacancy_id,))
    row = cur.fetchone()
    cur.close()
    return _row_to_dict(row) if row else None


def list_for_company(company_id: str) -> list[dict]:
    """Return all applications for a company, newest first."""
    if not company_id or not table_ready():
        return []
    cur = _conn().cursor()
    cur.execute(f"{_SELECT} WHERE company_id = %s ORDER BY created_at DESC", (company_id,))
    rows = cur.fetchall()
    cur.close()
    return [_row_to_dict(r) for r in rows]


def applications_by_vacancy() -> dict[str, dict]:
    """Bulk map {vacancy_id: application} for every application with a vacancy.

    One query for the whole dashboard, so the report generator does not do a
    round-trip per card. Empty dict when the table is not ready."""
    if not table_ready():
        return {}
    cur = _conn().cursor()
    cur.execute(f"{_SELECT} WHERE vacancy_id IS NOT NULL")
    rows = cur.fetchall()
    cur.close()
    out: dict[str, dict] = {}
    for r in rows:
        app = _row_to_dict(r)
        out[app["vacancy_id"]] = app
    return out


def applications_by_company() -> dict[str, list[dict]]:
    """Bulk map {company_id: [applications]} for the whole dashboard. Empty dict
    when the table is not ready."""
    if not table_ready():
        return {}
    cur = _conn().cursor()
    cur.execute(f"{_SELECT} ORDER BY created_at DESC")
    rows = cur.fetchall()
    cur.close()
    out: dict[str, list[dict]] = {}
    for r in rows:
        app = _row_to_dict(r)
        out.setdefault(app["company_id"], []).append(app)
    return out


# ---------------------------------------------------------------------------
# Writes (each commits explicitly — the DAL does not auto-commit)
# ---------------------------------------------------------------------------


def record_application(
    company_id: str,
    vacancy_id: str | None = None,
    *,
    channel: str | None = None,
    status: str = "applied",
    applied_at: str | None = None,
    artifacts: dict | None = None,
    notes: str | None = None,
) -> str:
    """Create (or update) the application for a vacancy and return its id.

    Idempotent per vacancy: one vacancy has at most one application, so if a row
    already exists for ``vacancy_id`` it is UPDATED rather than duplicated. On an
    update, new artifacts are MERGED into the existing ones and every other field
    is overwrite-if-given / preserve-if-None: passing ``channel``/``applied_at``/
    ``notes`` as None leaves the stored value untouched, an explicit value wins.
    A ``vacancy_id`` of None (a hand-added application with no tracked vacancy)
    always inserts.

    ``applied_at`` defaults to today's date on INSERT (unless the status is
    ``draft``); on UPDATE it is preserved when None, so re-recording — to add an
    artifact or move status — never resets it. Commits before returning; the
    write is durable across connections.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    if not table_ready():
        raise RuntimeError(
            "application table missing — run `python3 scripts/migrate.py` first (migration 0010)."
        )

    from db_backend import Json

    conn = _conn()
    cur = conn.cursor()

    existing_id = None
    if vacancy_id:
        cur.execute("SELECT id FROM application WHERE vacancy_id = %s", (vacancy_id,))
        row = cur.fetchone()
        if row:
            existing_id = str(row[0])

    if existing_id is not None:
        merged = dict(_artifacts(_current_artifacts(cur, existing_id)))
        if artifacts:
            merged.update(artifacts)
        cur.execute(
            "UPDATE application SET channel = COALESCE(%s, channel), status = %s, "
            "applied_at = COALESCE(%s, applied_at), artifacts = %s, "
            "notes = COALESCE(%s, notes), updated_at = now() "
            "WHERE id = %s",
            (channel, status, applied_at, Json(merged), notes, existing_id),
        )
        cur.close()
        conn.commit()
        return existing_id

    # INSERT: stamp today's date unless this is a draft (a draft has no
    # submission date yet). On UPDATE the stored applied_at is preserved above.
    if applied_at is None and status != "draft":
        applied_at = date.today().isoformat()
    cur.execute(
        "INSERT INTO application "
        "(vacancy_id, company_id, channel, status, applied_at, artifacts, notes) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            vacancy_id,
            company_id,
            channel,
            status,
            applied_at,
            Json(artifacts or {}),
            notes,
        ),
    )
    new_id = str(cur.fetchone()[0])
    cur.close()
    conn.commit()
    return new_id


def _current_artifacts(cur, application_id: str):
    cur.execute("SELECT artifacts FROM application WHERE id = %s", (application_id,))
    row = cur.fetchone()
    return row[0] if row else {}


def set_status(application_id: str, status: str) -> bool:
    """Move an application to a new lifecycle status. Returns True on a hit.

    Commits explicitly, mirroring the vacancy-status helpers' contract that a
    write is durable across connections."""
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    if not table_ready():
        return False
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE application SET status = %s, updated_at = now() WHERE id = %s",
        (status, application_id),
    )
    hit = cur.rowcount > 0
    cur.close()
    conn.commit()
    return hit


def attach_artifacts(application_id: str, artifacts: dict) -> bool:
    """Merge extra artifact references into an application. Returns True on a hit.

    New keys overwrite existing ones; other keys are preserved. Commits."""
    if not artifacts or not table_ready():
        return False
    from db_backend import Json

    conn = _conn()
    cur = conn.cursor()
    merged = dict(_artifacts(_current_artifacts(cur, application_id)))
    merged.update(artifacts)
    cur.execute(
        "UPDATE application SET artifacts = %s, updated_at = now() WHERE id = %s",
        (Json(merged), application_id),
    )
    hit = cur.rowcount > 0
    cur.close()
    conn.commit()
    return hit
