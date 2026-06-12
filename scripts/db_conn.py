"""Connection singleton — Supabase (psycopg2) or local SQLite.

Thin compatibility shim over ``db_backend``. Historically this module owned
the psycopg2 connection directly; it now delegates so the same ``get_conn()``
/ ``close_conn()`` API works against either backend. Crucially it does NOT
import psycopg2 at module level, so the SQLite path needs no Postgres driver.

Zero project imports — safe to import from any module without circular deps.
"""

from db_backend import IS_SQLITE, close_conn, get_conn  # noqa: F401

__all__ = ["get_conn", "close_conn", "IS_SQLITE"]
