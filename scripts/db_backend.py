"""Database backend selector — Supabase (psycopg2) or local SQLite.

The whole pipeline talks to one connection object obtained from
``get_conn()``. That object behaves like a psycopg2 connection: cursors
accept ``%s`` placeholders, ``cursor_factory=RealDictCursor`` yields dict
rows, JSON/array values can be wrapped with ``Json(...)``, and Postgres-only
idioms (``ON CONFLICT``, ``RETURNING``, ``ANY(%s::uuid[])``, ``ILIKE``,
``now()``/``interval``, ``aliases @> ARRAY[...]``, ``col->>'key'``) work.

Backend choice is by environment, decided once at import:

* ``SUPABASE_DB_URL`` (or ``SUPABASE_DIRECT_URL``) set  -> real psycopg2.
* neither set                                          -> local SQLite file.

In SQLite mode psycopg2 is never imported, so the easy install path needs no
compiled Postgres driver. ``Json`` and ``RealDictCursor`` are re-exported from
here so call sites import them from ``db_backend`` instead of ``psycopg2`` and
keep working under both backends.
"""

import os
import re
import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _supabase_url() -> str | None:
    return os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_DIRECT_URL")


#: True when running on the local SQLite backend (no Supabase configured).
IS_SQLITE = _supabase_url() is None


def print_backend_banner(stream=None) -> None:
    """Print one clear line naming the active backend.

    Surfaces which database the run is actually talking to, so a stray
    ``SUPABASE_DB_URL`` inherited from another project (which silently flips
    simple mode into someone else's Postgres) is immediately visible. Called at
    the start of fetch / score / filter.
    """
    import sys
    out = stream or sys.stderr
    if IS_SQLITE:
        print(f"Backend: local SQLite ({sqlite_db_path()})", file=out, flush=True)
    else:
        print("Backend: Postgres (Supabase)", file=out, flush=True)


def sqlite_db_path() -> Path:
    """Resolve the local SQLite database file path.

    Honors ``JOBSEARCH_DB_PATH`` (absolute or relative to CWD); otherwise
    ``data/jobsearch.db`` under the project root. Parent dirs are created.
    """
    override = os.environ.get("JOBSEARCH_DB_PATH")
    if override:
        path = Path(override).expanduser()
    else:
        path = PROJECT_ROOT / "data" / "jobsearch.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# psycopg2-compatible helpers (work for both backends)
# ---------------------------------------------------------------------------

if IS_SQLITE:
    import json as _json

    class Json:
        """psycopg2.extras.Json stand-in.

        Wraps a Python value so the SQLite cursor knows to serialize it as a
        JSON string. Mirrors the psycopg2 ``Json`` adapter contract: it is
        passed straight into the parameter list and the adapter boundary does
        the rest.
        """

        __slots__ = ("adapted",)

        def __init__(self, adapted):
            self.adapted = adapted

        def dumps(self) -> str:
            return _json.dumps(self.adapted, ensure_ascii=False, default=str)

    class RealDictCursor:
        """Marker type only — selected via ``cursor(cursor_factory=...)``.

        The real row-dict behavior lives in ``_SqliteCursor`` which checks for
        this marker. Importing this name keeps call sites unchanged.
        """

else:
    # Supabase backend — re-export the genuine psycopg2 helpers so call sites
    # importing them from db_backend behave identically to before.
    from psycopg2.extras import Json, RealDictCursor  # noqa: F401


# ---------------------------------------------------------------------------
# SQLite SQL translation
# ---------------------------------------------------------------------------

# Columns stored as JSON TEXT in SQLite, decoded back to list/dict on read so
# callers see the same shapes psycopg2 returns for JSONB / TEXT[].
_JSON_COLUMNS = frozenset({
    "locations", "triage", "about", "mission_fit", "ats_config",
    "aliases", "llm_tags", "llm_hard_requirements",
})

# Columns psycopg2 returns as datetime/date objects. SQLite stores them as
# ISO strings; decode them back so callers can use .isoformat(), .days, tzinfo.
_DATETIME_COLUMNS = frozenset({
    "created_at", "updated_at", "status_updated_at", "llm_scored_at",
    "last_fetched", "enriched_at", "digest_sent_at", "archived_at",
})
_DATE_COLUMNS = frozenset({"first_seen", "last_seen", "deadline"})


def _decode_temporal(col_name, value):
    """Parse ISO strings from SQLite into datetime/date objects.

    Mirrors psycopg2's TIMESTAMPTZ -> datetime and DATE -> date mapping so the
    DAL's ``.isoformat()`` / ``.days`` calls work unchanged. Timezone-naive
    timestamps are stamped UTC so ``datetime.now(tzinfo)`` arithmetic is safe.
    """
    if value is None or not isinstance(value, str):
        return value
    from datetime import datetime as _dt, date as _date, timezone as _tz
    if col_name in _DATETIME_COLUMNS:
        try:
            parsed = _dt.fromisoformat(value.replace(" ", "T"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_tz.utc)
            return parsed
        except (ValueError, TypeError):
            return value
    if col_name in _DATE_COLUMNS:
        try:
            return _date.fromisoformat(value[:10])
        except (ValueError, TypeError):
            return value
    return value

# Compiled once. Each entry rewrites one Postgres idiom into its SQLite form.
# Order matters: array/json operators run before the generic %s -> ? swap so
# the cast/operator regexes still see their original text.

_JSON_ARROW_RE = re.compile(r"(\w+(?:\.\w+)?)\s*->>\s*'([^']+)'")
_ANY_UUID_RE = re.compile(r"=\s*ANY\(%s::uuid\[\]\)", re.IGNORECASE)
_ANY_TEXT_RE = re.compile(r"=\s*ANY\(%s(?:::text\[\])?\)", re.IGNORECASE)
_CONTAINS_RE = re.compile(
    r"aliases\s*@>\s*ARRAY\[%s\](?:::text\[\])?", re.IGNORECASE
)
_ARRAY_LENGTH_RE = re.compile(
    r"array_length\(\s*aliases\s*,\s*1\s*\)\s*>\s*0", re.IGNORECASE
)
_INTERVAL_RE = re.compile(
    r"now\(\)\s*-\s*interval\s+'(\d+)\s*days?'", re.IGNORECASE
)
# Parameterised interval: interval '%s days'
_INTERVAL_PARAM_RE = re.compile(
    r"now\(\)\s*-\s*interval\s+'%s\s*days?'", re.IGNORECASE
)
_NOW_RE = re.compile(r"\bnow\(\)", re.IGNORECASE)
_CURRENT_DATE_RE = re.compile(r"\bCURRENT_DATE\b", re.IGNORECASE)
_GEN_UUID_RE = re.compile(r"\bgen_random_uuid\(\)", re.IGNORECASE)
_ILIKE_RE = re.compile(r"\bILIKE\b", re.IGNORECASE)
_CAST_UUID_RE = re.compile(r"::uuid\b", re.IGNORECASE)
_CAST_TEXT_ARR_RE = re.compile(r"::text\[\]", re.IGNORECASE)
_IS_DISTINCT_RE = re.compile(r"\bIS\s+DISTINCT\s+FROM\b", re.IGNORECASE)
# ARRAY[%s] (single-element alias array on INSERT) -> JSON array text.
_ARRAY_LIT_RE = re.compile(r"ARRAY\[\s*%s\s*\]", re.IGNORECASE)


def _translate_sql(sql: str) -> str:
    """Rewrite a psycopg2-flavored statement into SQLite dialect."""
    # col->>'key'  ->  json_extract(col,'$.key')
    sql = _JSON_ARROW_RE.sub(lambda m: f"json_extract({m.group(1)},'$.{m.group(2)}')", sql)

    # = ANY(%s::uuid[]) / = ANY(%s)  ->  IN (carray placeholder)
    # SQLite has no array param; _SqliteCursor expands the list into ?,?,? and
    # injects a sentinel here. We mark the spot with a unique token.
    sql = _ANY_UUID_RE.sub("IN (/*ANYLIST*/)", sql)
    sql = _ANY_TEXT_RE.sub("IN (/*ANYLIST*/)", sql)

    # aliases @> ARRAY[%s]::text[]  ->  membership test via json_each
    # company.aliases is stored as a JSON array string in SQLite.
    sql = _CONTAINS_RE.sub(
        "EXISTS (SELECT 1 FROM json_each(company.aliases) WHERE value = %s)",
        sql,
    )

    # array_length(aliases,1) > 0  ->  json_array_length(aliases) > 0
    sql = _ARRAY_LENGTH_RE.sub("json_array_length(aliases) > 0", sql)

    # ARRAY[%s] (single-element alias array literal on INSERT) -> json_array(%s)
    sql = _ARRAY_LIT_RE.sub("json_array(%s)", sql)

    # now() - interval 'N days'  ->  datetime('now','-N days')
    sql = _INTERVAL_RE.sub(lambda m: f"datetime('now','-{m.group(1)} days')", sql)
    # now() - interval '%s days' (parameterised count) — handled as token; the
    # cursor turns the bound int into a negative-days modifier.
    sql = _INTERVAL_PARAM_RE.sub("datetime('now', /*INTERVALDAYS*/)", sql)

    sql = _NOW_RE.sub("CURRENT_TIMESTAMP", sql)
    sql = _CURRENT_DATE_RE.sub("date('now')", sql)
    sql = _GEN_UUID_RE.sub("(lower(hex(randomblob(16))))", sql)
    sql = _ILIKE_RE.sub("LIKE", sql)
    sql = _IS_DISTINCT_RE.sub("IS NOT", sql)
    sql = _CAST_UUID_RE.sub("", sql)
    sql = _CAST_TEXT_ARR_RE.sub("", sql)
    return sql


class _SqliteCursor:
    """psycopg2-cursor-compatible wrapper around a sqlite3 cursor.

    Accepts ``%s`` placeholders, ``Json(...)`` wrapped params, ``IN (ANYLIST)``
    list expansion, and ``RealDictCursor`` dict rows.
    """

    def __init__(self, sqlite_cur, as_dict: bool):
        self._cur = sqlite_cur
        self._as_dict = as_dict

    # -- placeholder + param adaptation ------------------------------------

    def _adapt_params(self, params):
        if params is None:
            return []
        out = []
        for p in params:
            if isinstance(p, Json):
                out.append(p.dumps())
            elif isinstance(p, (list, tuple)):
                # A list param survives only as an ANYLIST expansion target;
                # passed through here it would break, so JSON-encode as a
                # fallback (covers Json-less array columns).
                out.append(_json.dumps(list(p), ensure_ascii=False, default=str))
            elif isinstance(p, dict):
                out.append(_json.dumps(p, ensure_ascii=False, default=str))
            elif isinstance(p, bool):
                out.append(1 if p else 0)
            else:
                out.append(p)
        return out

    def _prepare(self, sql, params):
        """Return (sqlite_sql, sqlite_params) ready for sqlite3.execute."""
        translated = _translate_sql(sql)
        params = list(params) if params is not None else []

        # Expand IN (/*ANYLIST*/) — the matching positional param is a list.
        if "/*ANYLIST*/" in translated:
            # Find which positional param feeds the ANYLIST. We expand the
            # FIRST list/tuple param encountered, in order, per ANYLIST token.
            def _expand(_match, _state={"i": 0}):
                # locate next list param
                idx = _state["i"]
                while idx < len(params) and not isinstance(params[idx], (list, tuple)):
                    idx += 1
                if idx >= len(params):
                    return "NULL"
                seq = list(params[idx])
                params[idx] = ("__EXPAND__", seq)
                _state["i"] = idx + 1
                return ",".join(["?"] * len(seq)) if seq else "NULL"

            translated = re.sub(r"/\*ANYLIST\*/", _expand, translated)

        # Parameterised interval days: datetime('now', /*INTERVALDAYS*/)
        if "/*INTERVALDAYS*/" in translated:
            translated = translated.replace("/*INTERVALDAYS*/", "?", 1)
            # The corresponding param is the day count; turn it into '-N days'.
            for i, p in enumerate(params):
                if isinstance(p, int) and not isinstance(p, bool):
                    params[i] = f"-{p} days"
                    break

        # %s -> ? (after operator rewrites that needed literal %s)
        translated = translated.replace("%s", "?")

        # Flatten expanded list params into the final positional list.
        final = []
        for p in params:
            if isinstance(p, tuple) and len(p) == 2 and p[0] == "__EXPAND__":
                final.extend(self._adapt_params(p[1]))
            else:
                final.extend(self._adapt_params([p]))
        return translated, final

    # -- execute -----------------------------------------------------------

    def execute(self, sql, params=None):
        sql2, params2 = self._prepare(sql, params)
        self._cur.execute(sql2, params2)
        return self

    def executemany(self, sql, seq_of_params):
        for params in seq_of_params:
            self.execute(sql, params)
        return self

    # -- fetch -------------------------------------------------------------

    def _decode(self, col_name, value):
        """Deserialize JSON-typed columns back into Python objects.

        SQLite stores JSONB/array columns as JSON TEXT. The pipeline expects
        ``locations``/``triage`` as lists, ``about``/``mission_fit``/
        ``ats_config`` as dicts, ``aliases``/``llm_*`` as lists — matching the
        psycopg2 JSONB/TEXT[] decode behavior.
        """
        if col_name in _JSON_COLUMNS and isinstance(value, str):
            try:
                return _json.loads(value)
            except (ValueError, TypeError):
                return value
        if col_name in _DATETIME_COLUMNS or col_name in _DATE_COLUMNS:
            return _decode_temporal(col_name, value)
        return value

    def _row(self, row):
        if row is None:
            return None
        if self._as_dict:
            return {k: self._decode(k, row[k]) for k in row.keys()}
        cols = [d[0] for d in (self._cur.description or [])]
        return tuple(self._decode(cols[i], v) for i, v in enumerate(row))

    def fetchone(self):
        return self._row(self._cur.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def close(self):
        self._cur.close()

    def __iter__(self):
        for r in self._cur:
            yield self._row(r)


class _SqliteConn:
    """psycopg2-connection-compatible wrapper around sqlite3.Connection."""

    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
        self.autocommit = False
        self.closed = 0

    def cursor(self, cursor_factory=None):
        as_dict = cursor_factory is RealDictCursor
        return _SqliteCursor(self._conn.cursor(), as_dict)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()
        self.closed = 1


# ---------------------------------------------------------------------------
# Connection singletons
# ---------------------------------------------------------------------------

_conn = None


def _connect_sqlite():
    path = sqlite_db_path()
    fresh = not path.exists() or path.stat().st_size == 0
    raw = sqlite3.connect(str(path))
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA journal_mode = WAL")
    if fresh:
        _apply_sqlite_schema(raw)
    else:
        # Idempotent: schema uses IF NOT EXISTS, safe to re-apply cheaply only
        # when the core table is missing (e.g. truncated file).
        cur = raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='company'"
        )
        if cur.fetchone() is None:
            _apply_sqlite_schema(raw)
    print(f"  SQLite: connected ({path})", flush=True)
    return _SqliteConn(raw)


def _apply_sqlite_schema(raw_conn):
    schema_path = PROJECT_ROOT / "sql" / "schema.sqlite.sql"
    sql = schema_path.read_text(encoding="utf-8")
    raw_conn.executescript(sql)
    raw_conn.commit()


def _connect_supabase():
    db_url = _supabase_url()
    import sys
    import psycopg2

    for attempt in range(2):
        try:
            conn = psycopg2.connect(
                db_url,
                connect_timeout=10,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
                options="-c idle_in_transaction_session_timeout=300000 -c statement_timeout=60000",
            )
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute("SET statement_timeout = 60000")
            cur.execute("SET idle_in_transaction_session_timeout = 300000")
            conn.commit()
            cur.execute("SELECT current_database(), current_user")
            db_name, db_user = cur.fetchone()
            print(f"  Supabase: connected ({db_name}, {db_user})")
            cur.close()
            return conn
        except psycopg2.OperationalError:
            if attempt == 0:
                continue
            raise
    print("ERROR: could not connect to Supabase", file=sys.stderr)
    sys.exit(1)


def get_conn():
    """Lazy singleton connection for the active backend.

    Returns a psycopg2 connection (Supabase) or a psycopg2-compatible SQLite
    wrapper. Auto-reconnects a dropped Supabase connection (PgBouncer timeout).
    """
    global _conn

    if IS_SQLITE:
        if _conn is None or _conn.closed:
            _conn = _connect_sqlite()
        return _conn

    # Supabase: verify liveness, reconnect if the pooler dropped us.
    import psycopg2

    if _conn is not None and _conn.closed == 0:
        try:
            cur = _conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return _conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            print("  Supabase: reconnecting (server closed connection)")
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None

    _conn = _connect_supabase()
    return _conn


def close_conn():
    """Explicit cleanup."""
    global _conn
    if _conn is not None and getattr(_conn, "closed", 0) == 0:
        _conn.close()
    _conn = None
